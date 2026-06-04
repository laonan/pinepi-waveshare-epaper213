import json
import os
from typing import List, Dict, Any

DEFAULT_CONFIG = {
    "wifi_networks": [],
    "wss_url": "wss://example.com/ws",
    "auth_token": "",
    "ap_ssid": "PinePi-Config",
    "ap_password": "12345678"
}

class Config:
    """Persistent configuration file manager (/etc/pinepi-waveshare-epaper213/config.json)"""

    def __init__(self, path: str = "/etc/pinepi-waveshare-epaper213/config.json"):
        self.path = path
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return {**DEFAULT_CONFIG, **json.load(f)}
            except Exception as e:
                print(f"[Config] Failed to load {self.path}: {e}")
        return dict(DEFAULT_CONFIG)

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] Failed to save: {e}")

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    @property
    def wifi_networks(self) -> List[Dict[str, str]]:
        return self._data.get("wifi_networks", [])

    @wifi_networks.setter
    def wifi_networks(self, networks: List[Dict[str, str]]) -> None:
        self._data["wifi_networks"] = networks
        self.save()

    @property
    def wss_url(self) -> str:
        return self._data.get("wss_url", "")

    @wss_url.setter
    def wss_url(self, url: str) -> None:
        self._data["wss_url"] = url
        self.save()

    @property
    def auth_token(self) -> str:
        return self._data.get("auth_token", "")

    @auth_token.setter
    def auth_token(self, token: str) -> None:
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        self._data["auth_token"] = token
        self.save()

    @property
    def ap_ssid(self) -> str:
        return self._data.get("ap_ssid", "PinePi-Config")

    @property
    def ap_password(self) -> str:
        return self._data.get("ap_password", "12345678")
