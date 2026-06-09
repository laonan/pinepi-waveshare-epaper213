#!/bin/bash
set -e

# =============================================================================
# PinePi Installer (Local Source Mode Preferred)
# Target: Raspberry Pi OS Lite (32-bit, Debian Bookworm)
#
# Usage:
#   sudo bash install.sh                    # Local mode (recommended)
#   PINEPI_RELEASE_URL=<url> sudo bash install.sh  # Remote mode (optional)
# =============================================================================

PINEPI_DIR="/opt/pinepi-waveshare-epaper213"
CONFIG_DIR="/etc/pinepi-waveshare-epaper213"
RELEASE_URL="${PINEPI_RELEASE_URL:-}"

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

die() {
    red "[ERROR] $*"
    exit 1
}

# ------------------------------------------------------------------
# 0. Detect script location & cd there
# ------------------------------------------------------------------
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "$SCRIPT_DIR"
fi

# ------------------------------------------------------------------
# 1. Root check
# ------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "Please run this script with sudo or as root"
fi

# ------------------------------------------------------------------
# 2. System dependencies
# ------------------------------------------------------------------
yellow "[1/8] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    liblgpio-dev \
    libopenjp2-7 \
    libfreetype6 \
    python3-venv \
    python3-pip \
    fonts-dejavu-core \
    network-manager \
    || die "Failed to install system dependencies"

# Ensure NetworkManager is running
systemctl enable --now NetworkManager >/dev/null 2>&1 || true

# ------------------------------------------------------------------
# 3. Create directory structure & ensure socket directory permissions
# ------------------------------------------------------------------
yellow "[2/8] Creating directory structure..."
mkdir -p "${PINEPI_DIR}/bin"
mkdir -p "${PINEPI_DIR}/core"
mkdir -p "${CONFIG_DIR}"

# Ensure /tmp has correct permissions for Unix Domain Sockets
chmod 1777 /tmp 2>/dev/null || true

# Clean up any stale socket files from previous runs
rm -f /tmp/pinepi.sock /tmp/pinepi-touch.sock 2>/dev/null || true

# ------------------------------------------------------------------
# 4. Create Python venv & install dependencies
# ------------------------------------------------------------------
yellow "[3/8] Setting up Python virtual environment..."
if [ ! -d "${PINEPI_DIR}/venv" ]; then
    python3 -m venv "${PINEPI_DIR}/venv"
fi

PIP="${PINEPI_DIR}/venv/bin/pip"
REQ_LOCAL=""

if [ -f "python/requirements.txt" ]; then
    REQ_LOCAL="python/requirements.txt"
elif [ -f "core/requirements.txt" ]; then
    REQ_LOCAL="core/requirements.txt"
fi

if [ -n "$REQ_LOCAL" ]; then
    yellow "  Installing from $REQ_LOCAL ..."
    "$PIP" install -r "$REQ_LOCAL" || die "Failed to install Python packages"
else
    yellow "  Local requirements.txt not found, downloading from remote..."
    curl -fsSL "${RELEASE_URL}/core/requirements.txt" \
        -o /tmp/pinepi-req.txt \
        || die "Failed to download requirements.txt"
    "$PIP" install -r /tmp/pinepi-req.txt || die "Failed to install Python packages"
fi

# ------------------------------------------------------------------
# 5. Deploy C binary (display driver)
# ------------------------------------------------------------------
yellow "[4/8] Deploying display driver..."
if [ -f "c/pinepi" ]; then
    cp "c/pinepi" "${PINEPI_DIR}/bin/pinepi-waveshare-epaper213"
elif [ -f "release/pinepi-waveshare-epaper213" ]; then
    cp "release/pinepi-waveshare-epaper213" "${PINEPI_DIR}/bin/pinepi-waveshare-epaper213"
elif [ -x "${PINEPI_DIR}/bin/pinepi-waveshare-epaper213" ]; then
    yellow "  Local display binary not found; keeping existing ${PINEPI_DIR}/bin/pinepi-waveshare-epaper213"
else
    yellow "  Downloading pinepi-waveshare-epaper213 from remote..."
    curl -fsSL "${RELEASE_URL}/pinepi-waveshare-epaper213" \
        -o "${PINEPI_DIR}/bin/pinepi-waveshare-epaper213" \
        || die "Failed to download pinepi-waveshare-epaper213. Please compile locally: cd c && make clean && make"
fi
chmod +x "${PINEPI_DIR}/bin/pinepi-waveshare-epaper213"

