# PinePi WebSocket Message Sending Guide

PinePi connects to the configured `wss_url` as a WebSocket client. After the connection is established, the client sends the raw `auth_token` from the configuration as its first message, without a `Bearer` prefix.

The server can send messages to PinePi in two formats:

1. Text JSON message: for displaying simple text.
2. 4000-byte binary message: for directly displaying a full image dot matrix.

If PinePi is currently on Page 1, the received message is shown immediately; otherwise the latest message is cached and shown when the user switches back to Page 1.

> `epaper` is the PinePi device identifier. The server must include this value in `targets` to trigger PinePi display. If you have multiple PinePi devices, you can use different identifiers to distinguish them, such as `epaper_kitchen`, `epaper_bedroom`, etc.

## Text Message Format

Send a WebSocket text frame with the following JSON content:

```json
{
  "targets": ["epaper"],
  "event": {
    "title": "Alert",
    "content": "Sensor triggered"
  }
}
```

- `targets`: list of receiving devices. PinePi processes the message only when `"epaper"` is in the list; otherwise it is ignored.
- `event.title`: the title to display.
- `event.content`: the body content to display.

## Image Binary Format

Send a WebSocket binary frame with a payload of exactly `4000` bytes.

Format requirements:

- Display dimensions: `250 x 122` (landscape)
- Color depth: 1-bit black and white
- White: `1` / `0xFF`
- Black: `0` / `0x00`
- Byte count: 4000 (the C driver stores it internally in a `122 x 250` portrait byte layout, `ceil(122 / 8) * 250 = 16 * 250 = 4000`)

The sender can directly use a `250 x 122` landscape canvas and call `.tobytes()` to get the 4000 bytes; **no rotation is needed**. PinePi will perform the coordinate conversion automatically after receiving and hand it to the C driver for landscape rendering.

## Python Example: Sending Text

```python
#!/usr/bin/env python3
import asyncio
import json
import os
import websockets

WSS_URL = os.environ.get("PINEPI_WSS_URL", "wss://yourdomain.com/ws/")
TOKEN = os.environ["PINEPI_AUTH_TOKEN"]


async def main():
    async with websockets.connect(
        WSS_URL,
        ping_interval=None,
        ping_timeout=None,
        close_timeout=5,
    ) as ws:
        await ws.send(TOKEN)

        msg = {
            "targets": ["epaper"],
            "event": {
                "title": "Alert",
                "content": "Sensor triggered",
            },
        }
        await ws.send(json.dumps(msg, ensure_ascii=False))
        print("text message sent")


if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
export PINEPI_WSS_URL='wss://yourdomain.com/ws/'
export PINEPI_AUTH_TOKEN='your-token-here'
python3 send_text.py
```

## Python Example: Sending Binary Image

Dependencies:

```bash
pip install pillow websockets
```

This example converts any image into the 4000-byte black-and-white dot matrix that PinePi requires.

```python
#!/usr/bin/env python3
import asyncio
import os
import sys
import websockets
from PIL import Image

WSS_URL = os.environ.get("PINEPI_WSS_URL", "wss://yourdomain.com/ws/")
TOKEN = os.environ["PINEPI_AUTH_TOKEN"]


def image_to_pinepi_bytes(path: str) -> bytes:
    # Server sends landscape (250×122); PinePi will rotate(270) on receipt.
    img = Image.open(path).convert("L")
    img = img.resize((250, 122))

    # Convert to 1-bit black/white. Threshold can be adjusted if needed.
    img = img.point(lambda p: 255 if p > 160 else 0).convert("1")

    if img.size != (250, 122):
        raise ValueError(f"unexpected image size: {img.size}")

    data = img.tobytes()
    if len(data) != 4000:
        raise ValueError(f"unexpected payload size: {len(data)}")
    return data


async def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 send_image.py /path/to/image.png")

    payload = image_to_pinepi_bytes(sys.argv[1])

    async with websockets.connect(
        WSS_URL,
        ping_interval=None,
        ping_timeout=None,
        close_timeout=5,
    ) as ws:
        await ws.send(TOKEN)
        await ws.send(payload)
        print(f"binary image sent: {len(payload)} bytes")


if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
export PINEPI_WSS_URL='wss://yourdomain.com/ws/'
export PINEPI_AUTH_TOKEN='your-token-here'
python3 send_image.py ./example.png
```

## Debug Logs

View logs on PinePi:

```bash
sudo journalctl -u pinepi-waveshare-epaper213 -f | grep -E 'WSClient|DisplayClient|Refreshing|Full|Partial'
```

For a successful text message you should see:

```text
[WSClient] Message rendered title='hello'
[WSClient] Message displayed sent=True
```

For a successful binary image you should see:

```text
[WSClient] Received 4000-byte image
```
