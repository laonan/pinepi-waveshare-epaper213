import asyncio
import json
import os
import socket
import time


class TouchListener:
    """Touch event listener supporting both UDS and legacy UDP protocols."""

    def __init__(
        self,
        socket_path: str,
        state_machine,
        display_client,
        renderer,
        ws_client,
        network_manager=None,
        udp_port: int = 5006,
    ):
        self.socket_path = socket_path
        self.state = state_machine
        self.display = display_client
        self.renderer = renderer
        self.ws = ws_client
        self.nm = network_manager
        self.udp_port = udp_port
        self._last_tap_ts = 0.0
        self._tap_lock = asyncio.Lock()

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
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
        except Exception as exc:
            print(f"[TouchListener] UDS socket unavailable: {exc}")
            return None

    def _create_udp_socket(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.udp_port))
            sock.setblocking(False)
            return sock
        except Exception as exc:
            print(f"[TouchListener] UDP socket unavailable: {exc}")
            return None

    async def _uds_loop(self, loop, sock):
        while True:
            try:
                data, _ = await loop.sock_recvfrom(sock, 1024)
                await self._process_raw_message(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[TouchListener] UDS receive error: {exc}")
                await asyncio.sleep(0.1)

    async def _udp_loop(self, loop, sock):
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 1024)
                await self._process_raw_message(data, addr)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[TouchListener] UDP receive error: {exc}")
                await asyncio.sleep(0.1)

    async def _process_raw_message(self, data: bytes, addr=None):
        message = data.decode("utf-8", errors="ignore").strip()
        if message == "TAP":
            suffix = f" from {addr}" if addr else ""
            print(f"[TouchListener] TAP received (legacy UDP){suffix}")
            await self._handle_tap()
            return

        try:
            event = json.loads(message)
            if event.get("type") == "tap":
                print(f"[TouchListener] TAP received (ts={event.get('ts', 0)})")
                await self._handle_tap()
                return
        except json.JSONDecodeError:
            pass
        print(f"[TouchListener] Invalid touch payload: {message}")

    async def _handle_tap(self):
        async with self._tap_lock:
            now = time.monotonic()
            if now - self._last_tap_ts < 0.3:
                return
            self._last_tap_ts = now
            new_page = self.state.next_page()
            _, generation = self.state.page_snapshot()
            print(f"[TouchListener] Switch to page {new_page}")
            self.state.begin_render()
            try:
                await self._dispatch_page(new_page, generation)
            finally:
                self.state.end_render()

    async def _dispatch_page(self, page: int, generation: int):
        sent = False
        if page == 1:
            online = self.ws.is_online()
            image = self.ws.get_cached_image()
            if image:
                image = self.renderer.render_page1_status(image, online)
            else:
                image = self.renderer.render_page1(is_offline=not online)
            sent = self.state.is_current(page, generation) and self.display.send(
                image, page=1, online=online
            )

        elif page == 2:
            online = self.ws.is_online()
            image = self.renderer.render_page2()
            sent = self.state.is_current(page, generation) and self.display.send(
                image, page=2, online=online
            )

        elif page == 3:
            print("[TouchListener] Entering Page 3 (network config)")
            net_state = None
            if self.nm:
                # NetworkManager calls can take tens of seconds while DHCP is
                # broken. Keep them off the asyncio loop so WSS ping/pong and
                # the status footer continue to run.
                net_state = await asyncio.to_thread(self.nm.get_network_state)
                if net_state["station_healthy"]:
                    print(
                        f"[TouchListener] Page 3: healthy station "
                        f"{net_state['wifi_ssid']} at {net_state['lan_ip']}"
                    )
                    if net_state["ap_active"]:
                        print("[TouchListener] Page 3: stopping AP (LAN available)")
                        await asyncio.to_thread(
                            self.nm.stop_ap_if_station_available
                        )
                        net_state = await asyncio.to_thread(
                            self.nm.get_network_state
                        )
                elif net_state["ap_active"]:
                    print(
                        f"[TouchListener] Page 3: AP already active "
                        f"(SSID: {net_state['ap_ssid']})"
                    )
                else:
                    print("[TouchListener] Page 3: no LAN; starting AP")
                    await asyncio.to_thread(
                        self.nm.start_ap_if_unavailable
                    )
                    net_state = await asyncio.to_thread(
                        self.nm.get_network_state
                    )
                image = self.renderer.render_page3_with_state(net_state)
            else:
                print("[TouchListener] No NetworkManager; using legacy Page 3")
                image = self.renderer.render_page3()
            sent = self.state.is_current(page, generation) and self.display.send(
                image, page=3, online=self.ws.is_online()
            )
            if net_state:
                print(
                    f"[TouchListener] Page 3 rendered: "
                    f"mode={net_state.get('mode', 'unknown')}, "
                    f"lan_ip={net_state.get('lan_ip')}, "
                    f"ap_active={net_state.get('ap_active')}"
                )

        print(f"[TouchListener] Page {page} dispatch sent={sent}")
