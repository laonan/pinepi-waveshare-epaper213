import asyncio
import datetime
import os
import random
import signal
import subprocess
import sys
import time

from config import Config
from display_client import DisplayClient
from network_manager import NetworkManager
from renderer import Renderer
from state_machine import StateMachine
from touch_listener import TouchListener
from web_server import WebServer
from ws_client import WSClient


CONFIG_PATH = os.environ.get(
    "PINEPI_CONFIG", "/etc/pinepi-waveshare-epaper213/config.json"
)
DISPLAY_BIN = os.environ.get(
    "PINEPI_DISPLAY",
    "/opt/pinepi-waveshare-epaper213/bin/pinepi-waveshare-epaper213",
)

NETWORK_POLL_INTERVAL = 15
LINK_FAILURE_CONFIRMATION = 30
AP_STATION_RETRY_CONFIRMATION = 120
INTERNET_FAILURE_CONFIRMATION = 300
STABLE_CONNECTION_RESET = 120
CONFIGURED_AP_FALLBACK_DELAY = 600
UNCONFIGURED_AP_FALLBACK_DELAY = 30
RECONNECT_BASE_DELAY = 5
RECONNECT_MAX_DELAY = 300

PAGE_FOOTERS = {
    1: "Page: 1/3 (Cloud)",
    2: "Page: 2/3 (Local)",
    3: "Page: 3/3 (Config)",
}


g_loop = None
g_tasks = []
g_ws_client = None
g_web_server = None
g_network_manager = None
g_shutting_down = False


def _kill_old_display():
    """Kill any stale C display process before starting a fresh one."""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", DISPLAY_BIN],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        time.sleep(0.5)
    except Exception:
        pass


def start_display_process():
    print(f"[Main] Starting display process: {DISPLAY_BIN}")
    return subprocess.Popen(
        [DISPLAY_BIN],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        bufsize=1,
        universal_newlines=True,
    )


async def monitor_display(proc, restart_event: asyncio.Event):
    """Forward C process logs and request a restart if it exits."""
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
    while True:
        await restart_event.wait()
        restart_event.clear()
        proc = start_fn()
        asyncio.create_task(monitor_display(proc, restart_event))


async def wait_until(hour: int, minute: int):
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


def _reconnect_delay(failure_count: int) -> float:
    """Equal-jitter exponential backoff for station recovery attempts."""
    exponent = min(max(failure_count - 1, 0), 10)
    ceiling = min(RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY * (2 ** exponent))
    return random.uniform(ceiling / 2, ceiling)


def _page3_signature(net_state: dict):
    return (
        net_state.get("mode"),
        net_state.get("lan_ip"),
        net_state.get("ap_active"),
        net_state.get("ap_ip"),
    )


async def status_display_loop(
    state, display, renderer, ws, network_manager
):
    """Keep the visible frame synchronized with live cloud connectivity.

    DisplayClient metadata changes only after a frame is accepted, so failed
    navigation sends and busy periods remain pending instead of being
    optimistically acknowledged.
    """
    while True:
        await asyncio.sleep(1)
        page, generation = state.page_snapshot()
        online = ws.is_online()
        displayed_page, displayed_online, _ = display.last_frame_info

        if state.render_in_progress:
            continue
        if displayed_page != page:
            if not display.can_refresh:
                continue
            if page == 1:
                image = ws.get_cached_image()
                if image:
                    image = renderer.render_page1_status(image, online)
                else:
                    image = renderer.render_page1(is_offline=not online)
            elif page == 2:
                image = await asyncio.to_thread(renderer.render_page2)
            elif page == 3:
                net_state = await asyncio.to_thread(
                    network_manager.get_network_state
                )
                image = renderer.render_page3_with_state(net_state)
            else:
                continue
            if (
                not state.is_current(page, generation)
                or state.render_in_progress
                or not display.can_refresh
            ):
                continue
            if display.send(image, page=page, online=online):
                print(f"[StatusDisplay] Recovered missing Page {page} frame")
            continue

        if displayed_online == online or not display.can_refresh:
            continue

        if page == 1 and not ws.get_cached_image():
            image = renderer.render_page1(is_offline=not online)
        else:
            image = display.last_image
            footer = PAGE_FOOTERS.get(page)
            if len(image) != 4000 or footer is None:
                continue
            image = renderer.render_footer_status(image, footer, online)

        if display.send(image, page=page, online=online):
            print(
                f"[StatusDisplay] Page {page} footer updated: "
                f"{'ONLINE' if online else 'OFFLINE'}"
            )


