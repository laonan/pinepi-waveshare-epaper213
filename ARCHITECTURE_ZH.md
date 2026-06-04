
---

# PinePi e-Paper Display 看板系统需求与架构部署规范文档 (v1.1)

## 一、 项目概述与硬件环境

### 1.1 硬件设备锁定

* **核心板卡**：Raspberry Pi Zero W / WH 或 Raspberry Pi Zero 2 W。
* **屏幕外设**：微雪 2.13寸 e-Paper 墨水屏（V3 版本，分辨率 250 x 122）。
* **触摸外设**：GT1151 触摸芯片（I2C 接口）。

### 1.2 操作系统与运行环境

* **操作系统**：**Raspberry Pi OS Lite (32-bit)**（基于 Debian Bookworm 分支）。
* **核心基石**：
  * **32位向下兼容**：Zero 2 W 虽然是 64 位芯片，但在 32 位系统下会完美兼容运行 ARMv6 指令集的 32 位程序。因此，整个产品线**仅需编译一个 32 位 C 语言可执行文件**即可通杀新老 Zero 硬件。
  * **NetworkManager**：利用系统原生的 `nmcli` 工具，由 Python 驱动，实现 **Station（连接路由）** 与 **AP（热点配网）** 模式的无缝平滑切换。注意：AP模式下无法访问外网，Page 1 将显示最后一次缓存的云端图像或本地提示。



---

## 二、 详细功能需求规范

系统在软件层面维护一个全局状态机，包含三个核心页面，通过屏幕触摸点击进行单向循环翻页（Page 1 ➔ Page 2 ➔ Page 3 ➔ Page 1）。

### Page 1：业务信息展示页（云端看板）

* **显示内容**：展示从云端服务器下发的最新的单张完整点阵位图。
* **网络行为**：实时长挂载云端 WSS（WebSocket Secure）服务。
* **动态响应**：一旦云端业务数据发生变化，服务端主动推送最新的 4000 字节点阵图，看板接收后立即刷新屏幕。

### Page 2：系统信息页（本地看板）

* **显示内容**：
* 局域网 IP 获取状态（有可用 IP / 无可用 IP）。
* 当前获取的局域网 IP 地址。
* 机器核心监控状况（CPU 占用率、内存使用率、开机运行时间）。
* 底部居中显示页码标识 `Page: 2/3 (Local)`。


* **数据来源**：完全由本地 Python 脚本实时抓取系统参数，动态画图后交付给屏幕。

### Page 3：智能配置页（配网与参数设置）

**重要约束**：当系统无可用局域网 IP 时（AP 模式），Page 1 的 WSS 连接必然中断，此时翻页到 Page 1 将显示本地提示或最后一次缓存的云端图像。

此页面根据树莓派当前是否获得可用局域网 IP，表现为两种智能行为：

* **行为 A：当系统【无可用局域网 IP】时（AP 盲操配网模式）**
1. Python 检测到无可用局域网 IP，通过后台指令强行将 Zero 的板载网卡切为 **AP 热点模式**，广播特定 SSID 的无线信号（如 `PinePi-Config`）。
2. 屏幕上生成并显示一个二维码，内容包含**连接该热点的配置信息**。
3. 用户扫码连上热点后，手机端自动弹出由树莓派本地 Python 搭建的**极简 Web 配置后台**，供用户输入家里的 Wi-Fi 密码、云端 WSS 地址以及鉴权 Token。
4. 点击提交后，树莓派收下配置，自动关闭热点切回正常模式去连接路由。


* **行为 B：当系统【已获得可用局域网 IP】时（局域网参数变更模式）**
1. 屏幕上的二维码内容自动变更为**树莓派当前局域网 IP 地址的 URL 访问链接**。
2. 用户手机连接家里同一个 Wi-Fi，扫码直接打开配置网页，可追加/修改多组 Wi-Fi 密码、调整 WebSocket 设置。网页中同时包含 **WebSocket 数据协议说明文档**。



---

## 三、 系统软件架构设计

系统采用高度解耦的“显卡 + 大脑”两层微服务架构，通过本地闭环的高速 IPC（进程间通信）维持运转。

```
+--------------------------------------------------------------------------+
|                        【大脑进程】 Python (pinepi-core)                  |
| 1. WSS 长连接客户端  2. 本地 Web 服务器 (配网)  3. Pillow 渲染引擎 (Page 2/3)  |
+--------------------------------------------------------------------------+
       | 发送 4000 字节二进制点阵图像 (UDS /tmp/pinepi.sock)       ^
       v                                                       | 发送触摸事件 (UDS /tmp/pinepi-touch.sock)
+--------------------------------------------------------------------------+
|                        【显卡进程】 C 语言 (pinepi-waveshare-epaper213)   |
| 1. liblgpio 硬件驱动  2. GT1151 触摸循环扫描  3. 局刷/全刷智能调度         |
+--------------------------------------------------------------------------+
```

