#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ezviz (萤石) to ONVIF / RTSP Stream Synchronizer
Exposes all cameras under an Ezviz account to go2rtc for 7x24 continuous NVR recording.
Supports both Open Platform API (open.ys7.com) and App Session Token (api.ys7.com).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_BASE_OPEN = "https://open.ys7.com"
API_BASE_APP = "https://api.ys7.com"

DEFAULTS = {
    # 模式: "open" (推荐, 走open.ys7.com) 或 "app" (走api.ys7.com)
    "AUTH_MODE": "open",
    # 开放平台凭证 (AUTH_MODE=open 时必填)
    "APP_KEY": "",
    "APP_SECRET": "",
    # 移动客户端凭证 (AUTH_MODE=app 时使用)
    "APP_SSID": "",
    "APP_ACCOUNT": "",
    "APP_PASSWORD": "",
    # 码流画质: "hd" (高清, 推荐) 或 "sd" (标清)
    "QUALITY": "hd",
    # 协议: "rtsp"
    "PROTOCOL": "rtsp",
    # 输出配置文件路径
    "OUTPUT": "/opt/ezviz2onvif/go2rtc.yaml",
    # 是否同步离线设备 (0: 忽略离线, 1: 包含离线)
    "INCLUDE_OFFLINE": "0",
    # 是否重启或热重载 go2rtc
    "RELOAD_GO2RTC": "1",
    # go2rtc API 监听地址
    "GO2RTC_API": "http://127.0.0.1:1984",
    # Token 本地持久化缓存
    "TOKEN_CACHE": "/opt/ezviz2onvif/token.json",
}


def load_env():
    cfg = dict(DEFAULTS)
    env_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("EZVIZ_ENV", "/opt/ezviz2onvif/ezviz.env")
    )
    if os.path.exists(env_path):
        logging.info("Loading configuration from %s", env_path)
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in DEFAULTS:
        if os.environ.get(k):
            cfg[k] = os.environ[k].strip()

    mode = cfg.get("AUTH_MODE", "open").lower()
    if mode == "open":
        if not cfg.get("APP_KEY") or not cfg.get("APP_SECRET"):
            logging.warning("AUTH_MODE is 'open', but APP_KEY or APP_SECRET is empty.")
    elif mode == "app":
        if not cfg.get("APP_SSID") and not (cfg.get("APP_ACCOUNT") and cfg.get("APP_PASSWORD")):
            logging.warning("AUTH_MODE is 'app', but neither APP_SSID nor (APP_ACCOUNT+APP_PASSWORD) is provided.")
    return cfg


