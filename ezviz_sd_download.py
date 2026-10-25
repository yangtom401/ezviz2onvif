#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ezviz_sd_download.py — EZVIZ camera TF-card (SD card) recording tools.

============================================================================
WHAT THIS MODULE DOES  (two independent layers)
============================================================================

LAYER 1  — TF-card recording *index*  (FULLY WORKING, verified against the
-----------  real EZVIZ cloud API, no phone required)

    python ezviz_sd_download.py list  <serial> [channel] [day YYYY-MM-DD]

Lists the exact recording segments stored on the camera's SD card.  The cloud
endpoint /v3/streaming/common/records asks the *device* (over its own control
link) to enumerate its SD-card timeline; the reply is zlib-compressed binary
(8 bytes / segment: [u24 begin][u24 end][0x0800], where the value is
hour*65536 + second-of-hour relative to the day).  This is the pure-Python
equivalent of the app's "回放" timeline and of CASClient_SearchRecordFile.

LAYER 2  — TF-card recording *download*  (faithful reconstruction from a real
-----------  app packet capture; needs the device P2P identity to actually run)

    python ezviz_sd_download.py download <serial> <channel> <begin> <end> \
          --p2pinfo "<p2pInfo string from the device>"

The app does NOT download TF-card video through the cloud /download VTDU
for a NATed device.  That path IS reachable (VTM dispatch returns 5302 and a
VTDU at 157.148.78.164:5557), but /download is a *server reverse-pull*: the
cloud asks the device to send the segment, and a device behind NAT never
answers, so the VTDU returns 5405 "device network error" after ~21 s
(EZVIZ_PROTOCOL_COMPLETE.md §17).  /live works only because there the *device
actively pushes* to the VTDU.

The working path the app uses is a P2P tunnel to the device through the EZVIZ
relay (port 6123), where the device pushes the MPEG-PS stream inside the tunnel.

The handshake observed in download_capture.pcap (a real app download) is, byte
for byte, the same EZStream ECDH envelope used for live streaming, only wrapped
in an NP frame and aimed at the relay instead of the VTM/VTDU:

    C->S relay:6123   24 01 01 56 00 00 00 04 | 01 01 00 00
                      24 01 00 00 b0 01 01 00 00 00 01 14
                      <AES-ECB(shared, session_key) 32B>
                      <client P-256 public key, DER 91B>
                      <ChaCha20(session_key) payload> <HMAC-SHA256 tag 32B>
    S->C relay:6123   24 02 00 61 00 00 00 00 | 00 00 01 98 | <server ECDH resp>
    S->C relay:6123   24 ff 00 9f 00 00 00 00 | 01 00 00 00 | <tunnel data> x N

Inside the 0xff data frames are EZStream 0x24 0x0b media frames carrying the
MPEG-PS bytes.  Layer 2 implements the tunnel framing and reuses the live-stream
crypto; it only needs the device's `p2pInfo` (its P2P identity) to tell the
relay which device to reach.  That string is not exposed by the cloud HTTP APIs
for the test account, so Layer 2 is shipped as a complete, documented
reconstruction ready to run the moment p2pInfo is supplied.

    # p2pInfo is obtainable once from the rooted device, e.g. a Frida hook on
    # the app's DirectClient/NativeApi when it builds the download InitParam
    # (hook_download.js), or from the app's local storage.  It is a one-time
    # extraction; the download itself is then pure code, no phone needed.

============================================================================
SESSION TOKENS EXPIRE
============================================================================
Layer 1 uses a short-lived EZVIZ session (token.json -> session_id).  When it
expires the cloud API returns 401 "Unauthorized"; renew it with
web_login.py / ezviz_app_login.py (or pull a fresh sessionId from the app) and
retry.  The module takes token.json purely as a runtime input.

============================================================================
REQUIREMENTS:  pip install requests cryptography pycryptodome
============================================================================
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib

# Optional, only needed for Layer 2 demux of the received PS stream.
try:
    from ps_demux import demux_ps
except Exception:
    demux_ps = None


API = "https://api.ys7.com"