async def daily_refresh_loop(state, display, renderer, ws, network_manager):
    """Perform the existing daily e-paper refresh independently of health checks."""
    while True:
        await wait_until(3, 0)
        print("[DailyRefresh] Refreshing the current page at 03:00")
        page, generation = state.page_snapshot()
        if page == 1:
            image = ws.get_cached_image()
            if image:
                image = renderer.render_page1_status(image, ws.is_online())
            else:
                image = renderer.render_page1(is_offline=not ws.is_online())
        elif page == 2:
            image = renderer.render_page2()
        elif page == 3:
            net_state = await asyncio.to_thread(network_manager.get_network_state)
            image = renderer.render_page3_with_state(net_state)
        else:
            image = b"\xff" * 4000

        if len(image) != 4000:
            image = b"\xff" * 4000
        if (
            not state.is_current(page, generation)
            or state.render_in_progress
            or not display.can_refresh
        ):
            print("[DailyRefresh] Skipped because a newer frame is active")
            continue
        display.send(image, page=page, online=ws.is_online())


async def network_loop(
    config, network_manager, state_machine, display, renderer, ws
):
    """Supervise station recovery without blocking touch or WebSocket tasks."""
    await asyncio.to_thread(
        network_manager.configure_wifi_reliability, config.wifi_networks
    )

    link_unhealthy_since = None
    internet_unhealthy_since = None
    stable_since = None
    next_reconnect_at = 0.0
    reconnect_failures = 0
    ap_retry_at = 0.0
    ap_active_since = None
    last_logged_signature = None
    observed_page3_signature = None
    pending_page3_state = None

    while True:
        try:
            net_state = await asyncio.to_thread(network_manager.get_network_state)
            now = time.monotonic()
            station_healthy = bool(net_state.get("station_healthy"))
            ap_active = bool(net_state.get("ap_active"))
            connectivity = net_state.get("connectivity", "unknown")
            ws_online = ws.is_online()
            networks = config.wifi_networks

            if ap_active:
                ap_active_since = (
                    net_state.get("ap_activated_at")
                    or ap_active_since
                    or now
                )
            else:
                ap_active_since = None

            log_signature = (
                net_state.get("wifi_ssid"),
                net_state.get("lan_ip"),
                net_state.get("has_default_route"),
                net_state.get("wifi_device_state"),
                connectivity,
                ap_active,
            )
            if log_signature != last_logged_signature:
                print(
                    "[NetworkLoop] State: "
                    f"ssid={net_state.get('wifi_ssid') or '-'} "
                    f"ip={net_state.get('lan_ip') or '-'} "
                    f"route={net_state.get('has_default_route')} "
                    f"nm={net_state.get('wifi_device_state')} "
                    f"internet={connectivity} ap={ap_active}"
                )
                last_logged_signature = log_signature

            if station_healthy:
                link_unhealthy_since = None
                if stable_since is None:
                    stable_since = now
            else:
                stable_since = None
                if link_unhealthy_since is None:
                    link_unhealthy_since = now

            # WSS is the user-visible cloud path. Cached NetworkManager
            # connectivity must not mask a stale lease; after this timer we
            # force an active connectivity check before touching the radio.
            if station_healthy and not ws_online:
                if internet_unhealthy_since is None:
                    internet_unhealthy_since = now
            else:
                internet_unhealthy_since = None

            if (
                stable_since is not None
                and now - stable_since >= STABLE_CONNECTION_RESET
                and reconnect_failures
            ):
                print("[NetworkLoop] Station stable; reconnect backoff reset")
                reconnect_failures = 0
                next_reconnect_at = 0.0

            link_outage_age = (
                now - link_unhealthy_since if link_unhealthy_since is not None else 0
            )
            internet_outage_age = (
                now - internet_unhealthy_since
                if internet_unhealthy_since is not None
                else 0
            )
            ap_grace_complete = (
                not ap_active
                or (
                    ap_active_since is not None
                    and now - ap_active_since >= AP_STATION_RETRY_CONFIRMATION
                )
            )
            link_recovery_needed = (
                not station_healthy
                and link_outage_age >= LINK_FAILURE_CONFIRMATION
                and ap_grace_complete
            )
            internet_recovery_needed = (
                station_healthy
                and internet_outage_age >= INTERNET_FAILURE_CONFIRMATION
            )

            if internet_recovery_needed:
                active_connectivity = await asyncio.to_thread(
                    network_manager.get_connectivity_state, True
                )
                if active_connectivity == "full":
                    # Wi-Fi and Internet are healthy; leave the radio alone and
                    # let WSClient continue endpoint-specific retries.
                    print(
                        "[NetworkLoop] Active Internet check passed; "
                        "cloud endpoint remains offline"
                    )
                    internet_unhealthy_since = now
                    internet_recovery_needed = False

            if (
                networks
                and (link_recovery_needed or internet_recovery_needed)
                and now >= next_reconnect_at
            ):
                attempt = reconnect_failures + 1
                aggressive = attempt >= 3 and attempt % 3 == 0
                reason = "link/DHCP" if link_recovery_needed else "Internet/cloud"
                print(
                    f"[NetworkLoop] Recovery attempt {attempt} "
                    f"for {reason} failure (radio_reset={aggressive})"
                )
                recovered = await asyncio.to_thread(
                    network_manager.ensure_best_wifi,
                    networks,
                    aggressive,
                    internet_recovery_needed,
                )
                net_state = await asyncio.to_thread(network_manager.get_network_state)
                now = time.monotonic()
                path_recovered = bool(net_state.get("station_healthy"))
                if internet_recovery_needed and path_recovered:
                    active_connectivity = await asyncio.to_thread(
                        network_manager.get_connectivity_state, True
                    )
                    if active_connectivity != "full":
                        print(
                            "[NetworkLoop] Station reactivated; active Internet "
                            f"state is {active_connectivity}, allowing cloud grace"
                        )

                if recovered and path_recovered:
                    print(
                        f"[NetworkLoop] Station recovered: "
                        f"ssid={net_state.get('wifi_ssid')} ip={net_state.get('lan_ip')}"
                    )
                    link_unhealthy_since = None
                    internet_unhealthy_since = None
                    stable_since = now
                    ap_active_since = None
                    next_reconnect_at = now + LINK_FAILURE_CONFIRMATION
                    ws.request_reconnect()
                else:
                    reconnect_failures += 1
                    delay = _reconnect_delay(reconnect_failures)
                    next_reconnect_at = now + delay
                    if net_state.get("ap_active"):
                        # ensure_best_wifi restored the AP after its bounded
                        # station probe; start a fresh AP availability window.
                        ap_active_since = (
                            net_state.get("ap_activated_at") or now
                        )
                        next_reconnect_at = max(
                            next_reconnect_at,
                            now + AP_STATION_RETRY_CONFIRMATION,
                        )
                    print(
                        f"[NetworkLoop] Recovery failed; attempt "
                        f"{reconnect_failures + 1} in {delay:.1f}s"
                    )

            # AP fallback is deliberately delayed for configured devices. A
            # short router/DHCP outage should not seize the only radio and
            # prevent NetworkManager's own station autoconnect.
            now = time.monotonic()
            link_outage_age = (
                now - link_unhealthy_since if link_unhealthy_since is not None else 0
            )
            fallback_delay = (
                CONFIGURED_AP_FALLBACK_DELAY
                if networks
                else UNCONFIGURED_AP_FALLBACK_DELAY
            )
            if (
                not station_healthy
                and link_outage_age >= fallback_delay
                and not net_state.get("ap_active")
                and now >= ap_retry_at
            ):
                print(
                    f"[NetworkLoop] Station unavailable for {link_outage_age:.0f}s; "
                    "starting configuration AP"
                )
                ap_started = await asyncio.to_thread(
                    network_manager.start_ap_if_unavailable
                )
                now = time.monotonic()
                ap_retry_at = now + 60
                net_state = await asyncio.to_thread(network_manager.get_network_state)
                if ap_started and net_state.get("ap_active"):
                    ap_active_since = (
                        net_state.get("ap_activated_at") or now
                    )
                    next_reconnect_at = max(
                        next_reconnect_at,
                        now + AP_STATION_RETRY_CONFIRMATION,
                    )
            elif station_healthy and net_state.get("ap_active"):
                print("[NetworkLoop] Healthy station detected; stopping AP")
                await asyncio.to_thread(
                    network_manager.stop_ap_if_station_available
                )
                net_state = await asyncio.to_thread(network_manager.get_network_state)

            # Redraw Page 3 only when its network information changes. Keep a
            # pending state until the display becomes available.
            page3_signature = _page3_signature(net_state)
            if observed_page3_signature is None:
                observed_page3_signature = page3_signature
            elif page3_signature != observed_page3_signature:
                observed_page3_signature = page3_signature
                if state_machine.current_page == 3:
                    pending_page3_state = net_state

            if state_machine.current_page != 3:
                pending_page3_state = None
            elif (
                pending_page3_state is not None
                and not state_machine.render_in_progress
                and display.can_refresh
            ):
                image = renderer.render_page3_with_state(pending_page3_state)
                if display.send(
                    image, page=3, online=ws.is_online()
                ):
                    pending_page3_state = None

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[NetworkLoop] Unexpected error: {type(exc).__name__}: {exc}")

        await asyncio.sleep(NETWORK_POLL_INTERVAL)


