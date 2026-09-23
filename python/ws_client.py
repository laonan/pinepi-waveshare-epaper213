import asyncio
import json
import random
import time
from collections import deque

import websockets
from PIL import Image
from websockets.exceptions import ConnectionClosed


class WSClient:
    """Resilient cloud WebSocket client for 4000-byte bitmap messages."""

    MIN_REFRESH_INTERVAL = 180
    REFRESH_POLL_INTERVAL = 5
    CONNECTION_TIMEOUT = 60
    PING_INTERVAL = 30
    PING_TIMEOUT = 8
    RECONNECT_BASE_DELAY = 2
    RECONNECT_MAX_DELAY = 60
    STABLE_CONNECTION_TIME = 120
    MAX_QUEUED_MESSAGES = 100

    def __init__(self, config, display_client, state_machine, renderer=None):
        self.config = config
        self.display = display_client
        self.state = state_machine
        self.renderer = renderer
        self._cached_image: bytes = b""
        self._running = True
        self._connected = False
        self._last_activity = 0.0
        self._ws = None
        self.connected = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._reconnect_now = asyncio.Event()
        self._refresh_task = None
        self._msg_queue: deque = deque()
        self._latest_refresh_at = 0.0

    def get_cached_image(self) -> bytes:
        return self._cached_image

    def _auth_message(self) -> str:
        token = (self.config.auth_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return json.dumps({"token": token, "device": "epaper"})

    def _token_hint(self) -> str:
        token = (self.config.auth_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return "empty"
        return f"len={len(token)} tail=...{token[-4:]}"

    @classmethod
    def _reconnect_delay(cls, failure_count: int) -> float:
        """Equal-jitter exponential backoff; bounded to avoid retry storms."""
        exponent = min(max(failure_count - 1, 0), 10)
        ceiling = min(
            cls.RECONNECT_MAX_DELAY,
            cls.RECONNECT_BASE_DELAY * (2 ** exponent),
        )
        return random.uniform(ceiling / 2, ceiling)

    async def _wait_for_retry(self, delay: float) -> None:
        if self._reconnect_now.is_set():
            self._reconnect_now.clear()
            return
        try:
            await asyncio.wait_for(self._reconnect_now.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        finally:
            self._reconnect_now.clear()

    async def run(self):
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_consumer())

        failure_count = 0
        try:
            while self._running:
                url = self.config.wss_url
                if not url or not url.startswith("wss://"):
                    print("[WSClient] WSS URL not configured; retrying in 10s")
                    await self._wait_for_retry(10)
                    continue
                if not (self.config.auth_token or "").strip():
                    print("[WSClient] Auth token is empty; retrying in 10s")
                    await self._wait_for_retry(10)
                    continue

                connected_at = 0.0
                ws = None
                try:
                    print(f"[WSClient] Connecting to {url} (token {self._token_hint()})...")
                    ws = await asyncio.wait_for(
                        websockets.connect(
                            url,
                            ping_interval=None,
                            ping_timeout=None,
                            close_timeout=5,
                            open_timeout=10,
                        ),
                        timeout=15,
                    )
                    self._ws = ws
                    await asyncio.wait_for(ws.send(self._auth_message()), timeout=5)
                    print("[WSClient] Connected; auth token sent")

                    connected_at = time.monotonic()
                    self._connected = True
                    self._last_activity = connected_at
                    self.connected.set()

                    async def ping_loop():
                        while self._running:
                            await asyncio.sleep(self.PING_INTERVAL)
                            try:
                                pong_waiter = await ws.ping()
                                await asyncio.wait_for(
                                    pong_waiter, timeout=self.PING_TIMEOUT
                                )
                                self._last_activity = time.monotonic()
                                print("[WSClient] Ping/pong completed successfully")
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                print(
                                    f"[WSClient] Ping failed ({type(exc).__name__}); closing stale socket"
                                )
                                try:
                                    await ws.close()
                                except Exception:
                                    pass
                                return

                    ping_task = asyncio.create_task(ping_loop())
                    try:
                        async for message in ws:
                            self._last_activity = time.monotonic()
                            if isinstance(message, bytes) and len(message) == 4000:
                                image = Image.frombytes("1", (250, 122), message)
                                image = image.rotate(270, expand=True)
                                if image.size != (122, 250):
                                    image = image.resize((122, 250))
                                print("[WSClient] Received 4000-byte image; enqueuing")
                                self._enqueue(image.tobytes())
                            else:
                                text = (
                                    message.decode("utf-8", errors="ignore")
                                    if isinstance(message, bytes)
                                    else str(message)
                                )
                                if await self._handle_text_message(ws, text):
                                    continue
                                print(f"[WSClient] Text message: {text}")
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)

                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    print(f"[WSClient] Connection closed: {exc}")
                except asyncio.TimeoutError:
                    print("[WSClient] Connection timeout (network may be down)")
                except OSError as exc:
                    print(f"[WSClient] Network error: {exc}")
                except Exception as exc:
                    print(f"[WSClient] Error: {type(exc).__name__}: {exc}")
                finally:
                    connection_age = (
                        time.monotonic() - connected_at if connected_at else 0.0
                    )
                    self._connected = False
                    self._last_activity = 0.0
                    self.connected.clear()
                    self._ws = None
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:
                            pass

                if not self._running:
                    break
                if connection_age >= self.STABLE_CONNECTION_TIME:
                    failure_count = 0
                failure_count += 1
                delay = self._reconnect_delay(failure_count)
                print(
                    f"[WSClient] Reconnect attempt {failure_count} in {delay:.1f}s"
                )
                await self._wait_for_retry(delay)
        finally:
            self._connected = False
            self.connected.clear()
            if self._refresh_task:
                self._refresh_task.cancel()
                await asyncio.gather(self._refresh_task, return_exceptions=True)
                self._refresh_task = None

    def request_reconnect(self) -> None:
        """Wake retry sleep and close a socket invalidated by network repair."""
        self._reconnect_now.set()
        if self._ws is not None:
            try:
                asyncio.create_task(self._ws.close())
            except RuntimeError:
                pass

    def stop(self):
        self._running = False
        self._stop_event.set()
        self.request_reconnect()

    def _enqueue(self, payload: bytes) -> None:
        if len(self._msg_queue) >= self.MAX_QUEUED_MESSAGES:
            self._msg_queue.popleft()
            print("[WSClient] Message queue full; discarded oldest pending message")
        self._msg_queue.append(
            {
                "received_at": time.monotonic(),
                "payload": payload,
            }
        )
        print(f"[WSClient] Message enqueued (queue size={len(self._msg_queue)})")

    def _decorate_page1(self, payload: bytes) -> bytes:
        if self.renderer is None:
            return payload
        return self.renderer.render_page1_status(payload, self.is_online())

    def _refresh_screen(self, payload: bytes) -> None:
        # Raw cloud bitmaps receive the same live footer as rendered text.
        decorated = self._decorate_page1(payload)
        self._cached_image = decorated
        if self.state.current_page == 1:
            ok = self.display.send(
                decorated, page=1, online=self.is_online()
            )
            print(f"[WSClient] Display refresh sent={ok}")

    async def _refresh_consumer(self) -> None:
        while self._running:
            await asyncio.sleep(self.REFRESH_POLL_INTERVAL)
            if not self._msg_queue:
                continue
            elapsed = time.monotonic() - self._latest_refresh_at
            if elapsed < self.MIN_REFRESH_INTERVAL:
                continue
            # Do not collide with a touch or status refresh already being
            # processed by the e-paper driver. Keep the queue head pending.
            if self.state.current_page == 1 and not self.display.can_refresh:
                continue

            item = self._msg_queue.popleft()
            self._refresh_screen(item["payload"])
            self._latest_refresh_at = time.monotonic()
            print(
                f"[WSClient] Refreshed queued message "
                f"(waited {time.monotonic() - item['received_at']:.0f}s, "
                f"queue remaining={len(self._msg_queue)})"
            )

    async def _handle_text_message(self, ws, text: str) -> bool:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False

        if data.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            print("[WSClient] Ping received; pong sent")
            return True

        targets = data.get("targets", [])
        if "epaper" not in targets:
            return False

        event = data.get("event", {})
        title = event.get("title")
        content = event.get("content")
        if title is not None and content is not None:
            if self.renderer is None:
                print("[WSClient] Message received, but renderer is unavailable")
                return True
            image = self.renderer.render_page1_message(str(title), str(content))
            print(
                f"[WSClient] Message rendered title={str(title)[:32]!r}; enqueuing"
            )
            self._enqueue(image)
            return True
        return False

    def is_online(self) -> bool:
        if not self._connected:
            return False
        return time.monotonic() - self._last_activity <= self.CONNECTION_TIMEOUT