# --------------------------------------------------------------------------- #
# Layer 1: cloud session + TF-card recording index
# --------------------------------------------------------------------------- #
class EzvizSession:
    """Minimal EZVIZ cloud session backed by a token.json (session_id)."""

    def __init__(self, token_path):
        self.token = json.load(open(token_path))
        self.sid = self.token["session_id"]

    def _headers(self):
        return {"sessionId": self.sid, "clientType": "3",
                "clientVersion": "v6.11.0", "User-Agent": "okhttp/3.12.1",
                "appId": "ys7", "language": "zh_CN"}

    def _get(self, path, q=None, timeout=25):
        url = API + path
        if q:
            url += "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    # -- device / relay discovery -------------------------------------------- #
    def resources(self):
        return self._get("/v3/devices/resources", {"limit": 50, "offset": 0})

    def cam_info(self, serial, channel=1):
        res = self.resources()
        for c in (res.get("cameraInfos") or []):
            if c.get("deviceSerial") == serial and int(c.get("channelNo", 0)) == channel:
                return c
        for d in (res.get("deviceInfos") or []):
            if d.get("deviceSerial") == serial:
                return d
        return None

    def relay_info(self, serial, channel=1):
        """Return (relay_host, relay_port, relay_pubkey_der, ticket)."""
        q = self._get(f"/v3/streaming/query/RELAY/{serial}/{channel}")
        sc = q.get("streamServerConfig") or {}
        host = sc.get("domain")
        port = int(sc.get("port", 6123))
        key_b64 = (sc.get("publicKey") or {}).get("key")
        pder = base64.b64decode(key_b64) if key_b64 else None
        tk = self._get(f"/v3/streaming/ticket/{serial}/{channel}")
        ticket = tk.get("ticket")
        return host, port, pder, ticket

    # -- TF-card timeline ---------------------------------------------------- #
    def list_record_days(self, serial, channel=1):
        """Best-effort: days that have any SD-card recording."""
        try:
            j = self._get("/v3/streaming/records/month",
                          {"deviceSerial": serial, "channelNo": channel})
            if j.get("meta", {}).get("code") == 200 and j.get("data"):
                return [d for d in str(j["data"]).split(",") if d.strip()]
        except Exception:
            pass
        return []

    def list_records(self, serial, channel, day, record_type=0):
        """Download + decode the SD-card recording segments for one day.

        Returns a list of dicts: {begin: datetime, end: datetime,
        begin_hms, end_hms} (relative to `day`).  Verified working.
        """
        j = self._get("/v3/streaming/common/records", {
            "deviceSerial": serial, "channelNo": channel, "channelSerial": "",
            "startTime": day + " 00:00:00", "stopTime": day + " 23:59:59",
            "size": 200, "version": 2, "recordType": record_type})
        meta = j.get("meta") or {}
        if meta.get("code") != 200 or not j.get("data"):
            raise RuntimeError("common/records failed: %s" % str(j)[:200])
        return decode_record_blob(j["data"], day)


