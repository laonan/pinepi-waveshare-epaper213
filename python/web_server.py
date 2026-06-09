import subprocess
import time
from flask import Flask, request, render_template_string, jsonify
from werkzeug.serving import make_server

HTML_INDEX = """
<!doctype html>
<html>
<head><title>PinePi Config</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:24px auto;padding:0 12px}
h1{font-size:1.4rem}
label{display:block;margin-top:12px;font-weight:600}
input{width:100%;padding:8px;box-sizing:border-box;margin-top:4px;border:1px solid #ccc;border-radius:4px}
button{margin-top:16px;padding:10px 18px;background:#111;color:#fff;border:none;border-radius:4px;cursor:pointer}
.msg{margin-top:12px;padding:10px;border-radius:4px}
.msg.ok{background:#e6f4ea;color:#137333}
.msg.err{background:#fce8e8;color:#c5221f}
.nav{margin-top:24px;border-top:1px solid #eee;padding-top:12px}
.nav a{color:#333}
</style>
</head>
<body>
<h1>PinePi Config</h1>
<p style="color:#666;font-size:.9rem">Click Save after changes; device will apply new config automatically.</p>
<form method="post" action="/save">
  <h3>Wi-Fi Networks</h3>
  <label>Wi-Fi 1 SSID</label>
  <input name="ssid_1" value="{{ ssid_1 }}" placeholder="MyHome-5G">
  <label>Wi-Fi 1 Password</label>
  <input name="pwd_1" type="text" value="{{ pwd_1 }}" placeholder="Leave empty to keep unchanged">

  <label>Wi-Fi 2 SSID</label>
  <input name="ssid_2" value="{{ ssid_2 }}" placeholder="Backup network">
  <label>Wi-Fi 2 Password</label>
  <input name="pwd_2" type="text" value="{{ pwd_2 }}">

  <h3 style="margin-top:18px">Cloud Service</h3>
  <label>WSS URL</label>
  <input name="wss_url" value="{{ wss_url }}" placeholder="wss://your-server.com/ws">
  <label>Auth Token</label>
  <input name="token" type="text" value="{{ token }}">

  <button type="submit">Save & Apply</button>
</form>

<form method="post" action="/reboot" style="display:inline-block;margin-right:8px">
  <button type="submit" style="background:#c5221f">Reboot</button>
</form>
<form method="post" action="/shutdown" style="display:inline-block">
  <button type="submit" style="background:#555">Power Off</button>
</form>

{% if message %}
  <div class="msg {{ mtype }}">{{ message }}</div>
{% endif %}

<div class="nav">
  <a href="/docs">WebSocket Protocol Docs</a>
</div>
</body>
</html>
"""

