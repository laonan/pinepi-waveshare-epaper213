import datetime
import os
import socket
import subprocess
import textwrap
import time
from typing import Optional

import psutil
import qrcode
from PIL import Image, ImageDraw, ImageFont


class Renderer:
    """Pillow rendering engine: handles Page 2 (System Monitor) and Page 3 (Config QR Code)"""

    # Canvas logical size (landscape). Final display is 122×250 portrait after rotation.
    CANVAS_W = 250
    CANVAS_H = 122

    def __init__(self, config):
        self.config = config
        # Try to load monospace font for neater numbers; fall back to default if unavailable
        self.font = self._load_font()
        self.font_small = self.font
        try:
            self.font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        except Exception:
            pass

    def _load_font(self) -> ImageFont.ImageFont:
        candidates = [
            "/opt/pinepi-waveshare-epaper213/fonts/MSYH.ttf",  # installed location
            os.path.join(os.path.dirname(__file__), "..", "fonts", "MSYH.ttf"),  # local dev: python/../fonts/
            os.path.join(os.getcwd(), "fonts", "MSYH.ttf"),  # cwd/fonts/
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts", "MSYH.ttf"),  # project_root/fonts/
            os.path.join(os.getcwd(), "MSYH.ttf"),  # project_root/ (legacy)
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return ImageFont.truetype(path, 12)
        return ImageFont.load_default()

    def _wrap_text(self, text: str, max_units: int):
        lines = []
        current = ""
        units = 0
        for ch in text:
            ch_units = 2 if ord(ch) > 127 else 1
            if ch == "\n":
                lines.append(current)
                current = ""
                units = 0
                continue
            if current and units + ch_units > max_units:
                lines.append(current)
                current = ch
                units = ch_units
            else:
                current += ch
                units += ch_units
        if current:
            lines.append(current)
        return lines

    def _new_canvas(self) -> Image.Image:
        """Create a 1-bit canvas with white background"""
        return Image.new("1", (self.CANVAS_W, self.CANVAS_H), 255)

    def _to_display_bytes(self, img: Image.Image) -> bytes:
        """Rotate 270° and export as 4000 bytes for display"""
        # If image is already landscape (250×122), rotate to portrait (122×250)
        # If image is already portrait (122×250), keep as-is (server sends landscape)
        if img.size == (250, 122):
            img = img.rotate(270, expand=True)
        elif img.size != (122, 250):
            img = img.resize((122, 250))
        return img.tobytes()

    def _draw_footer(self, draw: ImageDraw.ImageDraw, text: str):
        """Draw centered footer text at bottom"""
        bbox = draw.textbbox((0, 0), text, font=self.font_small)
        tw = bbox[2] - bbox[0]
        x = (self.CANVAS_W - tw) // 2
        draw.text((x, 110), text, fill=0, font=self.font_small)

    # ------------------------------------------------------------------
    # Page 1: Cloud Board (default shows "No message" or offline hint)
    # ------------------------------------------------------------------
    def render_page1(self, is_offline: bool = False) -> bytes:
        img = self._new_canvas()
        draw = ImageDraw.Draw(img)

        draw.text((5, 2), "Cloud Board", fill=0, font=self.font)
        draw.line((5, 16, 245, 16), fill=0, width=1)

        if is_offline:
            draw.text((5, 35), "OFFLINE", fill=0, font=self.font)
            draw.text((5, 52), "AP Mode Active", fill=0, font=self.font)
            draw.text((5, 69), "Check Page 3", fill=0, font=self.font)
        else:
            draw.text((5, 40), "No message", fill=0, font=self.font)

        self._draw_footer(draw, "Page: 1/3 (Cloud)")
        return self._to_display_bytes(img)

    def render_page1_message(self, title: str, content: str) -> bytes:
        img = self._new_canvas()
        draw = ImageDraw.Draw(img)

        title = (title or "Message").strip()
        content = (content or "").strip()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

        # Title: limit to ~10 Chinese chars width (20 units), add ellipsis if truncated
        title_display = ""
        title_units = 0
        title_truncated = False
        for ch in title:
            ch_units = 2 if ord(ch) > 127 else 1
            # Reserve 2 units for ".." if we need to truncate
            if title_units + ch_units > 18:
                title_truncated = True
                break
            title_display += ch
            title_units += ch_units
        if title_truncated:
            title_display += ".."
        draw.text((5, 2), title_display, fill=0, font=self.font)

        # Right-align timestamp
        ts_bbox = draw.textbbox((0, 0), ts, font=self.font_small)
        ts_width = ts_bbox[2] - ts_bbox[0]
        ts_x = self.CANVAS_W - ts_width - 5
        draw.text((ts_x, 2), ts, fill=0, font=self.font_small)
        draw.line((5, 16, 245, 16), fill=0, width=1)

        y = 22
        for line in self._wrap_text(content, max_units=40)[:6]:
            draw.text((5, y), line, fill=0, font=self.font)
            y += 14

        self._draw_footer(draw, "Page: 1/3 (Cloud)")
        return self._to_display_bytes(img)

    # ------------------------------------------------------------------
    # Page 2: System Monitor
    # ------------------------------------------------------------------
    def render_page2(self) -> bytes:
        img = self._new_canvas()
        draw = ImageDraw.Draw(img)

        online = self._is_online()
        ip = self._get_lan_ip()
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        uptime_sec = time.time() - psutil.boot_time()
        uptime = self._fmt_uptime(uptime_sec)

        draw.text((5, 2), "PinePi System", fill=0, font=self.font)
        draw.line((5, 16, 245, 16), fill=0, width=1)

        status = "ONLINE" if online else "OFFLINE"
        ip_display = ip if ip and ip != "0.0.0.0" else "-"
        refresh_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        draw.text((5, 22), f"Net: {status}", fill=0, font=self.font)
        draw.text((5, 36), f"IP:  {ip_display}", fill=0, font=self.font)
        draw.text((5, 50), f"CPU: {cpu:.0f}%", fill=0, font=self.font)
        draw.text((5, 64), f"Mem: {mem.percent:.0f}%", fill=0, font=self.font)
        draw.text((5, 78), f"Up:  {uptime}", fill=0, font=self.font)
        draw.text((5, 92), f"Refreshed: {refresh_time}", fill=0, font=self.font)

        self._draw_footer(draw, "Page: 2/3 (Local)")
        return self._to_display_bytes(img)

    # ------------------------------------------------------------------
    # Page 3: Config QR Code
    # ------------------------------------------------------------------
    def render_page3(self) -> bytes:
        """Legacy method: renders Page 3 using internal network checks.
        Prefer render_page3_with_state() for consistent behavior."""
        img = self._new_canvas()
        draw = ImageDraw.Draw(img)

        # Use usable LAN IP check instead of online check
        lan_ip = self._get_usable_lan_ip()

        if lan_ip:
            url = f"http://{lan_ip}:8080"
            label = "Scan to Config"
            print(f"[Renderer] Page 3: LAN mode, URL={url}")
        else:
            ssid = self.config.ap_ssid
            password = self.config.ap_password
            url = f"WIFI:T:WPA;S:{ssid};P:{password};;"
            label = "Scan for Wi-Fi"
            print(f"[Renderer] Page 3: AP mode, SSID={ssid}")

        # Generate QR code as PIL image (white background, black code)
        qr = qrcode.make(url, box_size=2, border=1)
        qr = qr.convert("1")  # 1-bit: black=0, white=255

        # Paste QR code centered on canvas
        qw, qh = qr.size
        px = (self.CANVAS_W - qw) // 2
        py = (self.CANVAS_H - qh) // 2 - 8
        img.paste(qr, (px, py))

        draw.text((5, 2), label, fill=0, font=self.font)
        # Show URL and hint in both LAN and AP modes
        hint = "Please visit the URL below for configuration."
        hint_w = self.font_small.getbbox(hint)[2]
        # In LAN mode, show the device IP; in AP mode, show AP gateway IP
        if lan_ip:
            display_url = url  # http://192.168.x.x:8080
        else:
            display_url = "http://10.42.0.1:8080"
        url_w = self.font_small.getbbox(display_url)[2]
        hint_x = max(0, (self.CANVAS_W - hint_w) // 2)
        url_x = max(0, (self.CANVAS_W - url_w) // 2)
        hint_y = py + qh + 2
        url_y = hint_y + 12
        draw.text((hint_x, hint_y), hint, fill=0, font=self.font_small)
        draw.text((url_x, url_y), display_url, fill=0, font=self.font_small)
        self._draw_footer(draw, "Page: 3/3 (Config)")
        return self._to_display_bytes(img)

    def render_page3_with_state(self, state: dict) -> bytes:
        """Render Page 3 using unified network state from NetworkManager.

        Args:
            state: dict from NetworkManager.get_network_state() with keys:
                - mode: "lan" | "ap" | "unavailable"
                - lan_ip: str | None
                - ap_ssid: str
                - ap_password: str
                - ap_ip: str
                - ap_active: bool
                - has_internet: bool

        Returns:
            bytes: Display image data
        """
        img = self._new_canvas()
        draw = ImageDraw.Draw(img)

        mode = state.get("mode", "unavailable")
        lan_ip = state.get("lan_ip")
        ap_ssid = state.get("ap_ssid", self.config.ap_ssid)
        ap_password = state.get("ap_password", self.config.ap_password)
        ap_active = state.get("ap_active", False)

        if mode == "lan" and lan_ip:
            url = f"http://{lan_ip}:8080"
            label = "Scan to Config"
            print(f"[Renderer] Page 3: LAN mode (state), URL={url}")
        elif mode == "ap" or not lan_ip:
            if ap_active:
                url = f"WIFI:T:WPA;S:{ap_ssid};P:{ap_password};;"
                label = "Scan for Wi-Fi"
                print(f"[Renderer] Page 3: AP mode (state), SSID={ap_ssid}")
            else:
                # AP should be active but isn't
                url = f"WIFI:T:WPA;S:{ap_ssid};P:{ap_password};;"
                label = "AP Starting..."
                print(f"[Renderer] Page 3: AP unavailable (not active), SSID={ap_ssid}")
        else:
            # unavailable mode
            url = f"WIFI:T:WPA;S:{ap_ssid};P:{ap_password};;"
            label = "No Network"
            print(f"[Renderer] Page 3: Unavailable mode")

        # Generate QR code as PIL image (white background, black code)
        qr = qrcode.make(url, box_size=2, border=1)
        qr = qr.convert("1")  # 1-bit: black=0, white=255

        # Paste QR code centered on canvas
        qw, qh = qr.size
        px = (self.CANVAS_W - qw) // 2
        py = (self.CANVAS_H - qh) // 2 - 8
        img.paste(qr, (px, py))

        draw.text((5, 2), label, fill=0, font=self.font)
        # Show URL and hint in both LAN and AP modes
        hint = "Please visit the URL below for configuration."
        hint_w = self.font_small.getbbox(hint)[2]
        # In LAN mode, show the device IP; in AP mode, show AP gateway IP
        if mode == "lan" and lan_ip:
            display_url = url  # http://192.168.x.x:8080
        else:
            display_url = "http://10.42.0.1:8080"
        url_w = self.font_small.getbbox(display_url)[2]
        hint_x = max(0, (self.CANVAS_W - hint_w) // 2)
        url_x = max(0, (self.CANVAS_W - url_w) // 2)
        hint_y = py + qh + 2
        url_y = hint_y + 12
        draw.text((hint_x, hint_y), hint, fill=0, font=self.font_small)
        draw.text((url_x, url_y), display_url, fill=0, font=self.font_small)
        self._draw_footer(draw, "Page: 3/3 (Config)")
        return self._to_display_bytes(img)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_online() -> bool:
        """Simple check: can resolve external DNS / has default route"""
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=2)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_lan_ip() -> str:
        """Get the primary LAN IP (legacy method, does not filter loopback/link-local)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            # fallback
            out = subprocess.getoutput("hostname -I").strip()
            return out.split()[0] if out else "0.0.0.0"

    @staticmethod
    def _get_usable_lan_ip() -> Optional[str]:
        """Get a usable LAN IPv4 address (not loopback, not link-local 169.254.x.x).
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
            for ip in ips:
                # Skip loopback
                if ip.startswith("127."):
                    continue
                # Skip link-local (APIPA)
                if ip.startswith("169.254."):
                    continue
                # Skip IPv6 for now (simple check for colon)
                if ":" in ip:
                    continue
                # Valid IPv4 address
                if Renderer._is_valid_ipv4(ip):
                    return ip
            return None
        except Exception:
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

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d:
            return f"{d}d{h}h{m}m"
        return f"{h}h{m}m{s}s"
