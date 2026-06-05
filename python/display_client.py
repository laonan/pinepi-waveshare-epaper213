import socket
import os
import time


class DisplayClient:
    """Unix Domain Socket client to send 4000-byte frame buffer to C display driver"""

    def __init__(self, socket_path: str = "/tmp/pinepi.sock"):
        self.socket_path = socket_path
        self._sock = None
        self._busy = False

    @property
    def is_busy(self) -> bool:
        """Check if display is currently processing a refresh (within last 10 seconds)"""
        if not self._busy:
            return False
        # Auto-clear busy flag after 10 seconds (max refresh time)
        if time.time() - self._last_refresh_time > 10:
            self._busy = False
            return False
        return True

    @property
    def can_refresh(self) -> bool:
        """Check if enough time has passed since last refresh for periodic updates"""
        if not self._busy:
            return time.time() - self._last_refresh_time > 5  # 5 second cooldown
        return False

    def send(self, image_bytes: bytes) -> bool:
        if len(image_bytes) != 4000:
            print(f"[DisplayClient] WARN: image size {len(image_bytes)} != 4000")
            return False

        # Mark as busy before sending
        self._busy = True
        self._last_refresh_time = time.time()

        try:
            # Create fresh connection for each send (C process accepts one connection at a time)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(2.0)
            self._sock.connect(self.socket_path)
            self._sock.sendall(image_bytes)
            self._sock.close()
            self._sock = None
            print(f"[DisplayClient] Sent {len(image_bytes)} bytes to {self.socket_path}")
            # Keep busy flag set - refresh is still in progress in C driver
            return True
        except Exception as e:
            self._busy = False  # Clear busy on error
            print(f"[DisplayClient] send error: {e}")
            if self._sock:
                self._sock.close()
                self._sock = None
            return False

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None
