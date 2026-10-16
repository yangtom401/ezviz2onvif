# Ezviz ONVIF Gateway (萤石转 ONVIF / RTSP 网关)

将萤石账号下的所有远程/4G摄像头转为标准 ONVIF Profile S/T 和 RTSP 流，供局域网录像机 (NVR) 7x24 小时无间断录像。

## 架构说明
- **go2rtc**：高性能流媒体引擎，负责 WS-Discovery 局域网自动发现、ONVIF SOAP 协议栈与 RTSP 转发。
- **ezviz_sync**：自动化同步守护进程，管理设备发现、Token 自动续期与热重载。
- **systemd**：提供开机自启、崩溃重启、多级健康监测。

## 快速安装
```bash
sudo bash install.sh
```

## 配置
在 `/opt/ezviz2onvif/ezviz.env` 中配置凭证：
```ini
AUTH_MODE=open
APP_KEY=your_app_key
APP_SECRET=your_app_secret
QUALITY=hd
```
重启并同步：
```bash
python3 /opt/ezviz2onvif/ezviz_sync.py
systemctl restart go2rtc
```
