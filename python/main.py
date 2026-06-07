import asyncio
import datetime
import os
import signal
import subprocess
import sys
import time

from PIL import ImageDraw

from config import Config
from state_machine import StateMachine
from display_client import DisplayClient
from touch_listener import TouchListener
from renderer import Renderer
from network_manager import NetworkManager
from ws_client import WSClient
from web_server import WebServer

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("PINEPI_CONFIG", "/etc/pinepi-waveshare-epaper213/config.json")
DISPLAY_BIN = os.environ.get("PINEPI_DISPLAY", "/opt/pinepi-waveshare-epaper213/bin/pinepi-waveshare-epaper213")

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------
g_loop = None
g_tasks = []
g_ws_client = None


def _kill_old_display():
    """Kill any existing C display process before starting"""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", DISPLAY_BIN],
            stderr=subprocess.DEVNULL, timeout=5
        )
        time.sleep(0.5)
    except Exception:
        pass


def start_display_process():
    """Start C display process via subprocess with separate session to avoid signal propagation"""
    print(f"[Main] Starting display process: {DISPLAY_BIN}")
    proc = subprocess.Popen(
        [DISPLAY_BIN],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        bufsize=1,
        universal_newlines=True,
    )
    return proc


async def monitor_display(proc, restart_event: asyncio.Event):
    """Watchdog: monitor C process stdout and restart on crash"""
    loop = asyncio.get_running_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, proc.stdout.readline)
        except Exception:
            line = ""

        if line:
            print(f"[Display] {line.rstrip()}")
            continue

        ret = proc.poll()
        if ret is None:
            await asyncio.sleep(0.1)
            continue

        print(f"[Watchdog] Display exited with code {ret}, restarting in 2s...")
        await asyncio.sleep(2)
        restart_event.set()
        return


async def display_watchdog(restart_event: asyncio.Event, start_fn):
    """Outer watchdog loop responsible for spawning new processes"""
    while True:
        await restart_event.wait()
        restart_event.clear()
        proc = start_fn()
        # Hand off new process to monitor_display for continued monitoring
        asyncio.create_task(monitor_display(proc, restart_event))


async def wait_until(hour: int, minute: int):
    """Sleep until the next occurrence of hour:minute today (or tomorrow)."""
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


async def keep_alive_loop(state, display, renderer, ws, network_manager):
    """Trigger full refresh daily at 03:00 to prevent e-paper screen burn-in
    Also monitor WebSocket health every 5 minutes"""
    last_ws_check = 0
    WS_CHECK_INTERVAL = 300  # 5 minutes
    
    while True:
        # Check WebSocket health every 5 minutes
        current_time = time.time()
        if current_time - last_ws_check >= WS_CHECK_INTERVAL:
            last_ws_check = current_time
            print("[KeepAlive] Checking WebSocket health...")
            if ws._running and network_manager.is_online():
                # If we're online but no recent WebSocket activity, it might be stuck
                cached_img = ws.get_cached_image()
                if not cached_img:
                    print("[KeepAlive] WARNING: WebSocket appears stuck (no cached image)")
                    # The ws.run() task should handle reconnection automatically
            else:
                print(f"[KeepAlive] WebSocket status: running={ws._running}, online={network_manager.is_online()}")
        
        # Wait until 03:00 for daily refresh
        await wait_until(3, 0)  # Daily at 03:00
        print("[KeepAlive] Daily refresh at 03:00 to prevent screen burn-in")

        page = state.current_page
        img = None
        if page == 1:
            img = ws.get_cached_image()
            if not img or len(img) != 4000:
                is_online = network_manager.is_online()
                img = renderer.render_page1(is_offline=not is_online)
        elif page == 2:
            img = renderer.render_page2()
        elif page == 3:
            # Use unified network state for consistent Page 3 rendering
            net_state = network_manager.get_network_state()
            img = renderer.render_page3_with_state(net_state)

        if not img or len(img) != 4000:
            # Fallback: blank white image (4000 bytes of 0xFF)
            img = b'\xff' * 4000

        display.send(img)


