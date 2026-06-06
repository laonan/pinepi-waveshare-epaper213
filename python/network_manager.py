import re
import socket
import subprocess
import time
from typing import List, Dict, Optional


class NetworkManager:
    """nmcli wrapper for Station / AP mode switching with unified network state"""

    AP_CONNECTION_NAME = "pinepi-ap"
    AP_GATEWAY_IP = "10.42.0.1"  # Common nmcli AP gateway

    def __init__(self, config):
        self.config = config

    def is_online(self) -> bool:
        """Check if device has full internet connectivity.
        Parses nmcli output - only 'full' counts as online."""
        try:
            result = subprocess.run(
                ["nmcli", "networking", "connectivity", "check"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                return False
            state = result.stdout.strip().lower()
            # 'full' means internet is reachable
            return state == "full"
        except Exception:
            return False

    def get_usable_lan_ip(self) -> Optional[str]:
        """Get a usable LAN IPv4 address (not loopback, not link-local 169.254.x.x, not AP IP).
        Returns None if no usable IP exists."""
        try:
            # Get all IP addresses using hostname -I
            result = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                return None

            ips = result.stdout.strip().split()
            ap_ip = self.get_ap_gateway_ip()

            for ip in ips:
                # Skip loopback
                if ip.startswith("127."):
                    continue
                # Skip link-local (APIPA)
                if ip.startswith("169.254."):
                    continue
                # Skip AP's own IP (e.g., 10.42.0.1 when in AP mode)
                # This prevents the AP from being shut down incorrectly
                if ip == ap_ip:
                    continue
                # Skip IPv6 for now (simple check for colon)
                if ":" in ip:
                    continue
                # Valid IPv4 address
                if self._is_valid_ipv4(ip):
                    return ip
            return None
        except Exception as e:
            print(f"[NetworkManager] Failed to get usable LAN IP: {e}")
            return None

    @staticmethod
    def _is_valid_ipv4(ip: str) -> bool:
        """Validate IPv4 address format."""
        try:
            parts = ip.split(".")
            if len(parts) != 4:
                return False
            for part in parts:
                if not part.isdigit():
                    return False
                num = int(part)
                if num < 0 or num > 255:
                    return False
            return True
        except Exception:
            return False

    def has_usable_lan_ip(self) -> bool:
        """Check if device has a usable LAN IP address."""
        return self.get_usable_lan_ip() is not None

    def is_ap_active(self) -> bool:
        """Check if the AP connection is actually active."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                return False

            for line in result.stdout.strip().split("\n"):
                parts = line.strip().split(":")
                if len(parts) >= 2:
                    name = parts[0]
                    conn_type = parts[1]
                    if name == self.AP_CONNECTION_NAME and "wifi" in conn_type.lower():
                        return True
            return False
        except Exception:
            return False

    def get_ap_gateway_ip(self) -> str:
        """Get the AP gateway IP. Detects from active connection or returns default."""
        try:
            # Try to get IP from the AP interface
            result = subprocess.run(
                ["nmcli", "-t", "-f", "IP4.ADDRESS", "connection", "show", self.AP_CONNECTION_NAME],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse IP4.ADDRESS like '10.42.0.1/24'
                addr_line = result.stdout.strip()
                if addr_line:
                    ip_with_prefix = addr_line.split("/")[0]
                    if self._is_valid_ipv4(ip_with_prefix):
                        return ip_with_prefix
        except Exception:
            pass
        return self.AP_GATEWAY_IP

    def get_network_state(self) -> dict:
        """Get unified network state for Page 3 rendering.

        Returns:
            dict with keys:
            - mode: "lan" | "ap" | "unavailable"
            - lan_ip: str | None - usable LAN IP if available
            - ap_ssid: str - AP SSID from config
            - ap_password: str - AP password from config
            - ap_ip: str - AP gateway IP
            - ap_active: bool - whether AP is actually active
            - has_internet: bool - full internet connectivity
        """
        lan_ip = self.get_usable_lan_ip()
        ap_active = self.is_ap_active()
        has_internet = self.is_online()

        if lan_ip:
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
            "has_internet": has_internet,
        }

    def get_active_connection(self) -> str:
        """Return SSID of current active connection, empty if none"""
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
                stderr=subprocess.DEVNULL, text=True, timeout=5
            )
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            return lines[0] if lines else ""
        except Exception:
            return ""

    def get_current_wifi_ssid(self) -> Optional[str]:
        """Get currently connected Wi-Fi SSID"""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('yes:'):
                        return line.split(':')[1] if ':' in line else None
            return None
        except Exception:
            return None

    def disconnect_wifi(self) -> bool:
        """Disconnect from current Wi-Fi"""
        try:
            subprocess.run(
                ["nmcli", "device", "disconnect", "wlan0"],
                check=True, stderr=subprocess.DEVNULL, timeout=10
            )
            print("[NetworkManager] Disconnected from current Wi-Fi")
            return True
        except Exception:
            return False

    def connect_wifi(self, ssid: str, password: str) -> bool:
        """Connect to specified Wi-Fi, update password if connection exists.
        Disconnects from current Wi-Fi first to ensure clean switch."""
        # Check if already connected to requested SSID
        current_ssid = self.get_current_wifi_ssid()
        if current_ssid == ssid:
            print(f"[NetworkManager] Already connected to {ssid}")
            return True

        # Disconnect from current Wi-Fi first to ensure clean switch
        if current_ssid:
            self.disconnect_wifi()
            time.sleep(1)  # Wait for disconnect to complete

        # Check if connection profile exists
        try:
            result = subprocess.run(
                ["nmcli", "connection", "show", ssid],
                capture_output=True,
                timeout=5
            )
            profile_exists = result.returncode == 0
        except Exception:
            profile_exists = False

        if profile_exists:
            # Update password in existing profile
            try:
                subprocess.run(
                    ["nmcli", "connection", "modify", ssid, "wifi-sec.psk", password],
                    check=True, stderr=subprocess.DEVNULL, timeout=5
                )
                print(f"[NetworkManager] Updated password for existing connection: {ssid}")
            except Exception as e:
                print(f"[NetworkManager] Failed to update password for {ssid}: {e}")

            # Activate the connection
            try:
                subprocess.run(
                    ["nmcli", "connection", "up", ssid],
                    check=True, stderr=subprocess.DEVNULL, timeout=15
                )
                return True
            except Exception as e:
                print(f"[NetworkManager] Failed to activate {ssid}: {e}")

        # Create new connection or re-scan and connect
        try:
            subprocess.run(
                ["nmcli", "device", "wifi", "connect", ssid, "password", password],
                check=True, stderr=subprocess.DEVNULL, timeout=30
            )
            return True
        except Exception as e:
            print(f"[NetworkManager] Failed to connect {ssid}: {e}")
            return False

    def create_ap(self) -> bool:
        """Create AP hotspot (requires Wi-Fi AP mode support).
        Captures stderr for detailed error logging."""
        ssid = self.config.ap_ssid
        password = self.config.ap_password
        print(f"[NetworkManager] Starting AP mode: SSID={ssid}")
        try:
            # Check if we already have the connection profile
            result = subprocess.run(
                ["nmcli", "connection", "show", self.AP_CONNECTION_NAME],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Connection exists, just bring it up
                up_result = subprocess.run(
                    ["nmcli", "connection", "up", self.AP_CONNECTION_NAME],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                if up_result.returncode == 0:
                    print(f"[NetworkManager] AP mode enabled (existing profile): {ssid}")
                    return True
                else:
                    print(f"[NetworkManager] Failed to start existing AP: {up_result.stderr}")
                    return False

            # Create new AP using device wifi hotspot
            result = subprocess.run(
                [
                    "nmcli", "device", "wifi", "hotspot",
                    "ifname", "wlan0",
                    "con-name", self.AP_CONNECTION_NAME,
                    "ssid", ssid,
                    "password", password,
                ],
                capture_output=True,
                text=True,
                timeout=15
            )
            if result.returncode == 0:
                print(f"[NetworkManager] AP mode enabled: {ssid}")
                return True
            else:
                print(f"[NetworkManager] AP mode failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"[NetworkManager] AP mode failed with exception: {e}")
            return False

    def stop_ap(self) -> bool:
        """Stop AP hotspot"""
        print("[NetworkManager] Stopping AP mode")
        try:
            result = subprocess.run(
                ["nmcli", "connection", "down", self.AP_CONNECTION_NAME],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                print("[NetworkManager] AP mode stopped")
                return True
            else:
                print(f"[NetworkManager] AP stop failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"[NetworkManager] AP stop failed with exception: {e}")
            return False

    def is_wifi_associated(self) -> bool:
        """Check if Wi-Fi is associated with any SSID (netplan-compatible check using iw)"""
        try:
            result = subprocess.run(
                ["iw", "dev", "wlan0", "link"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0 and "SSID:" in result.stdout
        except Exception:
            return False

    def _is_ssid_in_networks(self, ssid: str, networks: List[Dict[str, str]]) -> bool:
        """Check if given SSID is in the configured networks list"""
        if not ssid:
            return False
        for net in networks:
            if net.get("ssid", "").strip() == ssid:
                return True
        return False

    def ensure_best_wifi(self, networks: List[Dict[str, str]]) -> bool:
        """Try connecting to Wi-Fi networks in the configuration list sequentially.
        Accepts connection if a usable LAN IP is obtained (not requiring full internet).
        
        NETPLAN COMPATIBILITY: Avoids nmcli disconnections on working associations.
        Uses iw for read-only checks, only uses nmcli when truly needed.
        
        CRITICAL: Automatically switches between configured networks based on availability.
        If current network has no IP, will try others in order."""
        skipped_ssid = None  # Track network to skip (one that failed)
        
        # First, check if already associated with Wi-Fi (lightweight iw check)
        if self.is_wifi_associated():
            current_ssid = self.get_current_wifi_ssid()
            
            # Check if currently associated network is in our config list
            if not self._is_ssid_in_networks(current_ssid, networks):
                print(f"[NetworkManager] Associated with '{current_ssid}' but not in config list - disconnecting")
                skipped_ssid = current_ssid
                self.disconnect_wifi()
                time.sleep(2)
                # Fall through to connection logic below
            else:
                # Check if we have an IP
                lan_ip = self.get_usable_lan_ip()
                if lan_ip:
                    print(f"[NetworkManager] Wi-Fi associated ({current_ssid}), IP: {lan_ip}")
                    return True
                
                # Associated to configured network but no IP - try next configured network
                print(f"[NetworkManager] Associated to {current_ssid} but no IP - trying next configured network...")
                skipped_ssid = current_ssid
                self.disconnect_wifi()
                time.sleep(2)
                # Fall through to try other networks in order

        # Not associated (or we just disconnected) - try each configured network in order
        print("[NetworkManager] Wi-Fi not associated, attempting connection...")
        for net in networks:
            ssid = net.get("ssid", "").strip()
            pwd = net.get("password", "").strip()
            if not ssid:
                continue
            # Skip the network we just disconnected from (it's not working)
            if ssid == skipped_ssid:
                print(f"[NetworkManager] Skipping {ssid} (just disconnected, not working)")
                continue
            print(f"[NetworkManager] Trying {ssid}...")
            if self.connect_wifi(ssid, pwd):
                time.sleep(2)
                lan_ip = self.get_usable_lan_ip()
                if lan_ip:
                    print(f"[NetworkManager] Connected to {ssid} (LAN IP: {lan_ip})")
                    return True
                print(f"[NetworkManager] Connected to {ssid} but no usable LAN IP - will try next")
                # Disconnect and try next network
                self.disconnect_wifi()
                time.sleep(1)
        return False