async def shutdown(signal_name):
    global g_shutting_down
    if g_shutting_down:
        return
    g_shutting_down = True
    print(f"\n[Main] Received {signal_name}, shutting down...")
    if g_ws_client:
        g_ws_client.stop()
    if g_network_manager:
        g_network_manager.cancel_pending_operations()
    if g_web_server:
        g_web_server.shutdown()
    current = asyncio.current_task()
    pending = [task for task in g_tasks if task is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _signal_handler(signum):
    asyncio.create_task(shutdown(signal.Signals(signum).name))


async def main():
    global g_loop, g_tasks, g_ws_client, g_web_server, g_network_manager
    g_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        g_loop.add_signal_handler(sig, lambda selected=sig: _signal_handler(selected))

    config = Config(CONFIG_PATH)
    state = StateMachine()
    display = DisplayClient("/tmp/pinepi.sock")
    renderer = Renderer(config)
    network_manager = NetworkManager(config)
    ws = WSClient(config, display, state, renderer)
    web = WebServer(config, network_manager, port=8080)
    g_ws_client = ws
    g_web_server = web
    g_network_manager = network_manager
    renderer.ws_client = ws

    _kill_old_display()
    display_proc = start_display_process()
    print("[Main] Waiting 1s for display process to start...")
    await asyncio.sleep(1)
    if g_shutting_down:
        display.close()
        return

    ws_task = asyncio.create_task(ws.run())
    try:
        await asyncio.wait_for(ws.connected.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    if g_shutting_down:
        ws.stop()
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)
        display.close()
        return

    image = renderer.render_page1(is_offline=not ws.is_online())
    print(f"[Main] Initial image size: {len(image)} bytes")
    print(
        f"[Main] Initial send result: "
        f"{display.send(image, page=1, online=ws.is_online())}"
    )

    restart_event = asyncio.Event()
    asyncio.create_task(monitor_display(display_proc, restart_event))
    asyncio.create_task(display_watchdog(restart_event, start_display_process))

    touch = TouchListener(
        "/tmp/pinepi-touch.sock",
        state,
        display,
        renderer,
        ws,
        network_manager,
    )
    g_tasks = [
        ws_task,
        asyncio.create_task(touch.run()),
        asyncio.create_task(
            network_loop(
                config,
                network_manager,
                state,
                display,
                renderer,
                ws,
            )
        ),
        asyncio.create_task(
            status_display_loop(
                state, display, renderer, ws, network_manager
            )
        ),
        asyncio.create_task(asyncio.to_thread(web.run)),
        asyncio.create_task(
            daily_refresh_loop(state, display, renderer, ws, network_manager)
        ),
    ]

    print("[Main] pinepi-core started. Press Ctrl+C to exit.")
    try:
        await asyncio.gather(*g_tasks)
    except asyncio.CancelledError:
        pass
    finally:
        display.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Main] KeyboardInterrupt")
    finally:
        _kill_old_display()
        sys.exit(0)