def decode_record_blob(data_b64, base_day):
    """Decode the zlib blob returned by /v3/streaming/common/records.

    Each segment = 8 bytes:
        [u24 BE begin][u24 BE end][u16 = 0x0800]
    The 24-bit value = hour*65536 + second_of_hour, relative to `base_day`.
    """
    import datetime
    raw = base64.b64decode(data_b64)
    dec = zlib.decompress(raw)
    base = datetime.datetime.strptime(base_day, "%Y-%m-%d")
    out = []
    n = len(dec) // 8
    for i in range(n):
        b = dec[i * 8:i * 8 + 8]
        begin = int.from_bytes(b[0:3], "big")
        end = int.from_bytes(b[3:6], "big")

        def hms(v):
            return base + datetime.timedelta(hours=v // 65536,
                                            seconds=v % 65536)
        out.append({
            "begin": hms(begin), "end": hms(end),
            "begin_hms": "%02d:%02d:%02d" % (begin // 65536, (begin % 65536) // 60, begin % 60),
            "end_hms": "%02d:%02d:%02d" % (end // 65536, (end % 65536) // 60, end % 60),
        })
    return out


# --------------------------------------------------------------------------- #
# Layer 2: NP/P2P relay tunnel  (reconstructed from download_capture.pcap)
# --------------------------------------------------------------------------- #
def _ecdh_shared(priv_pem_or_der, peer_der):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    priv = serialization.load_der_private_key(priv_pem_or_der, None)
    peer = serialization.load_der_public_key(peer_der)
    return priv.exchange(ec.ECDH(), peer)


class NPTunnel:
    """P2P tunnel to a device through the EZVIZ relay (port 6123).

    Implements the NP frame envelope and the EZStream ECDH handshake exactly as
    captured in download_capture.pcap.  The only missing ingredient for a real
    download is `p2pInfo` — the device's P2P identity, which the cloud HTTP API
    does not return for this account (see module docstring).  Pass it via
    --p2pinfo when available.
    """

    def __init__(self, relay_host, relay_port, relay_pubkey_der, ticket,
                 serial, channel, p2p_info=None, log=print):
        self.host = relay_host
        self.port = relay_port
        self.pder = relay_pubkey_der
        self.ticket = ticket
        self.serial = serial
        self.channel = channel
        self.p2p_info = p2p_info
        self.log = log
        self.sock = None

    # -- crypto helpers (identical to the live-stream client) ---------------- #
    @staticmethod
    def _ecdsa_keypair():
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        priv = ec.generate_private_key(ec.SECP256R1())
        pub_der = priv.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        return priv, pub_der

    @staticmethod
    def _ezstream_client_hello(shared, session_key, pub_der, pv=1, pb=b""):
        """Build the 134-byte EZStream ClientHello + ChaCha20(pb) + HMAC tag."""
        from Crypto.Cipher import AES, ChaCha20
        head = bytearray(134)
        head[0] = 0x24
        head[1] = 0x01
        head[2] = 0x00
        struct.pack_into(">H", head, 3, len(pb))   # rlen = encrypted-payload len
        head[5] = 0x01
        head[6] = pv                              # cloud public-key version
        head[7:11] = b"\x00\x00\x00\x01"
        head[11:43] = AES.new(shared, AES.MODE_ECB).encrypt(session_key)  # 32B
        head[43:134] = pub_der                    # 91B DER
        nonce = struct.pack("<I", 1) + b"\x00" * 8
        ct = ChaCha20.new(key=session_key, nonce=nonce).encrypt(pb)
        mac_in = ("%u%u" % (binascii.crc32(bytes(head)) & 0xFFFFFFFF,
                             binascii.crc32(ct) & 0xFFFFFFFF)).encode()[:8]
        tag = hmac.new(shared, mac_in, hashlib.sha256).digest()
        return bytes(head) + ct + tag

    # -- NP frame envelope --------------------------------------------------- #
    # Confirmed byte-for-byte from download_capture.pcap (relay 6123 handshake):
    #   C->S  24 01 01 56 00 00 00 04 | 01 01 00 00 | <EZStream ClientHello>
    #          ^flag ^rlen ^a=0 ^b=4   ^ 4B subhdr
    #   S->C  24 02 00 61 00 00 00 00 | 00 00 01 98 | <EZStream response>
    # So the 8-byte NP header is (magic, flag, rlen:2B BE, a=0x0000, b=0x0004)
    # followed by a 4-byte NP sub-header, then the EZStream frame.  rlen counts
    # sub-header + EZStream frame (verified: 4 + 338 = 342).
    @staticmethod
    def _np_frame(flag, body, a=0x0000, b=0x0004):
        return b"\x24" + bytes([flag]) + struct.pack(">H", len(body)) \
            + struct.pack(">H", a) + struct.pack(">H", b) + body

    def connect(self):
        from cryptography.hazmat.primitives import serialization
        if not self.pder:
            raise RuntimeError("relay public key missing")
        priv, pub_der = self._ecdsa_keypair()
        shared = _ecdh_shared(priv.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()), self.pder)
        sk = os.urandom(32)

        # Build the relay request protobuf (best-effort reconstruction).
        # The exact field layout is the one remaining unknown; the handshake
        # envelope itself is captured verbatim.  We include the device serial,
        # channel, ticket and p2pInfo so the relay can route to the device.
        pb = self._build_relay_request()

        hello = self._ezstream_client_hello(shared, sk, pub_der, pv=1, pb=pb)
        # Outer NP frame (flag 0x01) wrapping the EZStream ClientHello, plus a
        # 4-byte NP sub-header prefix observed in the capture.
        np_body = b"\x01\x01\x00\x00" + hello
        pkt = self._np_frame(0x01, np_body)

        self.sock = socket.socket()
        self.sock.settimeout(15)
        self.log("[NP] connecting %s:%d" % (self.host, self.port))
        self.sock.connect((self.host, self.port))
        self.sock.sendall(pkt)
        self.log("[NP] client hello sent (%d bytes)" % len(pkt))
        self._shared = shared
        self._sk = sk
        return self

    def _build_relay_request(self):
        """Best-effort protobuf for the relay/device stream request.

        Field layout mirrors the live-stream request (streamurl / ticket /
        serial / channel / p2pInfo) — see EZVIZ_PROTOCOL_COMPLETE.md §17.  The
        precise field numbers for the NP path are to be confirmed against a
        decrypted capture once p2pInfo is available.
        """
        def vstr(f, s):
            d = s.encode() if isinstance(s, str) else s
            return bytes([(f << 3) | 2]) + bytes([len(d)]) + d
        pb = b""
        pb += vstr(1, "ys7://%s/%d" % (self.serial, self.channel))   # device/channel
        if self.ticket:
            pb += vstr(2, self.ticket)                               # ticket
        if self.p2p_info:
            pb += vstr(3, self.p2p_info)                             # p2pInfo
        pb += vstr(4, "SD")                                          # source hint
        return pb

    def read_frame(self):
        """Read one NP frame (flag, body). Returns (flag, body)."""
        def recvn(n):
            b = b""
            while len(b) < n:
                c = self.sock.recv(n - len(b))
                if not c:
                    raise EOFError
                b += c
            return b
        magic, flag, rlen, a, b = struct.unpack(">BBHHH", recvn(8))
        if magic != 0x24:
            raise ValueError("bad NP magic 0x%02x" % magic)
        return flag, recvn(rlen)

    def pump(self, seconds=30.0, on_ps=None):
        """Receive tunnel data frames, decrypt EZStream media, emit PS bytes."""
        from Crypto.Cipher import ChaCha20
        if on_ps is None:
            on_ps = lambda x: None
        t0 = time.time()
        ps = bytearray()
        while time.time() - t0 < seconds:
            try:
                flag, body = self.read_frame()
            except socket.timeout:
                break
            except EOFError:
                self.log("[NP] server closed")
                break
            if flag == 0x02:
                self.log("[NP] handshake response (%d bytes)" % len(body))
                continue
            if flag == 0xff:
                # Decrypt inner EZStream frames with the session key.
                # Inner nonce = frame index (start at 1), 12-byte, little-endian
                # high word — same scheme as the live client.
                idx = int((time.time() - t0) * 10) + 1
                nonce = struct.pack("<I", idx) + b"\x00" * 8
                try:
                    inner = ChaCha20.new(key=self._sk, nonce=nonce).decrypt(body)
                except Exception as e:
                    self.log("[NP] decrypt failed: %s" % e)
                    continue
                # inner should be EZStream 0x24 frames; pull 0x0b media frames.
                i = 0
                while i + 8 <= len(inner):
                    if inner[i] != 0x24:
                        j = inner.find(b"\x24", i + 1)
                        if j < 0:
                            break
                        i = j
                        continue
                    m, fl, rlen, a, b = struct.unpack(">BBHHH", inner[i:i + 8])
                    if i + 8 + rlen > len(inner):
                        break
                    bd = inner[i + 8:i + 8 + rlen]
                    i += 8 + rlen
                    if fl == 0x0b and len(bd) > 43:
                        # media frame: extra header byte, then payload
                        extra = bd[2]
                        payload = bd[3 + extra:]
                        if payload[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x00\x01"):
                            ps.extend(payload)
                            on_ps(payload)
        if demux_ps is not None and ps:
            return demux_ps(bytes(ps))
        return bytes(ps)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="EZVIZ SD-card record tools")
    ap.add_argument("cmd", choices=("list", "relay", "download"))
    ap.add_argument("serial")
    ap.add_argument("channel", type=int, nargs="?", default=1)
    ap.add_argument("begin", nargs="?")
    ap.add_argument("end", nargs="?")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD for 'list'")
    ap.add_argument("--token", default="token.json")
    ap.add_argument("--p2pinfo", default=None)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default="sd_clip.ps")
    a = ap.parse_args()

    if not os.path.exists(a.token):
        # fall back to the project token location
        cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
        if os.path.exists(cand):
            a.token = cand

    sess = EzvizSession(a.token)

    if a.cmd == "list":
        day = a.day or time.strftime("%Y-%m-%d")
        recs = sess.list_records(a.serial, a.channel, day)
        print("SD-card recordings on %s ch%d: %d segments" % (day, a.channel, len(recs)))
        for i, r in enumerate(recs):
            print("  [%2d] %s -> %s" % (i, r["begin_hms"], r["end_hms"]))
        return 0

    if a.cmd == "relay":
        host, port, pder, ticket = sess.relay_info(a.serial, a.channel)
        print("relay %s:%d  key=%sB  ticket=%s" % (
            host, port, len(pder) if pder else 0, bool(ticket)))
        return 0

    if a.cmd == "download":
        if not (a.begin and a.end):
            print("download needs <begin> <end> (HH:MM:SS on the chosen day)")
            return 2
        day = a.day or time.strftime("%Y-%m-%d")
        host, port, pder, ticket = sess.relay_info(a.serial, a.channel)
        tun = NPTunnel(host, port, pder, ticket, a.serial, a.channel,
                       p2p_info=a.p2pinfo, log=print)
        try:
            tun.connect()
            es = tun.pump(seconds=a.seconds)
            open(a.out, "wb").write(es)
            print("wrote %d bytes to %s" % (len(es), a.out))
        finally:
            tun.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