async def network_loop(config, network_manager, state_machine, display, renderer):
    """Background coroutine: check network status periodically, auto-switch to AP when no usable LAN IP, ensure Station mode when LAN IP available.
    Designed to be non-intrusive with netplan-managed connections."""
    ap_active = False
    last_mode = None  # "lan" | "ap" | None
    last_wifi_reconnect_attempt = 0  # Track last reconnection attempt time
    WIFI_RECONNECT_INTERVAL = 300  # 5 minutes between reconnection attempts (was 60s, too aggressive)
    lan_ip_missing_count = 0  # Track consecutive missing LAN IP checks
    LAN_IP_MISSING_THRESHOLD = 3  # Require 3 consecutive failures (45 seconds) before triggering AP mode
    grace_period_end = 0  # Grace period after reconnection to avoid flip-flopping

    while True:
        # Get unified network state
        net_state = network_manager.get_network_state()
        has_lan_ip = net_state["lan_ip"] is not None
        mode = net_state["mode"]

        # Log state changes
        if mode != last_mode:
            if mode == "lan":
                print(f"[NetworkLoop] Mode change: LAN config mode (IP: {net_state['lan_ip']})")
            elif mode == "ap":
                print(f"[NetworkLoop] Mode change: AP config mode (SSID: {net_state['ap_ssid']})")
            else:
                print(f"[NetworkLoop] Mode change: Unavailable (no LAN IP, AP not active)")
            last_mode = mode

        # Decision: should AP be active?
        # AP should be active when there's no usable LAN IP - but with debouncing
        # to avoid triggering on transient DHCP renewal glitches
        if has_lan_ip:
            lan_ip_missing_count = 0  # Reset counter on successful detection
            # If we recently reconnected, extend grace period
            if time.time() < grace_period_end:
                grace_period_end = time.time() + 30  # Extend grace period
        else:
            lan_ip_missing_count += 1

        # Only trigger AP mode after sustained absence of LAN IP
        # This prevents flip-flopping during DHCP renewals (~70 second outages observed)
        should_ap_be_active = lan_ip_missing_count >= LAN_IP_MISSING_THRESHOLD

        # When in grace period, don't attempt any reconnection (avoid nmcli interference with netplan)
        in_grace_period = time.time() < grace_period_end

        # When no LAN IP detected, try to reconnect - but only if grace period passed
        if not has_lan_ip and not in_grace_period and lan_ip_missing_count >= LAN_IP_MISSING_THRESHOLD - 1:
            current_time = time.time()
            if current_time - last_wifi_reconnect_attempt >= WIFI_RECONNECT_INTERVAL:
                if config.wifi_networks:
                    mode_str = "AP mode" if ap_active else "Station mode (no IP)"
                    print(f"[NetworkLoop] In {mode_str}, attempting Wi-Fi reconnection...")
                    print(f"[NetworkLoop] NOTE: Using non-intrusive method for netplan compatibility")
                    # Use lightweight check first - don't disconnect if already associated
                    current_ssid = network_manager.get_current_wifi_ssid()
                    if current_ssid:
                        print(f"[NetworkLoop] Already associated with {current_ssid}, waiting for DHCP...")
                        # Give DHCP more time before attempting reconnection
                        grace_period_end = time.time() + 60  # 60 second grace period
                    else:
                        reconnected = network_manager.ensure_best_wifi(config.wifi_networks)
                        if reconnected:
                            print("[NetworkLoop] Wi-Fi reconnected! 5-minute grace period starting.")
                            grace_period_end = time.time() + 300  # 5-minute grace period
                            net_state = network_manager.get_network_state()
                            has_lan_ip = net_state["lan_ip"] is not None
                            lan_ip_missing_count = 0
                        else:
                            print("[NetworkLoop] Wi-Fi reconnection failed, will retry in 5 minutes")
                    last_wifi_reconnect_attempt = current_time
                else:
                    last_wifi_reconnect_attempt = current_time

        if should_ap_be_active and not ap_active:
            print("[NetworkLoop] No usable LAN IP -> starting AP mode")
            ap_started = network_manager.create_ap()
            if ap_started:
                # Verify AP actually started
                await asyncio.sleep(1)  # Give nmcli time to activate
                ap_is_active = network_manager.is_ap_active()
                if ap_is_active:
                    ap_active = True
                    print("[NetworkLoop] AP mode confirmed active")
                else:
                    print("[NetworkLoop] AP start reported success but AP is not active, will retry")
            else:
                print("[NetworkLoop] AP mode failed to start, will retry")

            # Refresh Page 3 if needed (skip if display is busy from user interaction)
            if state_machine.current_page == 3 and display.can_refresh:
                img = renderer.render_page3_with_state(net_state)
                display.send(img)

        elif has_lan_ip and ap_active:
            print("[NetworkLoop] Usable LAN IP available -> stopping AP mode")
            ap_stopped = network_manager.stop_ap()
            if ap_stopped:
                ap_active = False
                print("[NetworkLoop] AP mode stopped")
            else:
                print("[NetworkLoop] AP stop failed, will retry")

            # Refresh Page 3 if needed (skip if display is busy from user interaction)
            if state_machine.current_page == 3 and display.can_refresh:
                img = renderer.render_page3_with_state(net_state)
                display.send(img)

        # If Page 3 is active, refresh periodically to show current state
        # (for when AP is still starting or user just connected to Wi-Fi)
        # Skip if display is currently busy (user may be turning pages)
        if state_machine.current_page == 3 and display.can_refresh:
            img = renderer.render_page3_with_state(net_state)
            display.send(img)

        await asyncio.sleep(15)


