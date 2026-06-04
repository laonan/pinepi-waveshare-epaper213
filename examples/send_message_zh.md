# PinePi WebSocket 消息发送说明

PinePi 会作为 WebSocket 客户端连接到配置里的 `wss_url`。连接建立后，客户端第一条消息会发送配置里的 `auth_token` 原文，不带 `Bearer` 前缀。

服务端发给 PinePi 的消息支持两种格式：

1. 文本 JSON 消息：用于显示简单文字。
2. 4000 字节二进制消息：用于直接显示完整图片点阵。

如果 PinePi 当前在 Page 1，会立即显示收到的消息；如果不在 Page 1，会缓存最新消息，等翻回 Page 1 时显示。

> `epaper` 是 PinePi 的设备标识，服务端需要在 `targets` 中包含此值才能触发 PinePi 显示, 如果你有多个 PinePi 设备，可以使用不同的标识来区分，如 `epaper_kitchen`、`epaper_bedroom` 等。

## 文本消息格式

发送一个 WebSocket text frame，内容为 JSON：

```json
{
  "targets": ["epaper"],
  "event": {
    "title": "Alert",
    "content": "Sensor triggered"
  }
}
```

- `targets`：接收设备列表，PinePi 仅在 `"epaper"` 出现在列表中时才处理该消息，否则忽略。
- `event.title`：显示标题。
- `event.content`：显示正文内容。

## 图片二进制格式

发送一个 WebSocket binary frame，payload 必须刚好是 `4000` 字节。

格式要求：

- 屏幕显示尺寸：`250 x 122`（横向）
- 色深：1 bit 黑白
- 白色：`1` / `0xFF`
- 黑色：`0` / `0x00`
- 字节数：4000（C 驱动内部以 `122 x 250` 竖向字节布局存储，`ceil(122 / 8) * 250 = 16 * 250 = 4000`）

发送方直接用 `250 x 122` 横向画布调用 `.tobytes()` 得到 4000 字节后发送，**不需要旋转**。PinePi 收到后会自动做坐标转换，交由 C 驱动以横屏渲染。

## Python 示例：发送文本

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

运行：

```bash
export PINEPI_WSS_URL='wss://yourdomain.com/ws/'
export PINEPI_AUTH_TOKEN='your-token-here'
python3 send_text.py
```

## Python 示例：发送图片二进制

依赖：

```bash
pip install pillow websockets
```

示例会把任意图片转成 PinePi 需要的 4000 字节黑白点阵。

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

运行：

```bash
export PINEPI_WSS_URL='wss://yourdomain.com/ws/'
export PINEPI_AUTH_TOKEN='your-token-here'
python3 send_image.py ./example.png
```

## 调试日志

在 PinePi 上看日志：

```bash
sudo journalctl -u pinepi-waveshare-epaper213 -f | grep -E 'WSClient|DisplayClient|Refreshing|Full|Partial'
```

文本消息成功时会看到：

```text
[WSClient] Message rendered title='hello'
[WSClient] Message displayed sent=True
```

图片二进制成功时会看到：

```text
[WSClient] Received 4000-byte image
```
