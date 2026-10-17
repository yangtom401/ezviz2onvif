#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ezviz ONVIF Gateway Web Management Portal
Provides a clean, modern web UI on port 80 for logging in to Ezviz,
discovering account cameras, and syncing them into go2rtc for NVR 7x24 recording.
"""

import argparse
import hashlib
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_BASE_APP = "https://api.ys7.com"
API_BASE_OPEN = "https://open.ys7.com"
PORT = 80
TOKEN_FILE = "/opt/ezviz2onvif/token.json"
GO2RTC_YAML = "/opt/ezviz2onvif/go2rtc.yaml"
GO2RTC_API = "http://127.0.0.1:1984"


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


def get_open_live_url(token, serial, channel):
    endpoints = [
        f"{API_BASE_OPEN}/api/lapp/v2/live/address/get",
        f"{API_BASE_OPEN}/api/lapp/live/address/get",
        f"{API_BASE_OPEN}/api/lapp/v2/live/urls/get",
    ]
    # 4: flv (HTTP-FLV low latency), 2: hls, 3: rtmp
    protocols = [4, 2, 3, "flv", "hls", "rtsp", "rtmp"]
    for proto in protocols:
        for ep in endpoints:
            try:
                params = {
                    "accessToken": token,
                    "deviceSerial": str(serial).strip(),
                    "channelNo": int(channel),
                    "protocol": proto,
                    "quality": 1,
                    "type": 1,
                    "expireTime": 62208000,
                }
                r = post_json_api(ep, params)
                if str(r.get("code")) == "200":
                    data = r.get("data")
                    if isinstance(data, dict):
                        if data.get("url"):
                            logging.info("Found stream URL for %s ch%s: %s", serial, channel, data["url"][:60])
                            return data["url"]
                        if data.get("liveAddress"):
                            logging.info("Found stream liveAddress for %s ch%s: %s", serial, channel, data["liveAddress"][:60])
                            return data["liveAddress"]
                    elif isinstance(data, list) and len(data) > 0:
                        first = data[0]
                        if isinstance(first, dict):
                            if first.get("url"):
                                return first["url"]
                            if first.get("liveAddress"):
                                return first["liveAddress"]
                    urls = find_urls(data, "http") + find_urls(data, "https") + find_urls(data, "rtsp") + find_urls(data, "rtmp")
                    if urls:
                        return urls[0]
            except Exception as e:
                logging.debug("get_open_live_url ep=%s proto=%s failed: %s", ep, proto, e)
    return None



DEFAULT_PROFILE = {
    "featureCode": "358721094812395b00c28471e98d12a4",
    "clientType": "3",  # 3: Android App
    "osVersion": "12",
    "clientVersion": "v6.11.0",
    "netType": "WIFI",
    "cuName": "Xiaomi 22011211C",
    "customno": "1000001",
    "clientNo": "web_site",
    "appId": "ys7",
    "language": "zh_CN",
    "User-Agent": "okhttp/3.12.1",
}


def build_app_headers(session_id=""):
    headers = {
        "User-Agent": DEFAULT_PROFILE["User-Agent"],
        "clientType": DEFAULT_PROFILE["clientType"],
        "osVersion": DEFAULT_PROFILE["osVersion"],
        "clientVersion": DEFAULT_PROFILE["clientVersion"],
        "netType": DEFAULT_PROFILE["netType"],
        "featureCode": DEFAULT_PROFILE["featureCode"],
        "cuName": DEFAULT_PROFILE["cuName"],
        "appId": DEFAULT_PROFILE["appId"],
        "language": DEFAULT_PROFILE["language"],
    }
    if session_id:
        headers["ssid"] = session_id
        headers["sessionId"] = session_id
    return headers


def post_json_api(url, data_dict, headers=None):
    post_data = urllib.parse.urlencode(data_dict).encode("utf-8")
    req = urllib.request.Request(url, data=post_data)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            logging.info("POST %s -> %s", url, content[:300])
            return json.loads(content)
    except urllib.error.HTTPError as e:
        err_raw = e.read().decode("utf-8", errors="replace")
        logging.error("POST %s failed (%d): %s", url, e.code, err_raw)
        try:
            err_json = json.loads(err_raw)
            msg = err_json.get("meta", {}).get("message") or err_json.get("message")
            if msg:
                raise RuntimeError(msg)
        except Exception as parse_ex:
            if not isinstance(parse_ex, RuntimeError):
                pass
            else:
                raise parse_ex
        raise RuntimeError(f"HTTP {e.code}: {err_raw[:120]}")


def get_json_api(url, headers=None):
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            logging.info("GET %s -> %s", url, content[:300])
            return json.loads(content)
    except urllib.error.HTTPError as e:
        err_raw = e.read().decode("utf-8", errors="replace")
        logging.error("GET %s failed (%d): %s", url, e.code, err_raw)
        try:
            err_json = json.loads(err_raw)
            msg = err_json.get("meta", {}).get("message") or err_json.get("message")
            if msg:
                raise RuntimeError(msg)
        except Exception as parse_ex:
            if not isinstance(parse_ex, RuntimeError):
                pass
            else:
                raise parse_ex
        raise RuntimeError(f"HTTP {e.code}: {err_raw[:120]}")



def load_saved_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error("Failed to read %s: %s", TOKEN_FILE, e)
    return None


def save_token_data(data):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def reload_go2rtc():
    try:
        req = urllib.request.Request(f"{GO2RTC_API}/api/restart", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            logging.info("go2rtc hot-reload succeeded.")
            return True
    except Exception:
        pass
    try:
        subprocess.call(["systemctl", "restart", "go2rtc"])
        return True
    except Exception as e:
        logging.warning("systemctl restart go2rtc failed: %s", e)
    return False


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>萤石摄像头 · ONVIF 网关控制台</title>
    <style>
        :root {
            --bg-body: #090d16;
            --bg-card: #131a29;
            --bg-input: #1a2336;
            --border: #242f45;
            --accent: #1f6feb;
            --accent-green: #238636;
            --accent-red: #da3633;
            --text-main: #f0f6fc;
            --text-sub: #8b949e;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg-body);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        header {
            background-color: #05080e;
            border-bottom: 1px solid var(--border);
            padding: 16px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .brand { display: flex; align-items: center; gap: 12px; font-size: 1.25rem; font-weight: 600; }
        .dot { width: 12px; height: 12px; border-radius: 50%; background: #3fb950; display: inline-block; }
        .dot.offline { background: #8b949e; }
        .container { max-width: 1080px; width: 100%; margin: 24px auto; padding: 0 16px; display: flex; flex-direction: column; gap: 24px; }
        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
        }
        .card-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
        .tabs { display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
        .tab-btn {
            background: none; border: none; color: var(--text-sub); font-size: 0.95rem; padding: 6px 12px; cursor: pointer; border-radius: 6px;
        }
        .tab-btn.active { color: #fff; background: var(--bg-input); font-weight: 600; }
        .form-row { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
        .form-row label { font-size: 0.85rem; color: var(--text-sub); }
        .input-group { display: flex; gap: 10px; }
        input {
            flex: 1;
            background: var(--bg-input);
            border: 1px solid var(--border);
            color: #fff;
            padding: 10px 14px;
            border-radius: 6px;
            font-size: 0.95rem;
            outline: none;
        }
        input:focus { border-color: var(--accent); }
        button {
            background: var(--accent);
            color: #fff;
            border: none;
            padding: 10px 18px;
            border-radius: 6px;
            font-size: 0.95rem;
            cursor: pointer;
            font-weight: 500;
            transition: opacity 0.2s;
        }
        button:hover { opacity: 0.9; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        button.green { background: var(--accent-green); }
        button.red { background: var(--accent-red); }
        .msg-box { padding: 12px; border-radius: 6px; font-size: 0.9rem; margin-top: 12px; display: none; }
        .msg-box.success { background: #133a20; border: 1px solid #238636; color: #7ee787; display: block; }
        .msg-box.error { background: #3d1418; border: 1px solid #da3633; color: #ff7b72; display: block; }
        
        .wall-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 12px;
            background: #05080e;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid var(--border);
            margin-top: 14px;
        }
        .wall-cell {
            position: relative;
            background: #000;
            border-radius: 6px;
            overflow: hidden;
            aspect-ratio: 16 / 9;
            border: 1px solid #1a2336;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .wall-cell iframe {
            width: 100%;
            height: 100%;
            border: none;
            background: #000;
        }
        .cell-tag {
            position: absolute;
            top: 6px;
            left: 8px;
            background: rgba(0, 0, 0, 0.75);
            color: #7ee787;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.78rem;
            font-weight: 600;
            z-index: 10;
            pointer-events: none;
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .card-player-box {
            margin-top: 10px;
            aspect-ratio: 16 / 9;
            background: #000;
            border-radius: 6px;
            overflow: hidden;
            border: 1px solid var(--border);
            display: none;
        }
        .card-player-box iframe {
            width: 100%;
            height: 100%;
            border: none;
            background: #000;
        }

        .cam-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin-top: 16px; }
        .cam-card {
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .cam-name { font-weight: 600; font-size: 1rem; display: flex; align-items: center; justify-content: space-between; }
        .badge { font-size: 0.75rem; padding: 2px 8px; border-radius: 12px; font-weight: 600; }
        .badge.online { background: #133a20; color: #7ee787; border: 1px solid #238636; }
        .badge.offline { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
        .cam-info { font-size: 0.82rem; color: var(--text-sub); display: flex; flex-direction: column; gap: 4px; }
        .cam-actions { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
        .btn-sm { padding: 6px 12px; font-size: 0.82rem; }
        .guide-box { background: #111a2e; border: 1px solid #1f3766; border-radius: 6px; padding: 16px; font-size: 0.9rem; line-height: 1.6; }
        code { background: #1e2638; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; color: #79c0ff; }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <span class="dot"></span>
            <span>萤石摄像头 · ONVIF 网关控制中心</span>
        </div>
        <div id="headerStatus" style="font-size:0.9rem; color:var(--text-sub);">
            检测服务状态中...
        </div>
    </header>

    <div class="container">

        <!-- 登录区域 -->
        <div class="card" id="loginCard">
            <div class="card-title">
                <span>🔐 登录萤石账号 (官方 App 免费通道)</span>
            </div>

            <div class="tabs">
                <button class="tab-btn active" id="tabSmsBtn" onclick="switchTab('sms')">📱 手机短信验证码登录</button>
                <button class="tab-btn" id="tabPwdBtn" onclick="switchTab('pwd')">🔑 账号密码登录</button>
                <button class="tab-btn" id="tabOpenBtn" onclick="switchTab('open')">⚡ 开放平台 AppKey 登录 (免验证码/最稳定)</button>
            </div>

            <!-- 短信登录表单 -->
            <div id="smsForm">
                <div class="form-row">
                    <label>手机号码：</label>
                    <div class="input-group">
                        <input type="text" id="smsPhone" placeholder="请输入绑定的手机号，如 13800000000">
                        <button type="button" id="btnSendCode" onclick="sendSmsCode()">获取验证码</button>
                    </div>
                </div>
                <div class="form-row">
                    <label>短信验证码：</label>
                    <input type="text" id="smsCode" placeholder="输入收到的 6 位短信验证码">
                </div>
                <div class="form-row">
                    <label>账号密码：</label>
                    <input type="password" id="smsPassword" placeholder="输入萤石账号登录密码 (萤石安全机制要求新设备需密码+短信双因子验证)">
                </div>
                <div style="font-size:0.8rem; color:var(--text-sub); margin-bottom:8px;">
                    💡 提示：若未收到短信，请检查手机是否开启了「垃圾短信拦截 / 骚扰拦截」，或切换至右侧「账号密码登录」/「开放平台登录」。
                </div>
                <button type="button" class="green" style="width:100%; margin-top:8px;" onclick="submitSmsLogin()">确认登录并同步设备</button>
            </div>

            <!-- 密码登录表单 -->
            <div id="pwdForm" style="display:none;">
                <div class="form-row">
                    <label>登录账号 (手机号/用户名)：</label>
                    <input type="text" id="pwdAccount" placeholder="输入手机号或萤石账号">
                </div>
                <div class="form-row">
                    <label>登录密码：</label>
                    <input type="password" id="pwdPassword" placeholder="输入账号密码">
                </div>
                <button type="button" class="green" style="width:100%; margin-top:8px;" onclick="submitPwdLogin()">确认登录并同步设备</button>
            </div>

            <!-- 开放平台登录表单 -->
            <div id="openForm" style="display:none;">
                <div class="form-row">
                    <label>开放平台 AppKey：</label>
                    <input type="text" id="openAppKey" placeholder="输入在 open.ys7.com 控制台创建的 AppKey">
                </div>
                <div class="form-row">
                    <label>开放平台 AppSecret：</label>
                    <input type="password" id="openAppSecret" placeholder="输入在 open.ys7.com 控制台的 Secret">
                </div>
                <div style="font-size:0.8rem; color:var(--text-sub); margin-bottom:8px;">
                    💡 提示：在您已打开的「萤石云开放平台」控制台直接复制 AppKey 与 Secret 即可，无需短信验证码，永久有效且不产生公网流量费！
                </div>
                <button type="button" class="green" style="width:100%; margin-top:8px;" onclick="submitOpenLogin()">通过开放平台凭证登录并同步设备</button>
            </div>

            <div id="loginMsg" class="msg-box"></div>
        </div>

        <!-- 已登录状态栏 -->
        <div class="card" id="userCard" style="display:none;">
            <div class="card-title">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span class="dot"></span>
                    <span id="accountLabel">已登录账号</span>
                </div>
                <div style="display:flex; gap:10px;">
                    <button class="btn-sm" onclick="syncCameras()">🔄 刷新设备列表</button>
                    <button class="btn-sm red" onclick="logout()">退出登录</button>
                </div>
            </div>
            <div id="sessionDetails" style="font-size:0.85rem; color:var(--text-sub);"></div>
        </div>

        <!-- 全部摄像头多画面实时监控墙 -->
        <div class="card" id="wallCard" style="display:none;">
            <div class="card-title">
                <div style="display:flex; align-items:center; gap:10px;">
                    <span>📺 全部摄像头同屏实时预览 (监控电视墙)</span>
                    <span class="badge online" id="wallCountBadge">5 路画面</span>
                </div>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    <a href="/preview.html" target="_blank" style="text-decoration:none;"><button class="btn-sm green" style="font-weight:700; background:#238636; border:1px solid #3fb950;">📺 极速 5 路并发 MSE 监控大屏</button></a>
                    <button class="btn-sm" onclick="playAllWall()">▶ 全部开启</button>
                    <button class="btn-sm red" onclick="stopAllWall()">⏸ 全部停止</button>
                    <button class="btn-sm" onclick="toggleFullScreenWall()">⛶ 网页全屏</button>
                </div>
            </div>
            
            <div style="background:#0e1b2e; border:1px solid #1f6feb; border-radius:6px; padding:12px 18px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                <div style="font-size:0.9rem; color:#79c0ff; display:flex; align-items:center; gap:8px;">
                    <span class="dot"></span>
                    <span><strong>官方 App 视频流通道已全量贯通</strong> (源地址: <code>10.0.0.185:8554</code> | 5 路并发支持 H.264 & H.265)</span>
                </div>
                <a href="/preview.html" target="_blank" style="text-decoration:none;">
                    <button class="btn-sm green" style="font-weight:700; padding:6px 14px;">🚀 打开 5 路全屏 MSE 监控中心</button>
                </a>
            </div>

            <div class="wall-grid" id="wallGrid">
                <!-- 动态生成 5 路实时画面 -->
            </div>
        </div>

        <!-- 发现的摄像头列表 -->
        <div class="card" id="cameraCard">
            <div class="card-title">
                <span>📹 账号名下的摄像头列表 (<span id="camCount">0</span> 台)</span>
                <a href="http://" + window.location.hostname + ":1984/" id="btnGo2rtc" target="_blank" style="text-decoration:none;">
                    <button class="btn-sm">📺 打开 go2rtc 实时全览</button>
                </a>
            </div>

            <div id="camList" class="cam-grid">
                <div style="color:var(--text-sub); grid-column: 1/-1; text-align:center; padding:20px;">
                    登录后将自动显示您账号下的所有摄像头与通道
                </div>
            </div>
        </div>

        <!-- 录像机接入指南 -->
        <div class="card">
            <div class="card-title">
                <span>📌 录像机 (NVR) 7x24 小时添加指南</span>
            </div>
            <div class="guide-box">
                <p>● <strong>自动搜索 (推荐)</strong>：在海康威视、大华、宇视等 NVR 中进入「通道管理」→「添加设备」→「搜索设备」，录像机会自动通过 WS-Discovery 搜索到 IP 为 <code>10.0.0.116</code> 的 ONVIF 设备，直接勾选添加即可。</p>
                <p style="margin-top:6px;">● <strong>手动添加</strong>：协议选择 <code>ONVIF</code>，IP 填 <code>10.0.0.116</code>，ONVIF 端口填 <code>1984</code>，用户名和密码留空即可。</p>
                <p style="margin-top:6px;">● <strong>RTSP 直连格式</strong>：<code>rtsp://10.0.0.116:8554/cam_序列号_ch1</code></p>
            </div>
        </div>

    </div>

    <script>
        let countdown = 0;
        let timer = null;

        function switchTab(type) {
            document.getElementById('tabSmsBtn').className = (type === 'sms') ? 'tab-btn active' : 'tab-btn';
            document.getElementById('tabPwdBtn').className = (type === 'pwd') ? 'tab-btn active' : 'tab-btn';
            document.getElementById('tabOpenBtn').className = (type === 'open') ? 'tab-btn active' : 'tab-btn';
            document.getElementById('smsForm').style.display = (type === 'sms') ? 'block' : 'none';
            document.getElementById('pwdForm').style.display = (type === 'pwd') ? 'block' : 'none';
            document.getElementById('openForm').style.display = (type === 'open') ? 'block' : 'none';
            showMsg('', '');
        }

        async function submitOpenLogin() {
            const appKey = document.getElementById('openAppKey').value.trim();
            const appSecret = document.getElementById('openAppSecret').value.trim();
            if (!appKey || !appSecret) { showMsg('请输入 AppKey 和 AppSecret', 'error'); return; }
            showMsg('正在向开放平台获取 Token 并同步设备...', 'success');
            try {
                const res = await fetch('/api/login/open', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ appKey, appSecret })
                });
                const data = await res.json();
                if (data.success) {
                    showMsg('登录成功！已自动获取摄像头列表', 'success');
                    loadStatus();
                } else {
                    showMsg('登录失败: ' + (data.message || '凭证错误'), 'error');
                }
            } catch (e) {
                showMsg('请求异常: ' + e, 'error');
            }
        }


        function showMsg(text, type) {
            const el = document.getElementById('loginMsg');
            if (!text) { el.style.display = 'none'; return; }
            el.className = 'msg-box ' + type;
            el.innerText = text;
            el.style.display = 'block';
        }

        async function sendSmsCode() {
            const phone = document.getElementById('smsPhone').value.trim();
            if (!phone) { showMsg('请输入手机号码', 'error'); return; }
            const btn = document.getElementById('btnSendCode');
            btn.disabled = true;
            showMsg('正在向萤石云申请发送验证码...', 'success');
            try {
                const res = await fetch('/api/sms/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ account: phone })
                });
                const data = await res.json();
                if (data.success) {
                    showMsg('验证码已发送至手机，请查收短信', 'success');
                    countdown = 60;
                    btn.innerText = countdown + '秒后可重发';
                    timer = setInterval(() => {
                        countdown--;
                        if (countdown <= 0) {
                            clearInterval(timer);
                            btn.disabled = false;
                            btn.innerText = '获取验证码';
                        } else {
                            btn.innerText = countdown + '秒后可重发';
                        }
                    }, 1000);
                } else {
                    btn.disabled = false;
                    showMsg('发送失败: ' + (data.message || '未知错误'), 'error');
                }
            } catch (e) {
                btn.disabled = false;
                showMsg('请求异常: ' + e, 'error');
            }
        }

        async function submitSmsLogin() {
            const phone = document.getElementById('smsPhone').value.trim();
            const code = document.getElementById('smsCode').value.trim();
            const password = document.getElementById('smsPassword').value.trim();
            if (!phone || !code) { showMsg('请输入手机号和收到的验证码', 'error'); return; }
            if (!password) { showMsg('请输入您的账号密码（萤石云在新客户端上采用密码+短信双因子认证）', 'error'); return; }
            showMsg('正在验证并同步设备...', 'success');
            try {
                const res = await fetch('/api/login/sms', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ account: phone, sms_code: code, password: password })
                });
                const data = await res.json();
                if (data.success) {
                    showMsg('登录成功！已自动获取摄像头列表', 'success');
                    loadStatus();
                } else {
                    showMsg('登录失败: ' + (data.message || '验证码或密码错误'), 'error');
                }
            } catch (e) {
                showMsg('请求异常: ' + e, 'error');
            }
        }

        async function submitPwdLogin() {
            const account = document.getElementById('pwdAccount').value.trim();
            const password = document.getElementById('pwdPassword').value.trim();
            if (!account || !password) { showMsg('请输入账号和密码', 'error'); return; }
            showMsg('正在登录并同步设备...', 'success');
            try {
                const res = await fetch('/api/login/pwd', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ account, password })
                });
                const data = await res.json();
                if (data.success) {
                    showMsg('登录成功！', 'success');
                    loadStatus();
                } else {
                    showMsg('登录失败: ' + (data.message || '账号或密码错误'), 'error');
                }
            } catch (e) {
                showMsg('请求异常: ' + e, 'error');
            }
        }

        let globalStatuses = {};
        let globalCustoms = {};

        async function loadStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                const host = window.location.hostname;
                document.getElementById('btnGo2rtc').href = `http://${host}:1984/`;

                if (data.logged_in) {
                    document.getElementById('loginCard').style.display = 'none';
                    document.getElementById('userCard').style.display = 'block';
                    document.getElementById('accountLabel').innerText = `已连接账号: ${data.account || '萤石用户'}`;
                    document.getElementById('sessionDetails').innerText = `SessionID: ${data.session_id_masked} | 登录时间: ${data.login_time || '近期'} | 运行节点: ${host}`;
                    document.getElementById('headerStatus').innerText = '🟢 已登录萤石官方通道';
                    globalStatuses = data.stream_statuses || {};
                    globalCustoms = data.custom_streams || {};
                    renderCameras(data.cameras || [], globalStatuses, globalCustoms);
                } else {
                    document.getElementById('loginCard').style.display = 'block';
                    document.getElementById('userCard').style.display = 'none';
                    document.getElementById('headerStatus').innerText = '⚪ 未登录';
                    renderCameras([], {}, {});
                }
            } catch (e) {
                console.error(e);
            }
        }

        function renderCameras(cameras, streamStatuses, customStreams) {
            document.getElementById('camCount').innerText = cameras.length;
            const container = document.getElementById('camList');
            const wallCard = document.getElementById('wallCard');
            const wallGrid = document.getElementById('wallGrid');
            const host = window.location.hostname;

            if (!cameras.length) {
                container.innerHTML = '<div style="color:var(--text-sub); grid-column: 1/-1; text-align:center; padding:20px;">未发现摄像头或尚未登录</div>';
                wallCard.style.display = 'none';
                return;
            }

            let activeCount = 0;
            cameras.forEach(c => {
                const sName = `cam_${c.deviceSerial}_ch${c.channelNo || 1}`;
                const st = (streamStatuses && streamStatuses[sName]) || {};
                if (st.type && st.type !== 'none') activeCount++;
            });

            wallCard.style.display = 'block';
            document.getElementById('wallCountBadge').innerText = `${cameras.length} 路设备 (${activeCount} 路出流就绪)`;

            // 渲染全部摄像头多画面实时监控墙
            wallGrid.innerHTML = cameras.map(c => {
                const streamName = `cam_${c.deviceSerial}_ch${c.channelNo || 1}`;
                const st = (streamStatuses && streamStatuses[streamName]) || {};
                let statusBadge = '<span class="badge" style="background:#4d3800; color:#e3b341; border:1px solid #9e6a03;">🟡 待配视频源</span>';
                if (st.type === 'app') {
                    statusBadge = '<span class="badge online" style="background:#133a20; color:#7ee787; border:1px solid #238636;">📱 官方 App 通道</span>';
                } else if (st.type === 'open') {
                    statusBadge = '<span class="badge online">🟢 官方云直连</span>';
                } else if (st.type === 'custom') {
                    statusBadge = '<span class="badge online" style="background:#0c2d6b; color:#58a6ff; border:1px solid #1f6feb;">🔵 内网直连</span>';
                }

                return `
                <div class="wall-cell" id="wall_cell_${streamName}">
                    <div class="cell-tag">
                        <span class="dot" style="width:7px; height:7px;"></span>
                        <span>${c.deviceName || c.deviceSerial} (通道 ${c.channelNo || 1})</span>
                        ${statusBadge}
                    </div>
                    <iframe id="wall_frame_${streamName}" src="http://${host}:1984/stream.html?src=${streamName}&mode=mse,webrtc" allowfullscreen></iframe>
                </div>
                `;
            }).join('');

            // 渲染每个摄像头卡片
            container.innerHTML = cameras.map(c => {
                const isOnline = (String(c.status) === '1');
                const streamName = `cam_${c.deviceSerial}_ch${c.channelNo || 1}`;
                const rtspUrl = `rtsp://${host}:8554/${streamName}`;
                const st = (streamStatuses && streamStatuses[streamName]) || {};
                const customVal = (customStreams && customStreams[streamName]) || '';
                
                let streamSourceText = '🟡 占位回环 (等待官方 AppKey 激活或配置本地 RTSP)';
                let cardBadge = '<span class="badge" style="background:#4d3800; color:#e3b341; border:1px solid #9e6a03;">待配视频源</span>';
                if (st.type === 'app') {
                    streamSourceText = '📱 官方手机 App 直连通道: ' + (st.url || 'rtsp://10.0.0.185:8554');
                    cardBadge = '<span class="badge online" style="background:#133a20; color:#7ee787; border:1px solid #238636;">官方 App 通道</span>';
                } else if (st.type === 'open') {
                    streamSourceText = '🟢 萤石云官方直连码流 (已生效)';
                    cardBadge = '<span class="badge online">官方云直连</span>';
                } else if (st.type === 'custom') {
                    streamSourceText = '🔵 自定义/本地 RTSP: ' + customVal;
                    cardBadge = '<span class="badge online" style="background:#0c2d6b; color:#58a6ff; border:1px solid #1f6feb;">内网直连</span>';
                }

                return `
                <div class="cam-card">
                    <div class="cam-name">
                        <span>${c.deviceName || c.deviceSerial}</span>
                        <div style="display:flex; gap:6px;">
                            ${cardBadge}
                            <span class="badge ${isOnline ? 'online' : 'offline'}">${isOnline ? '在线' : '离线'}</span>
                        </div>
                    </div>
                    <div class="cam-info">
                        <div>序列号: <code>${c.deviceSerial}</code> (通道 ${c.channelNo || 1})</div>
                        <div>视频加密: ${c.isEncrypt === 1 ? '🔒 已加密 (AES-128)' : '🔓 未加密'}</div>
                        <div>当前视频源: <span style="color:#79c0ff; word-break:break-all;">${streamSourceText}</span></div>
                        <div>对外 RTSP 地址: <code style="word-break:break-all;">${rtspUrl}</code></div>
                    </div>
                    <div class="card-player-box" id="card_player_${streamName}">
                        <iframe id="card_frame_${streamName}" src="" allowfullscreen></iframe>
                    </div>
                    <div class="cam-actions">
                        <button class="btn-sm green" id="btn_card_${streamName}" onclick="toggleCardPlay('${streamName}')">▶ 预览本路</button>
                        <a href="http://${host}:1984/stream.html?src=${streamName}&mode=mse,webrtc" target="_blank" style="text-decoration:none;">
                            <button class="btn-sm">新窗口大图</button>
                        </a>
                        <button class="btn-sm" onclick="navigator.clipboard.writeText('${rtspUrl}'); alert('已复制 RTSP 播放地址: ${rtspUrl}');">📋 复制 RTSP</button>
                        <button class="btn-sm" onclick="toggleCustomBox('${streamName}')">⚙️ 配置本地/自定义源</button>
                    </div>
                    <div id="custom_box_${streamName}" style="display:none; margin-top:10px; padding:10px; background:#0b101b; border:1px solid var(--border); border-radius:6px;">
                        <div style="font-size:0.8rem; color:var(--text-sub); margin-bottom:6px;">
                            配置此通道的视频源 (可填内网 RTSP，如 <code>rtsp://admin:pass@10.0.0.xxx:554/Streaming/Channels/301</code> 或 FLV 地址，留空重置)：
                        </div>
                        <div style="display:flex; gap:6px;">
                            <input type="text" id="input_custom_${streamName}" value="${customVal}" placeholder="输入 RTSP / FLV 源地址">
                            <button class="btn-sm green" onclick="saveCustomStream('${streamName}')">保存并生效</button>
                        </div>
                    </div>
                </div>
                `;
            }).join('');
        }

        function toggleCustomBox(sName) {
            const el = document.getElementById('custom_box_' + sName);
            el.style.display = (el.style.display === 'none' || !el.style.display) ? 'block' : 'none';
        }

        async function saveCustomStream(sName) {
            const input = document.getElementById('input_custom_' + sName);
            const url = input.value.trim();
            try {
                const res = await fetch('/api/camera/stream/set', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ stream_name: sName, stream_url: url })
                });
                const d = await res.json();
                if (d.success) {
                    alert('已保存并重新加载流源！');
                    loadStatus();
                } else {
                    alert('保存失败: ' + d.message);
                }
            } catch (e) {
                alert('请求异常: ' + e);
            }
        }

        function playAllWall() {
            const host = window.location.hostname;
            document.querySelectorAll('[id^="wall_frame_"]').forEach(el => {
                const sName = el.id.replace('wall_frame_', '');
                el.src = `http://${host}:1984/stream.html?src=${sName}&mode=mse,webrtc`;
            });
        }

        function stopAllWall() {
            document.querySelectorAll('[id^="wall_frame_"]').forEach(el => {
                el.src = 'about:blank';
            });
        }

        function toggleFullScreenWall() {
            const el = document.getElementById('wallGrid');
            if (!document.fullscreenElement) {
                el.requestFullscreen().catch(err => alert('全屏请求失败: ' + err.message));
            } else {
                document.exitFullscreen();
            }
        }

        function toggleCardPlay(streamName) {
            const host = window.location.hostname;
            const box = document.getElementById('card_player_' + streamName);
            const frame = document.getElementById('card_frame_' + streamName);
            const btn = document.getElementById('btn_card_' + streamName);
            if (box.style.display === 'none' || !box.style.display) {
                box.style.display = 'block';
                frame.src = `http://${host}:1984/stream.html?src=${streamName}&mode=mse,webrtc`;
                btn.innerText = '⏸ 关闭预览';
                btn.className = 'btn-sm red';
            } else {
                box.style.display = 'none';
                frame.src = 'about:blank';
                btn.innerText = '▶ 预览本路';
                btn.className = 'btn-sm green';
            }
        }

        async function activateOpenStreams() {
            const appKey = document.getElementById('actAppKey').value.trim();
            const appSecret = document.getElementById('actAppSecret').value.trim();
            const msgEl = document.getElementById('actMsg');
            if (!appKey || !appSecret) {
                msgEl.style.display = 'block';
                msgEl.style.color = '#ff7b72';
                msgEl.innerText = '请输入在 open.ys7.com 控制台复制的 AppKey 和 Secret';
                return;
            }
            msgEl.style.display = 'block';
            msgEl.style.color = '#7ee787';
            msgEl.innerText = '正在获取官方直连码流并激活播放矩阵...';
            try {
                const res = await fetch('/api/activate_open', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ appKey, appSecret })
                });
                const data = await res.json();
                if (data.success) {
                    msgEl.innerText = '✅ 激活成功！全部 5 路摄像头视频源已绑定并启动，正在加载实时画面...';
                    setTimeout(() => { 
                        loadStatus();
                        playAllWall(); 
                    }, 1200);
                } else {
                    msgEl.style.color = '#ff7b72';
                    msgEl.innerText = '激活失败: ' + (data.message || '凭证错误');
                }
            } catch (e) {
                msgEl.style.color = '#ff7b72';
                msgEl.innerText = '请求异常: ' + e;
            }
        }

        async function syncCameras() {
            showMsg('正在向萤石云同步最新设备列表...', 'success');
            try {
                const res = await fetch('/api/sync', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    alert('同步完成！已发现并映射 ' + data.count + ' 台摄像头');
                    loadStatus();
                } else {
                    alert('同步失败: ' + data.message);
                }
            } catch (e) {
                alert('请求异常: ' + e);
            }
        }

        async function logout() {
            if (!confirm('确定要退出当前萤石账号吗？')) return;
            try {
                await fetch('/api/logout', { method: 'POST' });
                loadStatus();
            } catch (e) {
                console.error(e);
            }
        }

        // 初始化加载
        loadStatus();
    </script>
</body>
</html>
"""


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logging.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        clean_path = parsed.path.lstrip("/")
        local_file = os.path.join(base_dir, clean_path)

        if clean_path and os.path.isfile(local_file):
            ctype = "text/plain"
            if clean_path.endswith(".html"): ctype = "text/html; charset=utf-8"
            elif clean_path.endswith(".js"): ctype = "application/javascript; charset=utf-8"
            elif clean_path.endswith(".css"): ctype = "text/css; charset=utf-8"
            elif clean_path.endswith(".ico"): ctype = "image/x-icon"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            with open(local_file, "rb") as f:
                content = f.read()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/status":
            token = load_saved_token()
            if not token or not token.get("session_id"):
                self.send_json({"logged_in": False})
                return
            session_id = token["session_id"]
            masked = session_id[:6] + "******" if len(session_id) > 6 else session_id
            cams = token.get("devices") or []
            self.send_json({
                "logged_in": True,
                "account": token.get("account"),
                "session_id_masked": masked,
                "login_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(token.get("login_time", time.time()))),
                "cameras": cams,
                "custom_streams": token.get("custom_streams") or {},
                "stream_statuses": token.get("stream_statuses") or {},
                "has_open_token": bool(token.get("access_token")),
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            params = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            params = {}

        if parsed.path == "/api/sms/send":
            account = str(params.get("account", "")).strip()
            if not account:
                self.send_json({"success": False, "message": "手机号码不能为空"})
                return
            headers = build_app_headers()
            data = {"from": account, "msgType": "1", "bizType": "USER_LOGIN"}
            try:
                res = post_json_api(f"{API_BASE_APP}/v3/users/checkcode/mt/unlogin", data, headers=headers)
                meta = res.get("meta", {})
                code = str(meta.get("code", "200"))
                if code == "200":
                    self.send_json({"success": True})
                elif code == "1042":
                    self.send_json({"success": False, "message": "短信发送频繁，请稍候2分钟后重试，或直接使用账号密码登录！"})
                else:
                    self.send_json({"success": False, "message": meta.get("message") or f"发送失败(code {code})"})
            except Exception as e:
                logging.exception("SMS send exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/login/sms":
            account = str(params.get("account", "")).strip()
            code = str(params.get("sms_code", "")).strip()
            password = str(params.get("password", "")).strip()
            if not account:
                self.send_json({"success": False, "message": "手机号不能为空"})
                return

            headers = build_app_headers()
            payload = {
                "account": account,
                "msgType": "0",
                "bizType": "USER_LOGIN",
                "featureCode": DEFAULT_PROFILE["featureCode"],
                "cuName": DEFAULT_PROFILE["cuName"],
                "clientType": DEFAULT_PROFILE["clientType"],
                "netType": DEFAULT_PROFILE["netType"],
            }
            if code:
                payload["smsCode"] = code
            if password:
                payload["password"] = hashlib.md5(password.encode("utf-8")).hexdigest()
            try:
                res = post_json_api(f"{API_BASE_APP}/v3/users/login/v2", payload, headers=headers)
                meta = res.get("meta", {})
                m_code = str(meta.get("code", "200"))
                if m_code not in ("200", "0"):
                    self.send_json({"success": False, "message": meta.get("message", "登录失败")})
                    return

                session_info = res.get("sessionInfo") or {}
                user_data = res.get("userInfos") or res.get("data") or {}
                session_id = (
                    res.get("sessionId")
                    or session_info.get("sessionId")
                    or user_data.get("sessionId")
                    or user_data.get("token")
                )
                if not session_id:
                    self.send_json({"success": False, "message": "未获取到有效 SessionID，返回: " + json.dumps(res, ensure_ascii=False)[:200]})
                    return

                cams = []
                try:
                    cams = self.fetch_devices(session_id)
                except Exception as ex:
                    logging.warning("fetch_devices warning: %s", ex)

                saved_data = {
                    "session_id": session_id,
                    "user_id": user_data.get("userId"),
                    "account": account,
                    "profile": DEFAULT_PROFILE,
                    "login_time": int(time.time()),
                    "devices": cams,
                }
                save_token_data(saved_data)
                self.update_go2rtc_streams(cams)
                self.send_json({"success": True, "count": len(cams)})
            except Exception as e:
                logging.exception("SMS login exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/login/pwd":
            account = str(params.get("account", "")).strip()
            password = str(params.get("password", "")).strip()
            if not account or not password:
                self.send_json({"success": False, "message": "账号或密码不能为空"})
                return

            headers = build_app_headers()
            pwd_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
            payload = {
                "account": account,
                "password": pwd_md5,
                "msgType": "0",
                "featureCode": DEFAULT_PROFILE["featureCode"],
                "cuName": DEFAULT_PROFILE["cuName"],
                "clientType": DEFAULT_PROFILE["clientType"],
                "netType": DEFAULT_PROFILE["netType"],
            }
            try:
                res = post_json_api(f"{API_BASE_APP}/v3/users/login/v2", payload, headers=headers)
                meta = res.get("meta", {})
                m_code = str(meta.get("code", "200"))
                if m_code not in ("200", "0"):
                    self.send_json({"success": False, "message": meta.get("message", "账号或密码错误")})
                    return

                session_info = res.get("sessionInfo") or {}
                user_data = res.get("userInfos") or res.get("data") or {}
                session_id = (
                    res.get("sessionId")
                    or session_info.get("sessionId")
                    or user_data.get("sessionId")
                    or user_data.get("token")
                )

                if (res.get("needTerminalValidate") or res.get("terminalValidate")) and not session_id:
                    self.send_json({
                        "success": False,
                        "message": "萤石安全防护：检测到新客户端首次登录，需要短信验证码。请切换至「手机短信验证码登录」标签页获取短信登录！"
                    })
                    return

                if not session_id:
                    self.send_json({
                        "success": False,
                        "message": "登录未返回 SessionID: " + json.dumps(res, ensure_ascii=False)[:200]
                    })
                    return

                cams = []
                try:
                    cams = self.fetch_devices(session_id)
                except Exception as ex:
                    logging.warning("fetch_devices warning: %s", ex)

                saved_data = {
                    "session_id": session_id,
                    "user_id": user_data.get("userId"),
                    "account": account,
                    "profile": DEFAULT_PROFILE,
                    "login_time": int(time.time()),
                    "devices": cams,
                }
                save_token_data(saved_data)
                self.update_go2rtc_streams(cams)
                self.send_json({"success": True, "count": len(cams)})
            except Exception as e:
                logging.exception("Password login exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/login/open":
            app_key = str(params.get("appKey", "")).strip()
            app_secret = str(params.get("appSecret", "")).strip()
            if not app_key or not app_secret:
                self.send_json({"success": False, "message": "AppKey 或 AppSecret 不能为空"})
                return

            try:
                logging.info("Requesting accessToken from open.ys7.com...")
                token_res = post_json_api(
                    f"{API_BASE_OPEN}/api/lapp/token/get",
                    {"appKey": app_key, "appSecret": app_secret}
                )
                if str(token_res.get("code")) != "200":
                    self.send_json({"success": False, "message": token_res.get("msg", "获取 AccessToken 失败")})
                    return
                token_data = token_res.get("data") or {}
                access_token = token_data.get("accessToken")
                if not access_token:
                    self.send_json({"success": False, "message": "返回的 AccessToken 为空"})
                    return

                logging.info("AccessToken obtained! Querying device list...")
                dev_res = post_json_api(
                    f"{API_BASE_OPEN}/api/lapp/device/list",
                    {"accessToken": access_token, "pageStart": 0, "pageSize": 50}
                )
                cams = []
                raw_devs = dev_res.get("data") or []
                for d in raw_devs:
                    serial = str(d.get("deviceSerial", "")).strip().upper()
                    if not serial:
                        continue
                    name = d.get("deviceName", serial)
                    st = d.get("status", 1)
                    try:
                        cam_res = post_json_api(
                            f"{API_BASE_OPEN}/api/lapp/camera/list",
                            {"accessToken": access_token, "deviceSerial": serial}
                        )
                        sub_cams = cam_res.get("data") or []
                    except Exception:
                        sub_cams = []

                    if sub_cams:
                        for sc in sub_cams:
                            ch = sc.get("channelNo", 1)
                            cname = sc.get("channelName") or name
                            cams.append({
                                "deviceSerial": serial,
                                "deviceName": cname,
                                "channelNo": int(ch),
                                "status": int(st),
                                "isEncrypt": int(sc.get("isEncrypt", 0)),
                            })
                    else:
                        cams.append({
                            "deviceSerial": serial,
                            "deviceName": name,
                            "channelNo": 1,
                            "status": int(st),
                            "isEncrypt": 0,
                        })

                saved_data = {
                    "auth_mode": "open",
                    "session_id": access_token,
                    "access_token": access_token,
                    "app_key": app_key,
                    "app_secret": app_secret,
                    "account": f"开放平台 ({app_key[:6]}...)",
                    "login_time": int(time.time()),
                    "devices": cams,
                }
                save_token_data(saved_data)
                self.update_go2rtc_streams(cams, access_token=access_token)
                self.send_json({"success": True, "count": len(cams)})
            except Exception as e:
                logging.exception("Open platform login exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/activate_open":
            app_key = str(params.get("appKey", "")).strip()
            app_secret = str(params.get("appSecret", "")).strip()
            if not app_key or not app_secret:
                self.send_json({"success": False, "message": "AppKey 或 AppSecret 不能为空"})
                return

            try:
                logging.info("Requesting accessToken from open.ys7.com to activate live streams...")
                token_res = post_json_api(
                    f"{API_BASE_OPEN}/api/lapp/token/get",
                    {"appKey": app_key, "appSecret": app_secret}
                )
                if str(token_res.get("code")) != "200":
                    self.send_json({"success": False, "message": token_res.get("msg", "获取 AccessToken 失败")})
                    return
                token_data = token_res.get("data") or {}
                access_token = token_data.get("accessToken")
                if not access_token:
                    self.send_json({"success": False, "message": "返回的 AccessToken 为空"})
                    return

                saved = load_saved_token() or {}
                saved["access_token"] = access_token
                saved["app_key"] = app_key
                saved["app_secret"] = app_secret
                devices = saved.get("devices") or []
                self.update_go2rtc_streams(devices, access_token=access_token)
                save_token_data(saved)
                logging.info("Successfully activated live streams for %d devices with open platform token!", len(devices))
                self.send_json({"success": True, "count": len(devices)})
            except Exception as e:
                logging.exception("activate_open exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/sync":
            token = load_saved_token()
            if not token or not token.get("session_id"):
                self.send_json({"success": False, "message": "尚未登录"})
                return
            try:
                if token.get("auth_mode") == "open":
                    access_token = token.get("access_token") or token.get("session_id")
                    dev_res = post_json_api(
                        f"{API_BASE_OPEN}/api/lapp/device/list",
                        {"accessToken": access_token, "pageStart": 0, "pageSize": 50}
                    )
                    cams = []
                    for d in dev_res.get("data") or []:
                        serial = str(d.get("deviceSerial", "")).strip().upper()
                        if not serial: continue
                        name = d.get("deviceName", serial)
                        st = d.get("status", 1)
                        cams.append({
                            "deviceSerial": serial,
                            "deviceName": name,
                            "channelNo": 1,
                            "status": int(st),
                            "isEncrypt": 0,
                        })
                    token["devices"] = cams
                    save_token_data(token)
                    self.update_go2rtc_streams(cams, access_token=access_token)
                    self.send_json({"success": True, "count": len(cams)})
                else:
                    cams = self.fetch_devices(token["session_id"])
                    token["devices"] = cams
                    save_token_data(token)
                    self.update_go2rtc_streams(cams)
                    self.send_json({"success": True, "count": len(cams)})
            except Exception as e:
                logging.exception("Sync exception: %s", e)
                self.send_json({"success": False, "message": str(e)})
            return

        if parsed.path == "/api/camera/stream/set":
            stream_name = str(params.get("stream_name", "")).strip()
            stream_url = str(params.get("stream_url", "")).strip()
            if not stream_name:
                self.send_json({"success": False, "message": "通道标识 stream_name 不能为空"})
                return
            saved = load_saved_token() or {}
            custom = saved.get("custom_streams") or {}
            if stream_url:
                custom[stream_name] = stream_url
            else:
                custom.pop(stream_name, None)
            saved["custom_streams"] = custom
            save_token_data(saved)
            self.update_go2rtc_streams(saved.get("devices") or [])
            self.send_json({"success": True, "stream_name": stream_name, "stream_url": stream_url})
            return

        if parsed.path == "/api/logout":
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            self.send_json({"success": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def fetch_devices(self, session_id):
        headers = build_app_headers(session_id=session_id)
        endpoints = [
            f"{API_BASE_APP}/v3/devices/resources?limit=50&offset=0",
            f"{API_BASE_APP}/v3/devices/info",
            f"{API_BASE_APP}/v3/userdevices/user",
            f"{API_BASE_APP}/v3/userdevices/v2/devices/search",
        ]
        devices = []
        for url in endpoints:
            try:
                logging.info("Querying device endpoint: %s", url)
                data = get_json_api(url, headers=headers)
                meta = data.get("meta", {})
                if str(meta.get("code", "200")) in ("200", "0"):
                    raw_list = (
                        data.get("resources")
                        or data.get("deviceInfos")
                        or data.get("data")
                        or data.get("devices")
                        or []
                    )
                    if isinstance(raw_list, list) and raw_list:
                        for item in raw_list:
                            serial = item.get("deviceSerial") or item.get("subSerial") or item.get("devSerial")
                            if not serial:
                                continue
                            name = item.get("deviceName") or item.get("name") or serial
                            ch = item.get("channelNo") or item.get("channel") or 1
                            st = item.get("status") if item.get("status") is not None else item.get("onlineStatus", 1)
                            enc = item.get("isEncrypt") or item.get("encryptStatus") or 0
                            devices.append({
                                "deviceSerial": str(serial).strip().upper(),
                                "deviceName": str(name).strip(),
                                "channelNo": int(ch),
                                "status": int(st),
                                "isEncrypt": int(enc),
                            })
                        if devices:
                            logging.info("Successfully fetched %d devices from %s", len(devices), url)
                            break
            except Exception as e:
                logging.warning("Query failed on %s: %s", url, e)
        return devices

    def update_go2rtc_streams(self, devices, access_token=None):
        saved = load_saved_token() or {}
        custom_streams = saved.get("custom_streams") or {}
        active_token = access_token or saved.get("access_token")

        lines = [
            "api:",
            '  listen: ":1984"',
            "rtsp:",
            '  listen: ":8554"',
            "webrtc:",
            '  listen: ":8555"',
            "log:",
            "  level: info",
            "",
            "streams:",
        ]
        statuses = {}
        for d in devices:
            serial = str(d.get("deviceSerial", "")).strip().upper()
            ch = int(d.get("channelNo", 1))
            name = d.get("deviceName", serial)
            clean_name = re.sub(r"[^0-9A-Za-z_\-]", "_", f"cam_{serial}_ch{ch}")
            lines.append(f"  # {name} (Serial: {serial})")
            lines.append(f"  {clean_name}:")
            
            stream_url = None
            source_type = "none"
            # 1. Custom / Local RTSP
            if clean_name in custom_streams and custom_streams[clean_name]:
                stream_url = custom_streams[clean_name]
                source_type = "custom"
            # 2. Open Platform direct stream
            elif active_token and saved.get("auth_mode") == "open":
                stream_url = get_open_live_url(active_token, serial, ch)
                if stream_url:
                    source_type = "open"

            # 3. 官方 App 通道 (10.0.0.185:8554 - 手机 App 网关)
            if not stream_url:
                app_gw = os.environ.get("APP_GATEWAY", "10.0.0.185:8554")
                serial_map = {
                    "D43521312_1": "cam0",
                    "D43521312": "cam0",
                    "C07585929_3": "cam1",
                    "C07585929_4": "cam2",
                    "C07585929_5": "cam3",
                    "BB8930323_1": "cam4",
                    "BB8930323": "cam4",
                }
                k = f"{serial}_{ch}"
                cam_track = serial_map.get(k) or serial_map.get(serial)
                if cam_track:
                    stream_url = f"rtsp://{app_gw}/{cam_track}"
                    source_type = "app"

            if stream_url:
                lines.append(f'    - "{stream_url}"')
            else:
                lines.append(f'    - "rtsp://127.0.0.1:8554/{clean_name}"')
            statuses[clean_name] = {
                "url": stream_url or "",
                "type": source_type
            }

        # 挂载 cam0..cam4 直通别名，适配 preview.html 与原生客户端
        lines.append("  # 官方 App 通道直接别名")
        app_gw = os.environ.get("APP_GATEWAY", "10.0.0.185:8554")
        for i in range(5):
            lines.append(f"  cam{i}:")
            lines.append(f'    - "rtsp://{app_gw}/cam{i}"')

        saved["stream_statuses"] = statuses
        save_token_data(saved)

        content = "\n".join(lines) + "\n"
        with open(GO2RTC_YAML, "w", encoding="utf-8") as f:
            f.write(content)
        reload_go2rtc()



def main():
    parser = argparse.ArgumentParser(description="Ezviz Web Portal Server")
    parser.add_argument("--port", type=int, default=PORT, help="Port to listen on (default 80)")
    args = parser.parse_args()

    server_address = ("", args.port)
    saved = load_saved_token()
    if saved and saved.get("devices"):
        try:
            logging.info("Initializing go2rtc streams with %d saved devices from App channel...", len(saved["devices"]))
            GatewayHandler.update_go2rtc_streams(None, saved.get("devices"))
        except Exception as e:
            logging.warning("Startup update_go2rtc_streams failed: %s", e)

    httpd = ThreadingHTTPServer(server_address, GatewayHandler)
    logging.info("Ezviz Gateway Web Portal listening on port %d...", args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping Web Portal...")
        httpd.server_close()


if __name__ == "__main__":
    main()