def http_post(url, data_dict=None, headers=None):
    post_data = urllib.parse.urlencode(data_dict or {}).encode("utf-8") if data_dict else None
    req = urllib.request.Request(url, data=post_data)
    req.add_header("User-Agent", "ezviz2onvif-gateway/1.0 (Linux x86_64)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(url, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "ezviz2onvif-gateway/1.0 (Linux x86_64)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ==================== 开放平台逻辑 ====================
def get_open_token(cfg):
    cache = cfg["TOKEN_CACHE"]
    now = int(time.time())
    if os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("token") and now < int(c.get("expire", 0)) - 7200:
                logging.info("Using cached open platform token (valid for %ds)", int(c.get("expire", 0)) - now)
                return c["token"]
        except Exception as e:
            logging.debug("Cache read failed: %s", e)

    logging.info("Requesting new accessToken from %s/api/lapp/token/get...", API_BASE_OPEN)
    r = http_post(
        f"{API_BASE_OPEN}/api/lapp/token/get",
        {"appKey": cfg["APP_KEY"], "appSecret": cfg["APP_SECRET"]},
    )
    if str(r.get("code")) != "200":
        raise RuntimeError("open token/get failed: " + json.dumps(r, ensure_ascii=False))

    token = r["data"]["accessToken"]
    expires = int(r["data"].get("expiresIn", 0))
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "expire": now + expires}, f)
    os.replace(tmp, cache)
    logging.info("New accessToken obtained, expires in %d seconds (~%d days)", expires, expires // 86400)
    return token


def list_open_devices(token):
    out = []
    page = 0
    while True:
        r = http_post(
            f"{API_BASE_OPEN}/api/lapp/device/list",
            {"accessToken": token, "pageStart": page, "pageSize": 50},
        )
        if str(r.get("code")) != "200":
            raise RuntimeError("device/list failed: " + json.dumps(r, ensure_ascii=False))
        data = r.get("data") or []
        out.extend(data)
        if len(data) < 50:
            break
        page += 1
    return out


def list_open_cameras(token, serial):
    try:
        r = http_post(
            f"{API_BASE_OPEN}/api/lapp/camera/list",
            {"accessToken": token, "deviceSerial": serial},
        )
        if str(r.get("code")) == "200":
            return r.get("data") or []
    except Exception as e:
        logging.warning("Failed to query cameras for %s: %s", serial, e)
    return []


def find_urls(obj, scheme):
    found = []

    def walk(v):
        if isinstance(v, str):
            if v.startswith(scheme + "://"):
                found.append(v)
        elif isinstance(v, list):
            for i in v:
                walk(i)
        elif isinstance(v, dict):
            for i in v.values():
                walk(i)

    walk(obj)
    return found


def get_open_live_url(token, serial, channel, cfg):
    endpoints = [
        f"{API_BASE_OPEN}/api/lapp/v2/live/address/get",
        f"{API_BASE_OPEN}/api/lapp/live/address/get",
        f"{API_BASE_OPEN}/api/lapp/v2/live/urls/get",
    ]
    protocols = [4, 2, 3, "flv", "hls", "rtsp"]
    last_err = None
    for proto in protocols:
        for ep in endpoints:
            try:
                r = http_post(ep, {
                    "accessToken": token,
                    "deviceSerial": str(serial).strip(),
                    "channelNo": int(channel),
                    "protocol": proto,
                    "quality": 1,
                    "type": 1,
                    "expireTime": 62208000,
                })
            except Exception as e:
                last_err = f"HTTP error: {e}"
                continue
            if str(r.get("code")) != "200":
                last_err = f"API error code={r.get('code')} msg={r.get('msg')}"
                continue
            data = r.get("data")
            if isinstance(data, dict):
                if data.get("url"): return data["url"]
                if data.get("liveAddress"): return data["liveAddress"]
            elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                if data[0].get("url"): return data[0]["url"]
                if data[0].get("liveAddress"): return data[0]["liveAddress"]
            urls = find_urls(data, "http") + find_urls(data, "https") + find_urls(data, "rtsp")
            if urls:
                return urls[0]
            last_err = f"No url found in response: {r}"
    raise RuntimeError(last_err)



# ==================== App 客户端接口逻辑 (备选通道) ====================
def list_app_devices(ssid):
    headers = {"ssid": ssid}
    url = f"{API_BASE_APP}/v3/userdevices/v2/devices/search"
    res = http_get(url, headers=headers)
    if str(res.get("meta", {}).get("code", "")) != "200":
        raise RuntimeError("App devices search failed: " + json.dumps(res, ensure_ascii=False))
    return res.get("deviceInfos") or []


def sanitize_name(name):
    clean = re.sub(r"[^0-9A-Za-z_\-]", "_", str(name).strip())
    return clean or "cam"


def build_stream_mappings(cfg):
    mode = cfg.get("AUTH_MODE", "open").lower()
    streams = []

    if mode == "open":
        token = get_open_token(cfg)
        devices = list_open_devices(token)
        logging.info("Found %d devices under Ezviz open platform account.", len(devices))
        for dev in devices:
            serial = str(dev.get("deviceSerial", "")).upper()
            name = dev.get("deviceName") or serial
            status = dev.get("status")
            cams = list_open_cameras(token, serial)
            if not cams:
                cams = [
                    {
                        "deviceSerial": serial,
                        "channelNo": dev.get("channelNo") or 1,
                        "status": status,
                    }
                ]
            for cam in cams:
                ch = int(cam.get("channelNo") or 1)
                cam_status = cam.get("status", status)
                label = f"{name}_ch{ch}"
                if cam_status is not None and str(cam_status) != "1" and cfg["INCLUDE_OFFLINE"] != "1":
                    logging.info("Skipping offline camera: %s (Serial: %s)", label, serial)
                    continue
                try:
                    url = get_open_live_url(token, serial, ch, cfg)
                except Exception as e:
                    logging.warning("Failed to obtain live stream for %s (ch%d): %s", serial, ch, e)
                    continue
                stream_name = sanitize_name(f"cam_{serial}_ch{ch}")
                streams.append((stream_name, url, label))
                logging.info("Configured stream: %s <- %s", stream_name, label)

    elif mode == "app":
        ssid = cfg.get("APP_SSID")
        if not ssid:
            raise RuntimeError("AUTH_MODE=app requires APP_SSID in ezviz.env")
        devices = list_app_devices(ssid)
        logging.info("Found %d devices via Ezviz app API.", len(devices))
        for dev in devices:
            serial = str(dev.get("deviceSerial", "")).upper()
            name = dev.get("deviceName") or serial
            status = dev.get("status")
            ch = int(dev.get("channelNo", 1))
            label = f"{name}_ch{ch}"
            if status is not None and str(status) != "1" and cfg["INCLUDE_OFFLINE"] != "1":
                logging.info("Skipping offline app device: %s (%s)", label, serial)
                continue
            # 在没有官方 RTSP 时，如果填入了已知的设备本地 RTSP 或代理源
            stream_name = sanitize_name(f"cam_{serial}_ch{ch}")
            # app 模式占位源或映射源
            streams.append((stream_name, f"rtsp://open.ys7.com:554/live/{serial}", label))

    return streams


def render_go2rtc_yaml(streams):
    lines = [
        "# ====================================================",
        "# Auto-generated by ezviz_sync.py",
        "# Exposing Ezviz cameras as ONVIF & RTSP for NVR 7x24",
        "# ====================================================",
        "",
        "api:",
        '  listen: ":1984"',
        "",
        "rtsp:",
        '  listen: ":8554"',
        "",
        "webrtc:",
        '  listen: ":8555"',
        "",
        "log:",
        "  level: info",
        "",
        "streams:",
    ]
    for name, url, label in streams:
        lines.append(f"  # {label}")
        lines.append(f"  {name}:")
        lines.append(f'    - "{url}"')
    lines.append("")
    return "\n".join(lines)


def hot_reload_go2rtc(api_url):
    try:
        req = urllib.request.Request(f"{api_url}/api/restart", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            logging.info("go2rtc hot-reload via API succeeded.")
            return True
    except Exception as e:
        logging.debug("go2rtc API reload failed (%s), will fallback to systemctl.", e)
    try:
        subprocess.check_call(["systemctl", "restart", "go2rtc"])
        logging.info("go2rtc restarted via systemctl.")
        return True
    except Exception as e:
        logging.warning("systemctl restart go2rtc failed: %s", e)
    return False


def main():
    logging.info("Starting Ezviz ONVIF Sync Service...")
    cfg = load_env()
    try:
        streams = build_stream_mappings(cfg)
    except Exception as e:
        logging.error("Failed to build stream mappings: %s", e)
        sys.exit(2)

    if not streams:
        logging.warning("No available streams generated. Keeping existing configuration.")
        sys.exit(0)

    new_yaml = render_go2rtc_yaml(streams)
    target_path = cfg["OUTPUT"]
    old_yaml = ""
    if os.path.exists(target_path):
        with open(target_path, "r", encoding="utf-8") as f:
            old_yaml = f.read()

    if new_yaml != old_yaml:
        tmp_path = target_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_yaml)
        os.replace(tmp_path, target_path)
        logging.info("Updated %s with %d camera streams.", target_path, len(streams))
        if cfg.get("RELOAD_GO2RTC", "1") == "1":
            hot_reload_go2rtc(cfg.get("GO2RTC_API", "http://127.0.0.1:1984"))
    else:
        logging.info("Configuration unchanged (%d active streams).", len(streams))


if __name__ == "__main__":
    main()
