#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ezviz (萤石) Pure-Linux App Channel Login & Session Manager
Allows logging into api.ys7.com using mobile credentials directly on Linux,
saving session tokens to /opt/ezviz2onvif/token.json for lightweight 7x24 streaming.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_BASE = "https://api.ys7.com"

# Simulated Android Client Profile (Extracted from official ys7-v6.11)
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


def build_headers(profile, session_id=""):
    headers = {
        "User-Agent": profile["User-Agent"],
        "clientType": profile["clientType"],
        "osVersion": profile["osVersion"],
        "clientVersion": profile["clientVersion"],
        "netType": profile["netType"],
        "featureCode": profile["featureCode"],
        "cuName": profile["cuName"],
        "appId": profile["appId"],
        "language": profile["language"],
    }
    if session_id:
        headers["ssid"] = session_id
        headers["sessionId"] = session_id
    return headers


def send_sms_code(account, profile):
    """Request SMS verification code from api.ys7.com"""
    url = f"{API_BASE}/v3/sms/nologin/checkcode"
    headers = build_headers(profile)
    data = urllib.parse.urlencode({
        "account": account,
        "bizType": "USER_LOGIN",
        "featureCode": profile["featureCode"],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            logging.info("SMS Code request result: %s", res)
            return res
    except Exception as e:
        logging.error("Failed to request SMS code: %s", e)
        return None


def login_app(account, password=None, sms_code=None, profile=None):
    """Login to api.ys7.com using App protocol"""
    if profile is None:
        profile = DEFAULT_PROFILE

    url = f"{API_BASE}/v3/users/login/v2"
    headers = build_headers(profile)

    payload = {
        "account": account,
        "featureCode": profile["featureCode"],
        "cuName": profile["cuName"],
        "clientType": profile["clientType"],
        "netType": profile["netType"],
    }

    if password:
        # App sends MD5 hex of the password
        pwd_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        payload["password"] = pwd_md5
        payload["msgType"] = "0"
    elif sms_code:
        payload["smsCode"] = str(sms_code)
        payload["msgType"] = "3"
        payload["bizType"] = "TERMINAL_BIND"

    post_data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, headers=headers)

    logging.info("Connecting to %s...", url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            meta = data.get("meta", {})
            if str(meta.get("code")) != "200":
                logging.error("Login failed: code=%s, msg=%s", meta.get("code"), meta.get("message"))
                return None

            user_data = data.get("userInfos") or data.get("data") or {}
            session_id = data.get("sessionId") or user_data.get("sessionId") or user_data.get("token")
            logging.info("Login SUCCESSFUL! Obtained sessionId: %s", session_id[:8] + "******" if session_id else "None")
            return {
                "session_id": session_id,
                "user_id": user_data.get("userId"),
                "account": account,
                "profile": profile,
                "login_time": int(time.time()),
            }
    except Exception as e:
        logging.error("HTTP error during login: %s", e)
        return None


def list_account_cameras(session_id, profile=None):
    """Enumerate cameras and devices under this session"""
    if profile is None:
        profile = DEFAULT_PROFILE
    url = f"{API_BASE}/v3/userdevices/v2/devices/search"
    headers = build_headers(profile, session_id=session_id)
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            devices = data.get("deviceInfos") or []
            logging.info("Found %d devices under this account.", len(devices))
            for d in devices:
                logging.info("  Device: %s (Serial: %s, Status: %s, Ch: %s)",
                             d.get("deviceName"), d.get("deviceSerial"), d.get("status"), d.get("channelNo"))
            return devices
    except Exception as e:
        logging.error("Failed to query devices: %s", e)
        return []


def main():
    parser = argparse.ArgumentParser(description="Ezviz Lightweight App-Channel Login Tool")
    parser.add_argument("--account", "-u", help="Ezviz login account (mobile phone)")
    parser.add_argument("--password", "-p", help="Account password")
    parser.add_argument("--sms", "-s", help="SMS verification code")
    parser.add_argument("--send-sms", action="store_true", help="Send SMS verification code to account")
    parser.add_argument("--output", "-o", default="/opt/ezviz2onvif/token.json", help="Path to save token JSON")

    args = parser.parse_args()

    if not args.account:
        print("Usage: python3 ezviz_app_login.py -u 13800000000 -p your_password")
        print("   Or: python3 ezviz_app_login.py -u 13800000000 --send-sms")
        print("       python3 ezviz_app_login.py -u 13800000000 -s 123456")
        sys.exit(1)

    if args.send_sms:
        logging.info("Requesting SMS verification code for %s...", args.account)
        send_sms_code(args.account, DEFAULT_PROFILE)
        return

    result = login_app(args.account, password=args.password, sms_code=args.sms)
    if not result:
        sys.exit(2)

    # Query cameras
    cams = list_account_cameras(result["session_id"], DEFAULT_PROFILE)
    result["devices"] = cams

    # Save token
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logging.info("Session saved to %s.", args.output)


if __name__ == "__main__":
    main()
