import ipaddress
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional


class NetworkManager:
    """Thread-safe NetworkManager wrapper for station recovery and AP fallback."""

    AP_CONNECTION_NAME = "pinepi-ap"
    AP_GATEWAY_IP = "10.42.0.1"
    DEFAULT_WIFI_INTERFACE = "wlan0"
    STATION_READY_TIMEOUT = 45

    def __init__(self, config):
        self.config = config
        # Flask and the asyncio recovery task can both request mode changes.
        # Serialize all mutating nmcli operations so they cannot fight over the
        # single Wi-Fi radio.
        self._mutation_lock = threading.RLock()
        self._wifi_interface: Optional[str] = None
        self._cancel_event = threading.Event()
        self._ap_activated_at = 0.0

    def cancel_pending_operations(self) -> None:
        """Cooperatively stop command waits during service shutdown."""
        self._cancel_event.set()

    def _sleep(self, seconds: float) -> bool:
        """Sleep unless shutdown was requested; return False when cancelled."""
        return not self._cancel_event.wait(seconds)

    def _run(
        self, args, timeout: float = 5
    ) -> Optional[subprocess.CompletedProcess]:
        """Run a bounded, cancellable command without logging its arguments."""
        if self._cancel_event.is_set():
            return None
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, ValueError):
            return None

        deadline = time.monotonic() + timeout
        while True:
            if self._cancel_event.is_set() or time.monotonic() >= deadline:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                if self._cancel_event.is_set():
                    return None
                return subprocess.CompletedProcess(
                    args, process.returncode, stdout, stderr
                )
            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.5, max(0.01, deadline - time.monotonic()))
                )
                return subprocess.CompletedProcess(
                    args, process.returncode, stdout, stderr
                )
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _is_valid_ipv4(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
            return (
                ip.version == 4
                and not ip.is_loopback
                and not ip.is_link_local
                and not ip.is_unspecified
            )
        except ValueError:
            return False

    def get_wifi_interface(self) -> str:
        """Return the NetworkManager Wi-Fi device, preferring wlan0."""
        if self._wifi_interface:
            return self._wifi_interface

        result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]
        )
        candidates = []
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":", 1)
                if len(parts) == 2 and parts[0] and parts[1].lower() in (
                    "wifi",
                    "802-11-wireless",
                ):
                    candidates.append(parts[0])

        if self.DEFAULT_WIFI_INTERFACE in candidates:
            self._wifi_interface = self.DEFAULT_WIFI_INTERFACE
        elif candidates:
            self._wifi_interface = candidates[0]
        else:
            self._wifi_interface = self.DEFAULT_WIFI_INTERFACE
        return self._wifi_interface

    # ------------------------------------------------------------------
    # Read-only health snapshot
    # ------------------------------------------------------------------
    def get_connectivity_state(self, force_check: bool = False) -> str:
        """Return NetworkManager connectivity, optionally forcing a live check."""
        command = (
            ["nmcli", "networking", "connectivity", "check"]
            if force_check
            else ["nmcli", "-t", "-f", "CONNECTIVITY", "general"]
        )
        result = self._run(command)
        if not result or result.returncode != 0:
            return "unknown"
        state = result.stdout.strip().lower()
        return state if state in {"full", "limited", "portal", "none"} else "unknown"

    def is_online(self) -> bool:
        return self.get_connectivity_state() == "full"

    def get_wifi_device_state(self) -> str:
        interface = self.get_wifi_interface()
        result = self._run(
            ["nmcli", "-g", "GENERAL.STATE", "device", "show", interface]
        )
        if not result or result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"

    @staticmethod
    def _device_state_code(state: str) -> Optional[int]:
        token = (state or "").split(None, 1)[0]
        try:
            return int(token)
        except ValueError:
            return None

    def get_wifi_ipv4(self) -> Optional[str]:
        """Return the station IPv4 assigned to the managed Wi-Fi device."""
        interface = self.get_wifi_interface()
        result = self._run(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", interface]
        )
        lines = result.stdout.splitlines() if result and result.returncode == 0 else []

        # `ip` is a fallback for NetworkManager transitions where device data
        # briefly lags behind the kernel's address state.
        if not lines:
            result = self._run(
                ["ip", "-4", "-o", "addr", "show", "dev", interface, "scope", "global"]
            )
            if result and result.returncode == 0:
                lines = [
                    part
                    for line in result.stdout.splitlines()
                    for part in line.split()
                    if "/" in part
                ]

        for value in lines:
            address = value.strip().split("/", 1)[0]
            if address == self.AP_GATEWAY_IP:
                continue
            if self._is_valid_ipv4(address):
                return address
        return None

    def get_usable_lan_ip(self) -> Optional[str]:
        """Compatibility alias for the station interface's usable IPv4."""
        return self.get_wifi_ipv4()

    def has_usable_lan_ip(self) -> bool:
        return self.get_wifi_ipv4() is not None

    def has_default_route(self) -> bool:
        interface = self.get_wifi_interface()
        result = self._run(
            ["ip", "-4", "route", "show", "default", "dev", interface]
        )
        return bool(result and result.returncode == 0 and result.stdout.strip())

    def is_ap_active(self) -> bool:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"]
        )
        if not result or result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            name, _, connection_type = line.partition(":")
            if name == self.AP_CONNECTION_NAME and connection_type.lower() in (
                "wifi",
                "802-11-wireless",
            ):
                return True
        return False

    def get_ap_gateway_ip(self) -> str:
        result = self._run(
            [
                "nmcli",
                "-g",
                "IP4.ADDRESS",
                "connection",
                "show",
                self.AP_CONNECTION_NAME,
            ]
        )
        if result and result.returncode == 0:
            for value in result.stdout.splitlines():
                address = value.strip().split("/", 1)[0]
                if self._is_valid_ipv4(address):
                    return address
        return self.AP_GATEWAY_IP

    def get_current_wifi_ssid(self) -> Optional[str]:
        interface = self.get_wifi_interface()
        result = self._run(["iw", "dev", interface, "link"])
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    return line.split("SSID:", 1)[1].strip() or None

        # Fallback for images without `iw`: resolve the active NM profile to
        # its real SSID instead of assuming profile name == SSID.
        if self.is_ap_active():
            return None
        result = self._run(
            ["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", interface]
        )
        if result and result.returncode == 0:
            profile_uuid = result.stdout.strip()
            if profile_uuid and profile_uuid != "--":
                return self._profile_ssid(profile_uuid)
        return None

    def is_wifi_associated(self) -> bool:
        return self.get_current_wifi_ssid() is not None

    @staticmethod
    def _station_snapshot_ready(
        associated: bool,
        lan_ip: Optional[str],
        default_route: bool,
        device_state: str,
        ap_active: bool,
    ) -> bool:
        state_code = NetworkManager._device_state_code(device_state)
        device_activated = state_code == 100
        return bool(
            associated
            and lan_ip
            and default_route
            and device_activated
            and not ap_active
        )

    def _ssid_is_expected(
        self, current_ssid: Optional[str], expected_ssid: Optional[str] = None
    ) -> bool:
        if not current_ssid:
            return False
        if expected_ssid is not None:
            return current_ssid == expected_ssid
        configured_ssids = {
            network.get("ssid", "").strip()
            for network in getattr(self.config, "wifi_networks", [])
            if network.get("ssid", "").strip()
        }
        return not configured_ssids or current_ssid in configured_ssids

    def _station_ready(self, expected_ssid: Optional[str] = None) -> bool:
        current_ssid = self.get_current_wifi_ssid()
        if not self._ssid_is_expected(current_ssid, expected_ssid):
            return False
        return self._station_snapshot_ready(
            True,
            self.get_wifi_ipv4(),
            self.has_default_route(),
            self.get_wifi_device_state(),
            self.is_ap_active(),
        )

    def get_network_state(self) -> dict:
        """Return one lock-protected snapshot used by recovery and Page 3."""
        with self._mutation_lock:
            lan_ip = self.get_wifi_ipv4()
            current_ssid = self.get_current_wifi_ssid()
            associated = current_ssid is not None
            device_state = self.get_wifi_device_state()
            default_route = self.has_default_route()
            ap_active = self.is_ap_active()
            connectivity = self.get_connectivity_state()
            station_healthy = (
                self._ssid_is_expected(current_ssid)
                and self._station_snapshot_ready(
                    associated,
                    lan_ip,
                    default_route,
                    device_state,
                    ap_active,
                )
            )
            if ap_active and self._ap_activated_at == 0.0:
                self._ap_activated_at = time.monotonic()
            elif not ap_active:
                self._ap_activated_at = 0.0

            if station_healthy:
                mode = "lan"
            elif ap_active:
                mode = "ap"
            else:
                mode = "unavailable"

            return {
                "mode": mode,
                "lan_ip": lan_ip,
                "ap_ssid": self.config.ap_ssid,
                "ap_password": self.config.ap_password,
                "ap_ip": self.get_ap_gateway_ip(),
                "ap_active": ap_active,
                "ap_activated_at": self._ap_activated_at,
                "has_internet": connectivity == "full",
                "connectivity": connectivity,
                "wifi_interface": self.get_wifi_interface(),
                "wifi_ssid": current_ssid,
                "wifi_associated": associated,
                "wifi_device_state": device_state,
                "has_default_route": default_route,
                "station_healthy": station_healthy,
            }

    def get_active_connection(self) -> str:
        interface = self.get_wifi_interface()
        result = self._run(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface]
        )
        if not result or result.returncode != 0:
            return ""
        name = result.stdout.strip()
        return "" if name == "--" else name

    # ------------------------------------------------------------------
    # Station profile management and recovery
    # ------------------------------------------------------------------
    def _profile_ssid(self, profile_id: str) -> Optional[str]:
        result = self._run(
            [
                "nmcli",
                "--escape",
                "no",
                "-g",
                "802-11-wireless.ssid",
                "connection",
                "show",
                profile_id,
            ]
        )
        if not result or result.returncode != 0:
            return None
        ssid = result.stdout.strip()
        return None if not ssid or ssid == "--" else ssid

    def _find_wifi_profile(self, ssid: str) -> Optional[str]:
        """Find a Wi-Fi profile by its SSID and return its UUID."""
        result = self._run(
            ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"]
        )
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                profile_uuid, _, connection_type = line.partition(":")
                if not profile_uuid or connection_type.lower() not in (
                    "wifi",
                    "802-11-wireless",
                ):
                    continue
                if self._profile_ssid(profile_uuid) == ssid:
                    return profile_uuid

        # Compatibility fallback for profiles whose ID is the SSID.
        result = self._run(["nmcli", "connection", "show", ssid])
        if result and result.returncode == 0 and self._profile_ssid(ssid) == ssid:
            return ssid
        return None

    def _configure_station_profile(
        self,
        profile_id: str,
        password: str = "",
        priority: Optional[int] = None,
    ) -> bool:
        args = [
            "nmcli",
            "connection",
            "modify",
            profile_id,
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-retries",
            "0",
            "802-11-wireless.powersave",
            "2",
        ]
        if priority is not None:
            args.extend(["connection.autoconnect-priority", str(priority)])
        result = self._run(args)
        ok = bool(result and result.returncode == 0)
        if not ok:
            print(f"[NetworkManager] Could not tune station profile for {self._profile_ssid(profile_id) or 'unknown SSID'}")

        if password:
            result = self._run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    profile_id,
                    "802-11-wireless-security.psk",
                    password,
                ]
            )
            if not result or result.returncode != 0:
                print("[NetworkManager] Could not update the station profile password")
                ok = False
        return ok

    def configure_wifi_reliability(self, networks: List[Dict[str, str]]) -> None:
        """Persist settings that let NetworkManager reconnect without the app."""
        with self._mutation_lock:
            interface = self.get_wifi_interface()
            result = self._run(
                ["nmcli", "device", "set", interface, "managed", "yes", "autoconnect", "yes"]
            )
            if not result or result.returncode != 0:
                print(f"[NetworkManager] WARN: could not enable autoconnect on {interface}")

            self._disable_runtime_power_save_locked()
            for index, network in enumerate(networks):
                ssid = network.get("ssid", "").strip()
                if not ssid:
                    continue
                profile_id = self._find_wifi_profile(ssid)
                if profile_id:
                    self._configure_station_profile(
                        profile_id,
                        network.get("password", "").strip(),
                        priority=max(1, 100 - index),
                    )
            print(f"[NetworkManager] Wi-Fi reliability settings applied on {interface}")

    def _disable_runtime_power_save_locked(self) -> bool:
        interface = self.get_wifi_interface()
        result = self._run(
            ["iw", "dev", interface, "set", "power_save", "off"]
        )
        if result and result.returncode == 0:
            print(f"[NetworkManager] Wi-Fi power saving disabled on {interface}")
            return True
        print(f"[NetworkManager] WARN: could not disable Wi-Fi power saving on {interface}")
        return False

    def _wait_for_station_ready(
        self,
        expected_ssid: str,
        timeout: float = STATION_READY_TIMEOUT,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._cancel_event.is_set():
            if self._station_ready(expected_ssid):
                return True
            if not self._sleep(2):
                return False
        return False

    def disconnect_wifi(self) -> bool:
        with self._mutation_lock:
            interface = self.get_wifi_interface()
            result = self._run(
                ["nmcli", "--wait", "10", "device", "disconnect", interface],
                timeout=15,
            )
            if result and result.returncode == 0:
                print(f"[NetworkManager] Disconnected {interface}")
                return True
            print(f"[NetworkManager] Failed to disconnect {interface}")
            return False

    def _connect_wifi_locked(
        self, ssid: str, password: str, force_reactivate: bool = False
    ) -> bool:
        ssid = (ssid or "").strip()
        password = (password or "").strip()
        if not ssid:
            return False

        interface = self.get_wifi_interface()
        if self.is_ap_active():
            self._stop_ap_locked()
            if not self._sleep(1):
                return False

        current_ssid = self.get_current_wifi_ssid()
        if not force_reactivate and self._station_ready(ssid):
            profile_id = self._find_wifi_profile(ssid)
            if profile_id:
                self._configure_station_profile(profile_id, password)
            return True

        profile_id = self._find_wifi_profile(ssid)
        if profile_id:
            self._configure_station_profile(profile_id, password)
            # Explicitly cycle an unhealthy activation. `connection up` alone
            # can report success while retaining a stale DHCP lease.
            if current_ssid == ssid:
                down_result = self._run(
                    ["nmcli", "--wait", "10", "connection", "down", profile_id],
                    timeout=15,
                )
                if not down_result or down_result.returncode != 0:
                    print(
                        "[NetworkManager] Profile deactivation failed; "
                        "disconnecting the Wi-Fi device"
                    )
                    self._run(
                        [
                            "nmcli",
                            "--wait",
                            "10",
                            "device",
                            "disconnect",
                            interface,
                        ],
                        timeout=15,
                    )
                if not self._sleep(1):
                    return False

            print(f"[NetworkManager] Activating configured Wi-Fi: {ssid}")
            result = self._run(
                [
                    "nmcli",
                    "--wait",
                    "35",
                    "connection",
                    "up",
                    profile_id,
                    "ifname",
                    interface,
                ],
                timeout=40,
            )
        else:
            print(f"[NetworkManager] Creating Wi-Fi profile: {ssid}")
            self._run(
                ["nmcli", "device", "wifi", "rescan", "ifname", interface],
                timeout=20,
            )
            args = [
                "nmcli",
                "--wait",
                "35",
                "device",
                "wifi",
                "connect",
                ssid,
                "ifname",
                interface,
            ]
            if password:
                args.extend(["password", password])
            result = self._run(args, timeout=40)
            profile_id = self._find_wifi_profile(ssid)
            if profile_id:
                self._configure_station_profile(profile_id, password)

        if result and result.returncode == 0:
            print(f"[NetworkManager] Association command completed for {ssid}; waiting for DHCP")
        else:
            detail = ""
            if result and result.stderr:
                detail = result.stderr.strip().splitlines()[-1]
            print(f"[NetworkManager] Activation failed for {ssid}{': ' + detail if detail else ''}")

        if self._wait_for_station_ready(ssid):
            print(f"[NetworkManager] Wi-Fi ready: {ssid} ({self.get_wifi_ipv4()})")
            self._disable_runtime_power_save_locked()
            return True

        print(f"[NetworkManager] Wi-Fi did not obtain IPv4/default route: {ssid}")
        return False

    def connect_wifi(self, ssid: str, password: str) -> bool:
        with self._mutation_lock:
            return self._connect_wifi_locked(ssid, password)

    def _reset_wifi_radio_locked(self) -> bool:
        """Escalation for a wedged driver/NM state after ordinary retries fail."""
        print("[NetworkManager] Escalating recovery: cycling the Wi-Fi radio")
        self._ap_activated_at = 0.0
        off = self._run(["nmcli", "radio", "wifi", "off"], timeout=10)
        if not self._sleep(2):
            return False
        on = self._run(["nmcli", "radio", "wifi", "on"], timeout=10)
        if not self._sleep(3):
            return False
        self._disable_runtime_power_save_locked()
        return bool(
            off
            and off.returncode == 0
            and on
            and on.returncode == 0
        )

    def reset_wifi_radio(self) -> bool:
        with self._mutation_lock:
            return self._reset_wifi_radio_locked()

    def _is_ssid_in_networks(self, ssid: Optional[str], networks: List[Dict[str, str]]) -> bool:
        return bool(
            ssid
            and any(network.get("ssid", "").strip() == ssid for network in networks)
        )

    def ensure_best_wifi(
        self,
        networks: List[Dict[str, str]],
        aggressive: bool = False,
        force: bool = False,
    ) -> bool:
        """Recover station mode, trying every configured SSID with full DHCP waits.

        `aggressive` cycles the radio and should only be used after several
        ordinary failures. `force` reactivates a station that still looks
        associated but has had no usable Internet/cloud path for a long time.
        """
        with self._mutation_lock:
            configured = [
                {
                    "ssid": network.get("ssid", "").strip(),
                    "password": network.get("password", "").strip(),
                }
                for network in networks
                if network.get("ssid", "").strip()
            ]
            if not configured:
                return False

            was_ap_active = self.is_ap_active()
            current_ssid = self.get_current_wifi_ssid()
            already_healthy = bool(
                current_ssid
                and self._is_ssid_in_networks(current_ssid, configured)
                and self._station_ready(current_ssid)
            )
            if already_healthy and not force and not aggressive:
                profile_id = self._find_wifi_profile(current_ssid)
                if profile_id:
                    self._configure_station_profile(profile_id)
                return True

            if was_ap_active:
                self._stop_ap_locked()
                if not self._sleep(1):
                    return False

            if aggressive:
                self._reset_wifi_radio_locked()
            else:
                self._disable_runtime_power_save_locked()

            # Try the current configured SSID first to avoid unnecessary
            # roaming, then every remaining network in configured priority.
            ordered = []
            if current_ssid:
                ordered.extend(
                    network for network in configured if network["ssid"] == current_ssid
                )
            ordered.extend(
                network for network in configured if network["ssid"] != current_ssid
            )

            for network in ordered:
                if self._connect_wifi_locked(
                    network["ssid"],
                    network["password"],
                    force_reactivate=(force or aggressive)
                    and network["ssid"] == current_ssid,
                ):
                    return True

            if was_ap_active:
                print("[NetworkManager] Station recovery failed; restoring AP mode")
                self._create_ap_locked()
            return False

    def apply_wifi_configuration(self, networks: List[Dict[str, str]]) -> bool:
        """Apply web-submitted networks as one serialized radio transaction."""
        with self._mutation_lock:
            configured = [network for network in networks if network.get("ssid", "").strip()]
            if not configured:
                return False
            was_ap_active = self.is_ap_active()
            if was_ap_active:
                self._stop_ap_locked()
                if not self._sleep(1):
                    return False
            self.configure_wifi_reliability(configured)
            for network in configured:
                if self._connect_wifi_locked(
                    network.get("ssid", ""), network.get("password", "")
                ):
                    return True
            if was_ap_active:
                self._create_ap_locked()
            return False

    # ------------------------------------------------------------------
    # AP mode
    # ------------------------------------------------------------------
    def _configure_captive_portal_dns(self) -> bool:
        ap_ip = self.AP_GATEWAY_IP
        try:
            result = self._run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    self.AP_CONNECTION_NAME,
                    "ipv4.dns",
                    ap_ip,
                    "+ipv4.dns-options",
                    "ndots:5",
                ]
            )
            if not result or result.returncode != 0:
                detail = result.stderr.strip() if result and result.stderr else "unknown error"
                print(
                    f"[NetworkManager] Failed to set captive DNS options: {detail}"
                )
                return False
            conf_dir = "/etc/NetworkManager/dnsmasq-shared.d"
            conf_path = os.path.join(conf_dir, "pinepi-captive.conf")
            os.makedirs(conf_dir, exist_ok=True)
            with open(conf_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"# PinePi captive portal DNS redirect\naddress=/#/{ap_ip}\nno-resolv\n"
                )
            print(f"[NetworkManager] Captive portal DNS configured (all -> {ap_ip})")
            return True
        except OSError as exc:
            print(f"[NetworkManager] Failed to configure captive portal DNS: {exc}")
            return False

    def _remove_captive_portal_dns(self) -> None:
        conf_path = "/etc/NetworkManager/dnsmasq-shared.d/pinepi-captive.conf"
        try:
            os.remove(conf_path)
            print("[NetworkManager] Captive portal DNS config removed")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[NetworkManager] Failed to remove captive portal DNS: {exc}")

    def _create_ap_locked(self) -> bool:
        ssid = self.config.ap_ssid
        password = self.config.ap_password
        interface = self.get_wifi_interface()
        print(f"[NetworkManager] Starting AP mode: SSID={ssid}")

        result = self._run(
            ["nmcli", "connection", "show", self.AP_CONNECTION_NAME]
        )
        if result and result.returncode == 0:
            self._run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    self.AP_CONNECTION_NAME,
                    "connection.autoconnect",
                    "no",
                    "802-11-wireless.ssid",
                    ssid,
                    "802-11-wireless.powersave",
                    "2",
                ]
            )
            if password:
                self._run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        self.AP_CONNECTION_NAME,
                        "802-11-wireless-security.psk",
                        password,
                    ]
                )
            result = self._run(
                [
                    "nmcli",
                    "--wait",
                    "20",
                    "connection",
                    "up",
                    self.AP_CONNECTION_NAME,
                    "ifname",
                    interface,
                ],
                timeout=25,
            )
        else:
            result = self._run(
                [
                    "nmcli",
                    "--wait",
                    "20",
                    "device",
                    "wifi",
                    "hotspot",
                    "ifname",
                    interface,
                    "con-name",
                    self.AP_CONNECTION_NAME,
                    "ssid",
                    ssid,
                    "password",
                    password,
                ],
                timeout=25,
            )
            if result and result.returncode == 0:
                self._run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        self.AP_CONNECTION_NAME,
                        "connection.autoconnect",
                        "no",
                        "802-11-wireless.powersave",
                        "2",
                    ]
                )

        if result and result.returncode == 0:
            self._ap_activated_at = time.monotonic()
            self._configure_captive_portal_dns()
            print(f"[NetworkManager] AP mode enabled: {ssid}")
            return True

        detail = result.stderr.strip() if result and result.stderr else "unknown error"
        print(f"[NetworkManager] AP mode failed: {detail}")
        return False

    def create_ap(self) -> bool:
        with self._mutation_lock:
            if self.is_ap_active():
                return True
            return self._create_ap_locked()

    def start_ap_if_unavailable(self) -> bool:
        """Start AP only if station health is still absent under the lock."""
        with self._mutation_lock:
            if self._station_ready():
                print("[NetworkManager] AP start skipped; station already recovered")
                return False
            if self.is_ap_active():
                return True
            return self._create_ap_locked()

    def stop_ap_if_station_available(self) -> bool:
        """Stop AP only when station link data still proves LAN availability."""
        with self._mutation_lock:
            current_ssid = self.get_current_wifi_ssid()
            station_available = (
                self._ssid_is_expected(current_ssid)
                and self._station_snapshot_ready(
                    current_ssid is not None,
                    self.get_wifi_ipv4(),
                    self.has_default_route(),
                    self.get_wifi_device_state(),
                    False,
                )
            )
            if not station_available:
                return False
            return self._stop_ap_locked()

    def _stop_ap_locked(self) -> bool:
        if not self.is_ap_active():
            self._ap_activated_at = 0.0
            self._remove_captive_portal_dns()
            return True
        print("[NetworkManager] Stopping AP mode")
        result = self._run(
            [
                "nmcli",
                "--wait",
                "10",
                "connection",
                "down",
                self.AP_CONNECTION_NAME,
            ],
            timeout=15,
        )
        if result and result.returncode == 0:
            self._ap_activated_at = 0.0
            self._remove_captive_portal_dns()
            print("[NetworkManager] AP mode stopped")
            return True
        detail = result.stderr.strip() if result and result.stderr else "unknown error"
        print(f"[NetworkManager] AP stop failed: {detail}")
        return False

    def stop_ap(self) -> bool:
        with self._mutation_lock:
            return self._stop_ap_locked()
