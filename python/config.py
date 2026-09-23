import json
import os
import threading
from typing import Any, Dict, List


DEFAULT_CONFIG = {
    "wifi_networks": [],
    "wss_url": "wss://example.com/ws",
    "auth_token": "",
    "ap_ssid": "PinePi-Config",
    "ap_password": "12345678",
}


class Config:
    """Thread-safe, atomic persistent configuration manager."""

    def __init__(self, path: str = "/etc/pinepi-waveshare-epaper213/config.json"):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    return {**DEFAULT_CONFIG, **json.load(handle)}
            except Exception as exc:
                print(f"[Config] Failed to load {self.path}: {exc}")
        return dict(DEFAULT_CONFIG)

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path) or "."
        temp_path = f"{self.path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except Exception as exc:
            print(f"[Config] Failed to save: {exc}")
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def update(self, **values) -> None:
        """Update related settings with one atomic disk replacement."""
        with self._lock:
            self._data.update(values)
            self._save_locked()

    @property
    def wifi_networks(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(network) for network in self._data.get("wifi_networks", [])]

    @wifi_networks.setter
    def wifi_networks(self, networks: List[Dict[str, str]]) -> None:
        with self._lock:
            self._data["wifi_networks"] = [dict(network) for network in networks]
            self._save_locked()

    @property
    def wss_url(self) -> str:
        with self._lock:
            return self._data.get("wss_url", "")

    @wss_url.setter
    def wss_url(self, url: str) -> None:
        with self._lock:
            self._data["wss_url"] = url
            self._save_locked()

    @property
    def auth_token(self) -> str:
        with self._lock:
            return self._data.get("auth_token", "")

    @auth_token.setter
    def auth_token(self, token: str) -> None:
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        with self._lock:
            self._data["auth_token"] = token
            self._save_locked()

    @property
    def ap_ssid(self) -> str:
        with self._lock:
            return self._data.get("ap_ssid", "PinePi-Config")

    @property
    def ap_password(self) -> str:
        with self._lock:
            return self._data.get("ap_password", "12345678")
