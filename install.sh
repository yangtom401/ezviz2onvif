#!/usr/bin/env bash
set -e

APP_DIR="/opt/ezviz2onvif"
mkdir -p "$APP_DIR"
cd "$(dirname "$0")"

echo "=== Installing Ezviz ONVIF Gateway ==="

# Check architecture
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    GO2RTC_BIN="go2rtc_linux_amd64"
elif [ "$ARCH" = "aarch64" ]; then
    GO2RTC_BIN="go2rtc_linux_arm64"
elif [[ "$ARCH" =~ ^armv ]]; then
    GO2RTC_BIN="go2rtc_linux_arm"
else
    GO2RTC_BIN="go2rtc_linux_amd64"
fi

if [ ! -f "$APP_DIR/go2rtc" ]; then
    if [ -f "./go2rtc" ]; then
        cp ./go2rtc "$APP_DIR/go2rtc"
    elif [ -f "./$GO2RTC_BIN" ]; then
        cp "./$GO2RTC_BIN" "$APP_DIR/go2rtc"
    else
        echo "Downloading go2rtc binary ($GO2RTC_BIN)..."
        curl -fsSL -o "$APP_DIR/go2rtc" "https://github.com/AlexxIT/go2rtc/releases/latest/download/$GO2RTC_BIN" || \
        curl -fsSL -o "$APP_DIR/go2rtc" "https://ghproxy.net/https://github.com/AlexxIT/go2rtc/releases/latest/download/$GO2RTC_BIN"
    fi
fi
chmod +x "$APP_DIR/go2rtc"

cp ezviz_sync.py "$APP_DIR/ezviz_sync.py"
chmod +x "$APP_DIR/ezviz_sync.py"

if [ ! -f "$APP_DIR/go2rtc.yaml" ]; then
    cp go2rtc.yaml "$APP_DIR/go2rtc.yaml"
fi

if [ ! -f "$APP_DIR/ezviz.env" ]; then
    cp ezviz.env.example "$APP_DIR/ezviz.env"
    chmod 600 "$APP_DIR/ezviz.env"
    echo "Notice: Default ezviz.env installed. Please update /opt/ezviz2onvif/ezviz.env with your Ezviz credentials."
fi

# Install systemd services
cp systemd/go2rtc.service /etc/systemd/system/
cp systemd/ezviz-sync.service /etc/systemd/system/
cp systemd/ezviz-sync.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable go2rtc.service
systemctl enable ezviz-sync.timer
systemctl start go2rtc.service
systemctl start ezviz-sync.timer

echo "=== Ezviz ONVIF Gateway installation complete ==="
systemctl status go2rtc.service --no-pager
