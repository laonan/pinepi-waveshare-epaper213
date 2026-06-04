import socket
import os


class DisplayClient:
    """Unix Domain Socket client to send 4000-byte frame buffer to C display driver"""

    def __init__(self, socket_path: str = "/tmp/pinepi.sock"):
        self.socket_path = socket_path
        self._sock = None

    def send(self, image_bytes: bytes) -> bool:
        if len(image_bytes) != 4000:
            print(f"[DisplayClient] WARN: image size {len(image_bytes)} != 4000")
            return False

        try:
            # Create fresh connection for each send (C process accepts one connection at a time)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(2.0)
            self._sock.connect(self.socket_path)
            self._sock.sendall(image_bytes)
            self._sock.close()
            self._sock = None
            print(f"[DisplayClient] Sent {len(image_bytes)} bytes to {self.socket_path}")
            return True
        except Exception as e:
            print(f"[DisplayClient] send error: {e}")
            if self._sock:
                self._sock.close()
                self._sock = None
            return False

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None
