import socket
import threading
import time
from typing import Optional


class DisplayClient:
    """Unix Domain Socket client for 4000-byte e-paper frame buffers."""

    BUSY_TIMEOUT = 10.0
    REFRESH_COOLDOWN = 5.0

    def __init__(self, socket_path: str = "/tmp/pinepi.sock"):
        self.socket_path = socket_path
        self._sock: Optional[socket.socket] = None
        self._busy = False
        self._last_refresh_time = 0.0
        self._last_image: bytes = b""
        self._last_page: Optional[int] = None
        self._last_online: Optional[bool] = None
        self._generation = 0
        self._lock = threading.RLock()

    @property
    def is_busy(self) -> bool:
        """Return whether the C driver may still be processing a refresh."""
        with self._lock:
            if not self._busy:
                return False
            if time.monotonic() - self._last_refresh_time >= self.BUSY_TIMEOUT:
                self._busy = False
                return False
            return True

    @property
    def can_refresh(self) -> bool:
        """Return whether a background refresh can safely be submitted.

        This deliberately calls ``is_busy`` so the estimated C-driver busy
        period expires even when no caller explicitly reads that property.
        """
        with self._lock:
            if self.is_busy:
                return False
            return time.monotonic() - self._last_refresh_time >= self.REFRESH_COOLDOWN

    @property
    def last_image(self) -> bytes:
        """Return the last frame successfully handed to the display process."""
        with self._lock:
            return self._last_image

    @property
    def last_frame_info(self):
        """Return acknowledged frame metadata as (page, online, generation)."""
        with self._lock:
            return self._last_page, self._last_online, self._generation

    def send(
        self,
        image_bytes: bytes,
        page: Optional[int] = None,
        online: Optional[bool] = None,
    ) -> bool:
        if len(image_bytes) != 4000:
            print(f"[DisplayClient] WARN: image size {len(image_bytes)} != 4000")
            return False

        with self._lock:
            self._busy = True
            self._last_refresh_time = time.monotonic()
            try:
                # The C process accepts one frame per UDS connection.
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(2.0)
                self._sock.connect(self.socket_path)
                self._sock.sendall(image_bytes)
                self._sock.close()
                self._sock = None
                self._last_image = bytes(image_bytes)
                self._last_page = page
                self._last_online = online
                self._generation += 1
                print(f"[DisplayClient] Sent {len(image_bytes)} bytes to {self.socket_path}")
                return True
            except Exception as exc:
                self._busy = False
                print(f"[DisplayClient] send error: {exc}")
                if self._sock:
                    self._sock.close()
                    self._sock = None
                return False

    def close(self):
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None
