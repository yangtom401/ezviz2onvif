# Ezviz ONVIF Gateway (萤石转 ONVIF / RTSP 网关)

将萤石账号下的所有远程/4G 摄像头转为标准 ONVIF Profile S/T 和 RTSP 流，供局域网录像机 (NVR) 7x24 小时无间断录像。

## 架构说明
- **go2rtc**：高性能流媒体引擎，负责 WS-Discovery 局域网自动发现、ONVIF SOAP 协议栈与 RTSP 转发。
- **stream_client.py**：纯 Python 原生直连萤石云（ECDH + ChaCha20 信令层），按设备类型解析
  MPEG-PS / H.264 / H.265 RTP，转封装后推入 go2rtc 的 RTSP 服务。
- **ezviz_sync**：自动化同步守护进程，管理设备发现、Token 自动续期与热重载。
- **phone_relay.sh**：把**手机网关**（官方 SDK 解密后的明文 RTSP）转推进 go2rtc，用于
  4G / ver=2 加密路（B K1969502、BB8930323）—— 见下方"已知限制"。
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

## 设备路由：直连 vs 手机网关

`stream_client.py` 直连萤石云能拿到大多数设备的明文流，但 **4G 设备与 ver=2 整包加密设备**
（`BB8930323`、`BK1969502`）在 VTDU 层是加密的，纯 Python 尚不能解密（见
`D:\APP\ys\linux\SRTP_REVERSE_ENGINEERING.md`）。

当前处置：把这两类设备从直连中排除，改由**手机网关**（`com.videogo.gw`，用官方 SDK 在手机内解密，
输出明文 RTSP :8554）供给：

```bash
# systemd 单元中已配置
ExecStart=/usr/bin/python3 -u /opt/ezviz2onvif/stream_client.py --all \
          --exclude-serial BK1969502,BB8930323
```

手机网关侧（10.0.0.185）稳定路径（不依赖 App 枚举顺序）：

| go2rtc stream | 手机 RTSP 路径 |
|---------------|----------------|
| `cam_BK1969502_ch1` | `rtsp://10.0.0.185:8554/BK1969502_1` |
| `cam_BK1969502_ch2` | `rtsp://10.0.0.185:8554/BK1969502_2` |
| `cam_BB8930323_ch1` | `rtsp://10.0.0.185:8554/BB8930323_1` |

`ezviz-phone-relay.service` 负责把这些明文流转推进 go2rtc（`phone_relay.sh`）。
若手机网关不可用，相应通道会缺画面（其余通道不受影响）。

## TF 卡录像工具 (ezviz_sd_download.py)

纯 Python 的萤石摄像头 TF 卡（SD 卡）录像工具，无需手机参与。分两层：

**Layer 1 — 录像索引（已实测可用）**

列出摄像头 SD 卡上某天的录像片段（等价于 App 的"回放"时间轴）。走云接口
`/v3/streaming/common/records` 让设备自己枚举 SD 卡时间线，返回 zlib 压缩的
二进制（每段 8 字节：`[u24 begin][u24 end][0x0800]`，值为 `小时*65536 + 小时内秒`）。
实测 `BK1969502` 50 段、`BB8930323` 25 段，解码正确。

```bash
python3 ezviz_sd_download.py list BK1969502 1 --day 2026-10-24 --token token.json
```

**Layer 2 — 录像下载（完整逆向重构）**

云 `/download` 服务对 NAT 后设备会返回 5405（服务端反向拉不到设备，见
`EZVIZ_PROTOCOL_COMPLETE.md` §17）；App 实际走 **CAS → DirectClient P2P → 中继(6123) → 设备**。
`download_capture.pcap`（一次真实 App 下载抓包）证实中继握手与直播的 EZStream ECDH 信封
逐字节一致，只是外层多包一层 NP 帧（`24 01 … 00 04 | 01 01 00 00 | EZStream…`）。
模块已实现该隧道框架并复用直播的 ECDH/ChaCha20 加密；只差设备的 `p2pInfo`（P2P 身份），
它不出现在云 HTTP 接口里，需从设备一次性提取（Frida hook `NativeApi.createDownloadClient`，
见 `hook_download.js`），之后下载即为纯代码、无需手机：

```bash
python3 ezviz_sd_download.py download BK1969502 1 00:29:23 00:30:05 \
        --day 2026-10-23 --p2pinfo "<从设备提取的 p2pInfo>" --out clip.ps
```

> 会话 `token.json`（session_id）会过期（401），用 `web_login.py` / `ezviz_app_login.py`
> 续期即可，模块把 token 仅作运行时输入。`token.json` 已在 `.gitignore` 中，不会外泄。

## 已知限制
- 4G 设备并发受限：CT116 直连与手机网关同时拉同一路会互相抢占（VTDU 返回 5405），
  因此同一路只能有一个消费方。
- 手机网关在 Android 5.0 上需**解锁后**才会自启（BOOT_COMPLETED 需解锁才广播），
  重启后可能需要人工解锁一次。
- `ezviz-sync.service` 当前为 failed 状态，`go2rtc.yaml` 由 `web_portal.py` 启动时生成；
  手工改动 yaml 后需重启 `ezviz-web` 或 `go2rtc`。
