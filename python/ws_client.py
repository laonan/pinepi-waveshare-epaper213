import asyncio
import json
import time
import websockets
from websockets.exceptions import ConnectionClosed
from PIL import Image

class WSClient:
    """WebSocket Secure client: connects to cloud, receives 4000-byte bitmap images"""

    def __init__(self, config, display_client, state_machine, renderer=None):
        self.config = config
        self.display = display_client
        self.state = state_machine
        self.renderer = renderer
        self._cached_image: bytes = b""
        self._running = True

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

    async def run(self):
        while self._running:
            url = self.config.wss_url
            if not url or not url.startswith("wss://"):
                print("[WSClient] WSS URL not configured, retry in 10s...")
                await asyncio.sleep(10)
                continue
            if not (self.config.auth_token or "").strip():
                print("[WSClient] Auth token is empty, retry in 10s...")
                await asyncio.sleep(10)
                continue

            try:
                print(f"[WSClient] Connecting to {url} (token {self._token_hint()})...")
                
                # Wrap entire connection attempt in timeout to prevent hanging
                ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        ping_interval=None,
                        ping_timeout=None,
                        close_timeout=5,
                        open_timeout=10,
                    ),
                    timeout=15  # Total connection timeout including handshake
                )
                
                try:
                    # Authentication with timeout
                    await asyncio.wait_for(
                        ws.send(self._auth_message()),
                        timeout=5
                    )
                    print("[WSClient] Connected; auth token sent")

                    # Add a task to monitor connection health
                    last_pong_time = time.time()
                    PING_INTERVAL = 30  # Send ping every 30 seconds
                    CONNECTION_TIMEOUT = 60  # Consider dead after 60 seconds without activity
                    
                    async def ping_loop():
                        """Send periodic pings to detect dead connections"""
                        nonlocal last_pong_time
                        while True:
                            await asyncio.sleep(PING_INTERVAL)
                            try:
                                await asyncio.wait_for(ws.ping(), timeout=5)
                                last_pong_time = time.time()
                                print("[WSClient] Ping sent successfully")
                            except Exception:
                                break
                    
                    ping_task = asyncio.create_task(ping_loop())
                    
                    try:
                        async for message in ws:
                            # Update last activity on any message
                            last_pong_time = time.time()
                            
                            if isinstance(message, bytes) and len(message) == 4000:
                                # Server sends landscape (250×122), convert to portrait (122×250)
                                img = Image.frombytes("1", (250, 122), message)
                                img = img.rotate(270, expand=True)
                                if img.size != (122, 250):
                                    img = img.resize((122, 250))
                                display_bytes = img.tobytes()
                                self._cached_image = display_bytes
                                print("[WSClient] Received 4000-byte image")
                                # If currently on Page 1, flush to screen immediately
                                if self.state.current_page == 1:
                                    self.display.send(display_bytes)
                            else:
                                text = message.decode("utf-8", errors="ignore") if isinstance(message, bytes) else str(message)
                                if await self._handle_text_message(ws, text):
                                    continue
                                print(f"[WSClient] Text message: {text}")
                            
                            # Check if connection is stale
                            if time.time() - last_pong_time > CONNECTION_TIMEOUT:
                                print("[WSClient] Connection appears dead (no activity for 60s)")
                                break
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                finally:
                    await ws.close()

            except ConnectionClosed as e:
                print(f"[WSClient] Connection closed: {e}, reconnecting in 5s...")
            except asyncio.TimeoutError:
                print("[WSClient] Connection timeout (network may be down), reconnecting in 5s...")
            except OSError as e:
                # Catch network errors like "Network is unreachable"
                print(f"[WSClient] Network error: {e}, reconnecting in 5s...")
            except Exception as e:
                print(f"[WSClient] Error: {type(e).__name__}: {e}, reconnecting in 5s...")

            await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _handle_text_message(self, ws, text: str) -> bool:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False

        if data.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            print("[WSClient] Ping received; pong sent")
            return True

        # New format: check if epaper is in targets array
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

            img = self.renderer.render_page1_message(str(title), str(content))
            self._cached_image = img
            print(f"[WSClient] Message rendered title={str(title)[:32]!r}")
            if self.state.current_page == 1:
                ok = self.display.send(img)
                print(f"[WSClient] Message displayed sent={ok}")
            return True

        return False