# ------------------------------------------------------------------
# 6. Deploy Python source
# ------------------------------------------------------------------
yellow "[5/8] Deploying Python core..."
if [ -d "python" ]; then
    cp -r python/* "${PINEPI_DIR}/core/"
elif [ -d "core" ]; then
    cp -r core/* "${PINEPI_DIR}/core/"
else
    yellow "  Downloading Python source from remote..."
    mkdir -p "${PINEPI_DIR}/core"
    curl -fsSL "${RELEASE_URL}/core/main.py" \
        -o "${PINEPI_DIR}/core/main.py" \
        || die "Failed to download main.py. Please ensure local python/ directory exists or set PINEPI_RELEASE_URL"
    curl -fsSL "${RELEASE_URL}/core/requirements.txt" \
        -o "${PINEPI_DIR}/core/requirements.txt" || true
fi

if [ -f "fonts/MSYH.ttf" ]; then
    mkdir -p "${PINEPI_DIR}/fonts"
    cp "fonts/MSYH.ttf" "${PINEPI_DIR}/fonts/MSYH.ttf"
    yellow "  Font installed from fonts/MSYH.ttf"
elif [ -f "MSYH.ttf" ]; then
    mkdir -p "${PINEPI_DIR}/fonts"
    cp "MSYH.ttf" "${PINEPI_DIR}/fonts/MSYH.ttf"
    yellow "  Font installed from MSYH.ttf"
fi

# ------------------------------------------------------------------
# 7. Register systemd service
# ------------------------------------------------------------------
yellow "[6/8] Registering systemd service..."

# Default config
if [ ! -f "${CONFIG_DIR}/config.json" ]; then
    if [ -f "config.json" ]; then
        cp "config.json" "${CONFIG_DIR}/config.json"
    else
        cat > "${CONFIG_DIR}/config.json" <<'EOF'
{
  "wifi_networks": [],
  "wss_url": "wss://your-server.com/ws",
  "auth_token": "",
  "ap_ssid": "PinePi-Config",
  "ap_password": "12345678"
}
EOF
    fi
fi

# Service file
if [ -f "pinepi-waveshare-epaper213.service" ]; then
    cp "pinepi-waveshare-epaper213.service" /etc/systemd/system/pinepi-waveshare-epaper213.service
else
    curl -fsSL "${RELEASE_URL}/pinepi-waveshare-epaper213.service" \
        -o /etc/systemd/system/pinepi-waveshare-epaper213.service \
        || die "Failed to download pinepi-waveshare-epaper213.service"
fi

systemctl daemon-reload
systemctl enable pinepi-waveshare-epaper213.service

if systemctl is-active --quiet pinepi-waveshare-epaper213; then
    systemctl restart pinepi-waveshare-epaper213
else
    systemctl start pinepi-waveshare-epaper213
fi

# ------------------------------------------------------------------
# 8. Enable dnsmasq in NetworkManager (for captive portal)
# ------------------------------------------------------------------
yellow "[7/8] Configuring NetworkManager dnsmasq for captive portal..."
NM_CONF="/etc/NetworkManager/NetworkManager.conf"
if grep -q "^dns=dnsmasq" "$NM_CONF" 2>/dev/null; then
    yellow "  dnsmasq already configured in NetworkManager, skipping"
else
    # Check if [main] section exists
    if grep -q "^\[main\]" "$NM_CONF" 2>/dev/null; then
        # Insert dns=dnsmasq after [main] line
        sed -i '/^\[main\]/a dns=dnsmasq' "$NM_CONF"
    else
        # Append [main] section
        echo -e "\n[main]\ndns=dnsmasq" >> "$NM_CONF"
    fi
    mkdir -p /etc/NetworkManager/dnsmasq-shared.d
    systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true
    green "  dnsmasq enabled in NetworkManager"
fi

# ------------------------------------------------------------------
# 9. Configure sudoers for web server reboot/shutdown
# ------------------------------------------------------------------
yellow "[8/8] Configuring sudoers for web control..."

# Get the non-root user who invoked sudo, or prompt if not available
PINEPI_USER="${SUDO_USER:-}"
if [ -z "$PINEPI_USER" ]; then
    # Try to detect from process or home directory ownership
    PINEPI_USER=$(stat -c '%U' "$HOME" 2>/dev/null || echo "")
fi

# If still not found or is root, default to common Raspberry Pi user
if [ -z "$PINEPI_USER" ] || [ "$PINEPI_USER" = "root" ]; then
    # Check if 'pi' user exists
    if id -u "pi" >/dev/null 2>&1; then
        PINEPI_USER="pi"
    # Check if current directory is owned by a non-root user
    elif [ -n "$(stat -c '%U' . 2>/dev/null)" ] && [ "$(stat -c '%U' . 2>/dev/null)" != "root" ]; then
        PINEPI_USER=$(stat -c '%U' .)
    else
        PINEPI_USER="pi"
    fi
fi

# Check if user already has the required sudo permissions
if sudo -l -U "${PINEPI_USER}" 2>/dev/null | grep -q "NOPASSWD.*reboot\|NOPASSWD.*poweroff" 2>/dev/null; then
    yellow "  User ${PINEPI_USER} already has reboot/poweroff permissions, skipping sudoers config"
else
    # Create sudoers file for the user
    SUDOERS_FILE="/etc/sudoers.d/pinepi-web"
    cat > "$SUDOERS_FILE" <<EOF
# PinePi web server permissions
# Allow user to reboot and shutdown via web UI without password
${PINEPI_USER} ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/poweroff
EOF
    chmod 440 "$SUDOERS_FILE"
    green "  Sudoers configured for user: ${PINEPI_USER}"
fi

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
green "========================================"
green "  PinePi installation complete!"
green "========================================"
echo ""
echo "  Install dir : ${PINEPI_DIR}"
echo "  Config file : ${CONFIG_DIR}/config.json"
echo "  Service     : systemctl status pinepi-waveshare-epaper213"
echo "  View logs   : journalctl -u pinepi-waveshare-epaper213 -f"
echo ""
echo "  First-time setup: edit Wi-Fi and WSS config:"
echo "    sudo nano ${CONFIG_DIR}/config.json"
echo "  Then restart the service:"
echo "    sudo systemctl restart pinepi-waveshare-epaper213"
echo ""