HTML_DOCS = """
<!doctype html>
<html>
<head><title>PinePi Protocol Docs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 12px;line-height:1.6}
code{background:#f4f4f4;padding:2px 6px;border-radius:3px}
pre{background:#111;color:#0f0;padding:12px;border-radius:6px;overflow:auto}
h1{font-size:1.4rem}
h2{font-size:1.1rem;margin-top:24px;border-bottom:1px solid #eee;padding-bottom:6px}
</style>
</head>
<body>
<h1>PinePi WebSocket Data Protocol</h1>

<h2>1. Connection & Auth</h2>
<p>After the client (Raspberry Pi) connects to the server via <code>wss://</code>, the <strong>first message</strong> must send the auth Token. Only enter the Token itself in the config page; the program will send it as-is:</p>
<pre>YOUR_TOKEN_HERE</pre>
<p>After server verification, bidirectional communication begins.</p>

<h2>2. Server → Client: Binary Frame Buffer</h2>
<p>When business data updates, the server proactively pushes a <strong>binary WebSocket message</strong> with fixed length of <strong>4000 bytes</strong>.</p>
<ul>
<li>Resolution: 250 × 122 pixels (horizontal logical canvas), mapped to screen 122 × 250 after client <code>.rotate(90)</code>.</li>
<li>Color depth: 1 bit (monochrome), white = <code>0xFF</code>, black = <code>0x00</code>.</li>
<li>Bytes per row: 122 pixels per row, rounded up to 16 bytes (<code>ceil(122/8) = 16</code>), 250 rows total → <code>16 × 250 = 4000</code> bytes.</li>
<li>Pillow export: <code>img.rotate(90, expand=True).tobytes()</code></li>
</ul>

<h2>3. Client → Server: Touch Events (Optional Extension)</h2>
<p>Current version touch page-flip is local closed-loop behavior (C ↔ Python), not reported to cloud. Can be extended as heartbeat or status reporting in the future.</p>

<h2>4. Local IPC Protocol</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;width:100%">
<tr><th>Direction</th><th>Protocol</th><th>Path</th><th>Payload</th></tr>
<tr><td>Python → C</td><td>UDS stream</td><td><code>/tmp/pinepi.sock</code></td><td>4000 bytes binary image</td></tr>
<tr><td>C → Python</td><td>UDS datagram</td><td><code>/tmp/pinepi-touch.sock</code></td><td>JSON <code>{"type":"tap","ts":...}</code></td></tr>
</table>

<h2>5. Page State Machine</h2>
<ul>
<li><strong>Page 1</strong>: Display latest image pushed from cloud WSS.</li>
<li><strong>Page 2</strong>: Local system monitor (Python Pillow real-time rendering).</li>
<li><strong>Page 3</strong>: Config QR code (offline = Wi-Fi AP info, online = this page URL).</li>
</ul>
<p>Three pages cycle through single-direction page flip by tapping anywhere on screen.</p>

<div style="margin-top:32px;border-top:1px solid #eee;padding-top:12px">
<a href="/">← Back to Config</a>
</div>
</body>
</html>
"""