### 3.1 显卡进程：C 语言端 (`pinepi-waveshare-epaper213`)

* **职责边界**：纯粹的执行性硬件守护进程。不关心网络、不关心逻辑、不关心页码。
* **核心逻辑**：
1. 开机完成 `liblgpio`、SPI 屏幕和 I2C 触摸芯片的初始化。
2. 监听 Unix Domain Socket `/tmp/pinepi.sock`（流式）。一旦收到标准的 4000 字节数据，根据新旧画面差异和局刷计数决定刷新模式：
   * **局刷模式**：仅用于同页小面积变化，使用 `EPD_2in13_V3_Init(EPD_2IN13_V3_PART)` + 质量优先的局刷波形
   * **全刷模式**：首次刷新、整页翻页/二维码等大面积变化，以及局刷累计达到周期上限时强制走一次 `FULL` 全刷清除残影
   * 最小刷新间隔 `MIN_REFRESH_INTERVAL_SEC = 1` 秒，防止过快刷新损伤屏幕
3. 主循环中以 50ms 间隔扫描 GT1151 触摸芯片，一旦捕获到有效触摸，向 Unix Domain Socket `/tmp/pinepi-touch.sock` 发送结构化 JSON：`{"type":"tap","ts":<毫秒时间戳>}`，随后休眠 300ms 防抖。



### 3.2 大脑进程：Python 语言端 (`pinepi-core`)

* **职责边界**：系统的策略中枢与状态机管理器。
* **核心逻辑**：
1. **状态机维护**：内存中存储 `current_page` 变量（1, 2, 3 对应 Page 1/2/3）。
2. **网络与事件监听**：同时挂载异步 WSS 任务和本地 Unix Domain Socket `/tmp/pinepi-touch.sock` 触摸事件监听任务。
3. **渲染分发**：
   * 当监听到触摸事件，页码循环递增（1 → 2 → 3 → 1）。
   * 根据当前页码，如果是 Page 2 或 Page 3，调用 `Pillow` 库在本地瞬间绘制出 250 x 122 的单色图像，通过 `.rotate(90)` 顺应 C 语言底层对齐，导出 4000 字节二进制，通过 Unix Domain Socket `/tmp/pinepi.sock` 发送给 C 进程。
   * 如果是 Page 1，则把 WSS 拿到的云端二进制图直接转发给 C 进程；若当前无可用局域网 IP，显示最后一次缓存图像或本地提示页面。





---

## 四、 部署与分发设计

### 4.1 本地源码安装（推荐）

在完整源码目录下执行（C 驱动编译为可选）：

```bash
# 可选：自行编译 C 显示驱动
cd c && make clean && make && cd ..

# 一键安装
sudo bash install.sh
```

脚本自动完成以下 7 个步骤：

1. **系统依赖安装** - 自动安装 `liblgpio-dev`, `libopenjp2-7`, `libfreetype6`, `python3-venv`, `python3-pip`, `fonts-dejavu-core`, `network-manager`
2. **目录结构创建** - 创建 `/opt/pinepi-waveshare-epaper213/` 和 `/etc/pinepi-waveshare-epaper213/`
3. **Python 虚拟环境** - 创建隔离 venv 并安装 `websockets`, `pillow`, `qrcode`, `psutil`, `flask` 等依赖
4. **C 驱动部署** - 优先使用本地编译的 `c/pinepi`，不存在则报错提示用户编译
5. **Python 源码部署** - 优先使用本地 `python/` 目录
6. **Systemd 服务注册** - 注册并启动 `pinepi-waveshare-epaper213.service`
7. **Sudoers 配置** - 自动配置当前用户免密执行 `reboot` 和 `poweroff`（用于 Web 控制）

### 4.2 远程安装模式（可选，需自行部署 CDN）

如需通过网络分发，可将制品部署到 CDN：

```
release/
├── pinepi-waveshare-epaper213      # 32 位 C 二进制文件
├── core/
│   ├── main.py
│   └── requirements.txt
└── pinepi-waveshare-epaper213.service
```

用户执行：

```bash
export PINEPI_RELEASE_URL=https://your-cdn.example.com/pinepi/release
sudo bash install.sh
```

### 4.3 字体文件（可选）

如需中文显示支持，将 `MSYH.ttf` 放置于项目根目录，安装脚本会自动复制到 `/opt/pinepi-waveshare-epaper213/fonts/`。

### 4.4 安装后验证

```bash
# 查看服务状态
sudo systemctl status pinepi-waveshare-epaper213

# 查看实时日志
sudo journalctl -u pinepi-waveshare-epaper213 -f

# 编辑配置
sudo nano /etc/pinepi-waveshare-epaper213/config.json
sudo systemctl restart pinepi-waveshare-epaper213
```

---
