# PinePi e-Paper Display Dashboard System

A cloud/local hybrid dashboard based on Raspberry Pi Zero + Waveshare 2.13 inch e-Paper display (V3).

---

## Project Structure

```
pinepi-waveshare-epaper213/
├── .gitignore              # Git ignore rules
├── ARCHITECTURE_ZH.md      # Requirements & architecture design doc (Chinese)
├── README.md               # This file
├── install.sh              # One-click install script
├── pinepi-waveshare-epaper213.service  # systemd service config
├── config.json             # Default config template
├── test.py                 # Local test script (no hardware needed)
├── c/                      # C display driver process (hardware layer)
│   ├── Makefile
│   ├── src/main.c          # Core: UDS image recv + touch report + partial/full refresh
│   └── lib/                # Waveshare official driver libs (EPD / GT1151 / GUI / Fonts)
├── python/                 # Python brain process (strategy hub)
│   ├── main.py             # asyncio main loop + subprocess C process management
│   ├── requirements.txt
│   ├── config.py           # /etc/pinepi-waveshare-epaper213/config.json read/write
│   ├── state_machine.py    # Page state machine (Page 1/2/3)
│   ├── display_client.py   # UDS send image to C
│   ├── touch_listener.py   # UDS receive TAP touch events
│   ├── renderer.py         # Pillow real-time render Page 2/3
│   ├── ws_client.py        # WebSocket Secure cloud client
│   ├── web_server.py       # Flask config web UI + reboot/shutdown buttons
│   └── network_manager.py  # nmcli wrapper (Station / AP switching)
├── fonts/                  # Font files (user-provided, e.g., MSYH.ttf)
└── examples/               # Example messages and documentation
```

---

## Architecture Overview

```
+--------------------------------------------------------------------------+
|                        [Brain Process] Python (pinepi-core)            |
|  WSS Client  |  Local Web Server  |  Pillow Render Engine  |  NetworkManager  |
+--------------------------------------------------------------------------+
       | Send 4000-byte binary frame buffer (UDS /tmp/pinepi.sock)  ^
       v                                                          | Send TAP JSON (UDS /tmp/pinepi-touch.sock)
+--------------------------------------------------------------------------+
|                        [Display Driver] C (pinepi-waveshare-epaper213)   |
|  liblgpio hardware driver  |  GT1151 touch scan  |  2.13" e-Paper partial/full refresh  |
+--------------------------------------------------------------------------+
```

- **C Process**: Pure hardware execution layer. Launched by Python via `subprocess` with separate session group. Only responsible for:
  1. Listening on Unix Domain Socket `/tmp/pinepi.sock`, executing partial or full refresh based on counter after receiving 4000 bytes.
  2. Scanning GT1151 touch chip, sending `{"type":"tap","ts":...}` to `/tmp/pinepi-touch.sock` when valid touch detected.
- **Python Process**: Strategy hub. Maintains `current_page` (1→2→3→1 cycle), renders or forwards images based on page number.

---

## Page Description

| Page | Name | Content Source | Description |
|------|------|----------------|-------------|
| 1 | Cloud Dashboard | WSS server push | Receive 4000-byte binary bitmap, refresh to screen directly |
| 2 | Local Monitor | Python Pillow real-time render | CPU / Memory / IP / Uptime |
| 3 | Smart Config | Python Pillow real-time render | No LAN IP = Wi-Fi AP QR code; Has LAN IP = Config page URL QR code |

Tap anywhere on screen to cycle through pages in single direction.

---

## Installation

Execute on **Raspberry Pi OS Lite (32-bit)**:

### Local Installation (Recommended)

Clone the repository and run the installer:

```bash
git clone https://github.com/laonan/pinepi-waveshare-epaper213.git
cd pinepi-waveshare-epaper213
cd c
make clean && make
cd ..
sudo bash install.sh
```

After installation:
```bash
# Check service status
sudo systemctl status pinepi-waveshare-epaper213

# View real-time logs
sudo journalctl -u pinepi-waveshare-epaper213 -f

# Edit config
sudo nano /etc/pinepi-waveshare-epaper213/config.json
sudo systemctl restart pinepi-waveshare-epaper213
```

---

## Configuration (`/etc/pinepi-waveshare-epaper213/config.json`)

| Field | Type | Description |
|-------|------|-------------|
| `wifi_networks` | Array | Wi-Fi list, `[{ssid, password}]` |
| `wss_url` | String | Cloud WebSocket URL, e.g., `wss://server.com/ws` |
| `auth_token` | String | WSS auth token (sent as-is in first message after connection, without Bearer prefix) |
| `ap_ssid` | String | AP hotspot name (default `PinePi-Config`) |
| `ap_password` | String | AP hotspot password (default `12345678`) |

---

## WebSocket Server

For the cloud WebSocket server that pushes display content to the device, you can deploy **[pinepi-broadcast-mid](https://github.com/laonan/pinepi-broadcast-mid)**:

```bash
git clone https://github.com/laonan/pinepi-broadcast-mid.git
cd pinepi-broadcast-mid
# Follow the repository's README to set up the server
```

Set the `wss_url` and `auth_token` in the device config to match your deployed server.

---

## Key Design Decisions

1. **C Process Crash Auto-Restart**: Python built-in watchdog, `poll()` detects C process status, auto-spawns new process within 2 seconds.
2. **GT_Scan Thread Safety**: All touch scans execute only in main thread; UDS receive thread only delivers refresh flags, avoiding race on `Dev_Now`.
3. **Flush Touch Cache After Refresh**: After each screen refresh, continuously read GT1151 chip 10 times to discard ghost touch data generated during e-paper refresh jitter.
4. **AP Mode Smart Fallback**: Python background coroutine checks LAN IP availability every 15 seconds, auto-enables hotspot when no usable LAN IP is detected, auto-disables hotspot when a valid LAN IP is obtained.

---

## Security Notes

**This device requires a trusted local network environment.**

- The web configuration page (`http://<device-ip>:8080`) has **no access authentication**
- Wi-Fi passwords and auth tokens are displayed and transmitted in **plaintext**
- The configuration page includes **reboot/shutdown controls** that execute immediately
- **Do not expose port 8080 to the public internet** or untrusted networks

**Recommended deployment:**
- Use within trusted network segments (VLANs, isolated management networks, secure LANs)
- Implement firewall rules to block external access to port 8080
- For production use, ensure the deployment network is properly secured and segmented

---

## License

MIT (C driver library follows Waveshare original license)

## Waveshare 2.13inch E-Paper HAT Documentation

For detailed documentation on the Waveshare 2.13inch E-Paper HAT, please refer to the official documentation:

- [Waveshare 2.13inch E-Paper HAT Product Page](https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT)
