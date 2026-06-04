import asyncio
import json
import socket
import os
import time

class TouchListener:
    """Touch event listener supporting both UDS and legacy UDP protocols"""

    def __init__(self, socket_path: str, state_machine, display_client, renderer, ws_client, network_manager=None, udp_port: int = 5006):
        self.socket_path = socket_path
        self.state = state_machine
        self.display = display_client
        self.renderer = renderer
        self.ws = ws_client
        self.nm = network_manager
        self.udp_port = udp_port
        self._last_tap_ts = 0

    async def run(self):
        uds_sock = self._create_uds_socket()
        udp_sock = self._create_udp_socket()

        if uds_sock is None and udp_sock is None:
            print("[TouchListener] No touch socket available, exiting touch listener")
            return

        loop = asyncio.get_running_loop()
        tasks = []

        if uds_sock is not None:
            print(f"[TouchListener] Listening on UDS {self.socket_path}")
            tasks.append(asyncio.create_task(self._uds_loop(loop, uds_sock)))

        if udp_sock is not None:
            print(f"[TouchListener] Listening on UDP 0.0.0.0:{self.udp_port}")
            tasks.append(asyncio.create_task(self._udp_loop(loop, udp_sock)))

        try:
            await asyncio.gather(*tasks)
        finally:
            if uds_sock is not None:
                uds_sock.close()
            if udp_sock is not None:
                udp_sock.close()

    def _create_uds_socket(self):
        try:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(self.socket_path)
            sock.setblocking(False)
            return sock
        except Exception as e:
            print(f"[TouchListener] UDS socket unavailable: {e}")
            return None

    def _create_udp_socket(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.udp_port))
            sock.setblocking(False)
            return sock
        except Exception as e:
            print(f"[TouchListener] UDP socket unavailable: {e}")
            return None

    async def _uds_loop(self, loop, sock):
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 1024)
                await self._process_raw_message(data)
            except Exception as e:
                print(f"[TouchListener] UDS receive error: {e}")
                await asyncio.sleep(0.1)

    async def _udp_loop(self, loop, sock):
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 1024)
                await self._process_raw_message(data, addr)
            except Exception as e:
                print(f"[TouchListener] UDP receive error: {e}")
                await asyncio.sleep(0.1)

    async def _process_raw_message(self, data: bytes, addr=None):
        msg = data.decode("utf-8", errors="ignore").strip()
        if msg == "TAP":
            print(f"[TouchListener] TAP received (legacy UDP){' from ' + str(addr) if addr else ''}")
            self._handle_tap()
            return

        try:
            event = json.loads(msg)
            if event.get("type") == "tap":
                ts = event.get("ts", 0)
                print(f"[TouchListener] TAP received (ts={ts})")
                self._handle_tap()
                return
        except json.JSONDecodeError:
            pass

        print(f"[TouchListener] Invalid touch payload: {msg}")

    def _handle_tap(self):
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_tap_ts < 300:
            return
        self._last_tap_ts = now_ms
        new_page = self.state.next_page()
        print(f"[TouchListener] Switch to page {new_page}")
        self._dispatch_page(new_page)

    def _dispatch_page(self, page: int):
        sent = False
        if page == 1:
            # Page 1: Cloud dashboard - send latest cached cloud image, or show default/offline page if no cache
            img = self.ws.get_cached_image()
            if img:
                sent = self.display.send(img)
            else:
                is_offline = self.nm.is_online() if self.nm else False
                img = self.renderer.render_page1(is_offline=not is_offline)
                sent = self.display.send(img)
        elif page == 2:
            # Page 2: Local system monitor
            img = self.renderer.render_page2()
            sent = self.display.send(img)
        elif page == 3:
            # Page 3: Config page QR code
            print("[TouchListener] Entering Page 3 (network config)")
            net_state = None
            if self.nm:
                # Use unified network state for consistent behavior
                net_state = self.nm.get_network_state()
                if net_state["lan_ip"]:
                    print(f"[TouchListener] Page 3: detected LAN IP {net_state['lan_ip']}")
                    # If LAN IP available but AP is active, stop AP immediately
                    if net_state["ap_active"]:
                        print("[TouchListener] Page 3: stopping AP (LAN IP available)")
                        self.nm.stop_ap()
                elif net_state["ap_active"]:
                    print(f"[TouchListener] Page 3: AP is already active (SSID: {net_state['ap_ssid']})")
                else:
                    # No usable LAN IP and AP not active - start AP immediately
                    print("[TouchListener] Page 3: no usable LAN IP, starting AP now...")
                    ap_started = self.nm.create_ap()
                    if ap_started:
                        # Re-check state after starting AP
                        net_state = self.nm.get_network_state()
                        if net_state["ap_active"]:
                            print("[TouchListener] Page 3: AP started successfully")
                        else:
                            print("[TouchListener] Page 3: AP start reported success but not confirmed active")
                    else:
                        print("[TouchListener] Page 3: AP failed to start")
                img = self.renderer.render_page3_with_state(net_state)
            else:
                # Fallback to legacy method if no network manager
                print("[TouchListener] Page 3: no NetworkManager, using legacy render")
                img = self.renderer.render_page3()
            sent = self.display.send(img)
            if net_state:
                print(f"[TouchListener] Page 3 rendered: mode={net_state.get('mode', 'unknown')}, "
                      f"lan_ip={net_state.get('lan_ip')}, ap_active={net_state.get('ap_active')}")
        print(f"[TouchListener] Page {page} dispatch sent={sent}")