class WebServer:
    """Minimal Flask web UI for device configuration"""

    def __init__(self, config, network_manager, port: int = 8080):
        self.config = config
        self.nm = network_manager
        self.port = port
        self.app = self._create_app()
        self._server = make_server("0.0.0.0", port, self.app, threaded=True)

    def _create_app(self):
        app = Flask(__name__)

        @app.route("/")
        def index():
            nets = self.config.wifi_networks
            ctx = {
                "ssid_1": nets[0].get("ssid", "") if len(nets) > 0 else "",
                "pwd_1": nets[0].get("password", "") if len(nets) > 0 else "",
                "ssid_2": nets[1].get("ssid", "") if len(nets) > 1 else "",
                "pwd_2": nets[1].get("password", "") if len(nets) > 1 else "",
                "wss_url": self.config.wss_url,
                "token": self.config.auth_token,
                "message": request.args.get("msg", ""),
                "mtype": request.args.get("type", ""),
            }
            return render_template_string(HTML_INDEX, **ctx)

        @app.route("/save", methods=["POST"])
        def save():
            networks = []
            for i in range(1, 3):
                ssid = request.form.get(f"ssid_{i}", "").strip()
                pwd = request.form.get(f"pwd_{i}", "").strip()
                if ssid:
                    networks.append({"ssid": ssid, "password": pwd})

            self.config.wifi_networks = networks
            self.config.wss_url = request.form.get("wss_url", "").strip()
            self.config.auth_token = request.form.get("token", "").strip()

            # Try switching back to Station mode and connect
            self.nm.stop_ap()
            time.sleep(1)  # Wait for AP to fully stop

            # Check if we need to switch Wi-Fi networks
            ok = False
            if networks:
                current_ssid = self.nm.get_current_wifi_ssid()
                target_ssid = networks[0].get("ssid", "").strip()

                if current_ssid == target_ssid:
                    # Already on correct network, just verify connection
                    print(f"[WebServer] Already connected to target SSID: {target_ssid}")
                    ok = True
                elif current_ssid:
                    # Connected to wrong network - need to disconnect first
                    print(f"[WebServer] Connected to {current_ssid}, need to switch to {target_ssid}")
                    self.nm.disconnect_wifi()
                    time.sleep(2)  # Wait for disconnect
                    ok = self.nm.connect_wifi(target_ssid, networks[0].get("password", ""))
                else:
                    # Not connected, try to connect to primary network
                    print(f"[WebServer] Not connected, attempting to connect to {target_ssid}")
                    ok = self.nm.ensure_best_wifi(networks)

            # Restart service to apply WSS settings
            print("[WebServer] Restarting service to apply new configuration...")
            subprocess.Popen(["sudo", "systemctl", "restart", "pinepi-waveshare-epaper213"])

            msg = "Config saved. Service restarting to apply changes." + (" Connected to network." if ok else " Trying to connect...")
            mtype = "ok" if ok else "err"
            return render_template_string(
                HTML_INDEX,
                ssid_1=networks[0].get("ssid", "") if len(networks) > 0 else "",
                pwd_1=networks[0].get("password", "") if len(networks) > 0 else "",
                ssid_2=networks[1].get("ssid", "") if len(networks) > 1 else "",
                pwd_2=networks[1].get("password", "") if len(networks) > 1 else "",
                wss_url=self.config.wss_url,
                token=self.config.auth_token,
                message=msg,
                mtype=mtype,
            )

        @app.route("/docs")
        def docs():
            return render_template_string(HTML_DOCS)

        @app.route("/reboot", methods=["POST"])
        def reboot():
            print("[WebServer] Reboot requested via web UI")
            subprocess.Popen(["sudo", "reboot"])
            return render_template_string(
                HTML_INDEX,
                ssid_1=request.form.get("ssid_1", ""),
                pwd_1=request.form.get("pwd_1", ""),
                ssid_2=request.form.get("ssid_2", ""),
                pwd_2=request.form.get("pwd_2", ""),
                wss_url=self.config.wss_url,
                token=self.config.auth_token,
                message="Rebooting device...",
                mtype="ok",
            )

        @app.route("/shutdown", methods=["POST"])
        def shutdown():
            print("[WebServer] Shutdown requested via web UI")
            subprocess.Popen(["sudo", "poweroff"])
            return render_template_string(
                HTML_INDEX,
                ssid_1=request.form.get("ssid_1", ""),
                pwd_1=request.form.get("pwd_1", ""),
                ssid_2=request.form.get("ssid_2", ""),
                pwd_2=request.form.get("pwd_2", ""),
                wss_url=self.config.wss_url,
                token=self.config.auth_token,
                message="Shutting down...",
                mtype="ok",
            )

        # ------------------------------------------------------------------
        # Captive Portal Detection Routes (Auto-redirect to config page)
        # ------------------------------------------------------------------

        PORTAL_IP = self.nm.AP_GATEWAY_IP if self.nm else "10.42.0.1"
        PORTAL_URL = f"http://{PORTAL_IP}:8080/"
        REDIRECT_HTML = f"""<!DOCTYPE html>
<html><head><title>PinePi Config</title>
<meta http-equiv="refresh" content="0; url={PORTAL_URL}">
</head><body>
<p>Redirecting to <a href="{PORTAL_URL}">PinePi Configuration</a>...</p>
</body></html>"""

        @app.route("/generate_204")   # Android connectivity check
        @app.route("/generate204")
        @app.route("/connecttest.txt") # Windows NCSI
        @app.route("/ncsi.txt")
        @app.route("/hotspot-detect.html")        # Apple iOS/macOS
        @app.route("/library/test/success.html")  # Apple fallback
        @app.route("/success.txt")                # Apple iOS 14+
        @app.route("/canonical.html")             # Apple macOS
        @app.route("/redirect")
        def captive_portal():
            """Captive portal: redirect all OS connectivity checks to config page"""
            return REDIRECT_HTML, 200

        return app

    def run(self):
        print(f"[WebServer] Starting on 0.0.0.0:{self.port}")
        self._server.serve_forever()

    def shutdown(self):
        self._server.shutdown()