async def shutdown(signal_name):
    print(f"\n[Main] Received {signal_name}, shutting down...")
    if g_ws_client:
        g_ws_client.stop()
    for t in g_tasks:
        t.cancel()
    await asyncio.gather(*g_tasks, return_exceptions=True)
    g_loop.stop()


def _signal_handler(signum):
    asyncio.create_task(shutdown(signal.Signals(signum).name))


async def main():
    global g_loop, g_tasks, g_ws_client
    g_loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        g_loop.add_signal_handler(sig, lambda s=sig: _signal_handler(s))

    # ------------------------------------------------------------------
    # Init modules
    # ------------------------------------------------------------------
    config = Config(CONFIG_PATH)
    state = StateMachine()
    display = DisplayClient("/tmp/pinepi.sock")
    renderer = Renderer(config)
    nm = NetworkManager(config)
    ws = WSClient(config, display, state, renderer)
    g_ws_client = ws
    web = WebServer(config, nm, port=8080)

    # ------------------------------------------------------------------
    # Pre-launch: kill stale display, then start fresh
    # ------------------------------------------------------------------
    _kill_old_display()
    display_proc = start_display_process()

    # C process binds UDS immediately on startup, just wait for process creation
    print("[Main] Waiting 1s for display process to start...")
    await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Send initial Page 1 (blank or cached cloud image)
    # ------------------------------------------------------------------
    # Show default page on first startup to indicate system is ready
    img = renderer.render_page1()
    print(f"[Main] Initial image size: {len(img)} bytes")
    ok = display.send(img)
    print(f"[Main] Initial send result: {ok}")

    # ------------------------------------------------------------------
    # Start all background tasks
    # ------------------------------------------------------------------
    restart_event = asyncio.Event()
    asyncio.create_task(monitor_display(display_proc, restart_event))
    asyncio.create_task(display_watchdog(restart_event, start_display_process))

    touch = TouchListener("/tmp/pinepi-touch.sock", state, display, renderer, ws, nm)

    g_tasks = [
        asyncio.create_task(ws.run()),
        asyncio.create_task(touch.run()),
        asyncio.create_task(network_loop(config, nm, state, display, renderer)),
        asyncio.create_task(asyncio.to_thread(web.run)),
        asyncio.create_task(keep_alive_loop(state, display, renderer, ws, nm)),
    ]

    print("[Main] pinepi-core started. Press Ctrl+C to exit.")
    await asyncio.gather(*g_tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Main] KeyboardInterrupt")
    finally:
        _kill_old_display()
        sys.exit(0)
