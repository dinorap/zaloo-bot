# -*- coding: utf-8 -*-
"""Gửi file đính kèm (PDF, DOCX, XLSX, ZIP, MP4...) qua Zalo Web API (asyncfile/upload + WS file_done + asyncfile/msg)."""
import base64
import hashlib
import json
import os
import struct
import threading
import time
import zlib
from typing import Optional
from urllib.parse import unquote

import websocket
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.zalo.dec import zalo_decode
from core.zalo.enc import zalo_encode
from core.zalo.zalo_config import get_zpw_ver
from core.zalo.zalo_headers import zalo_mobile_headers
from core.zalo import http as zalo_http

FILE_DOMAIN = "https://tt-files-wpa.chat.zalo.me"
SERVER_INFO_URL = "https://wpa.chat.zalo.me/api/login/getServerInfo"
CHUNK_SIZE = 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024


def _cookies_str_to_dict(cookies: str) -> dict:
    out = {}
    for item in (cookies or "").split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _decode_data_field(resp_json: dict, zpw_enk: str):
    data_field = (resp_json or {}).get("data", "")
    if isinstance(data_field, str) and data_field:
        try:
            decoded = zalo_decode(data_field, zpw_enk)
            if isinstance(decoded, str):
                return json.loads(decoded)
            return decoded
        except Exception:
            return None
    return data_field


def _get_ws_urls(cookies: str, imei: str, zpw_ver: str) -> list:
    params = {
        "imei": str(imei or ""),
        "type": "30",
        "client_version": str(zpw_ver),
        "computer_name": "Web",
    }
    raw = "zsecuregetserverinfo" + "".join(str(params[k]) for k in sorted(params.keys()))
    params["signkey"] = hashlib.md5(raw.encode("utf-8")).hexdigest()
    resp = zalo_http.get(
        SERVER_INFO_URL,
        params=params,
        headers=zalo_mobile_headers(),
        cookies=_cookies_str_to_dict(cookies),
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    err_code = payload.get("error_code")
    if err_code not in (None, 0, "0"):
        err_msg = str(payload.get("error_message") or "")
        raise RuntimeError(f"getServerInfo lỗi {err_code}: {err_msg}".strip())
    data = payload.get("data") or {}
    ws_list = data.get("zpw_ws") or []
    return [str(u) for u in ws_list if u]


def _decode_ws_event(parsed: dict, cipher_key_b64: str) -> dict:
    enc_type = int(parsed.get("encrypt", 0))
    raw_data = parsed.get("data") or ""
    if enc_type == 0:
        return json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    b64_str = raw_data if enc_type == 1 else unquote(raw_data)
    buf = base64.b64decode(b64_str)
    if enc_type != 1:
        if not cipher_key_b64 or len(buf) < 48:
            raise ValueError("Thiếu cipherKey hoặc dữ liệu WS không hợp lệ")
        iv = buf[:16]
        aad = buf[16:32]
        ct = buf[32:]
        key = base64.b64decode(cipher_key_b64)
        buf = AESGCM(key).decrypt(iv, ct, aad)
    if enc_type != 3 and not buf.lstrip().startswith((b"{", b"[")):
        try:
            # Zalo WS cmd=601 gui payload nen gzip (1f 8b); MAX_WBITS|32 tu nhan ca gzip lan zlib.
            buf = zlib.decompress(buf, zlib.MAX_WBITS | 32)
        except zlib.error:
            buf = zlib.decompress(buf, -zlib.MAX_WBITS)
    return json.loads(buf.decode("utf-8"))


class _FileDoneWaiter:
    """Short-lived Zalo WebSocket listener waiting for cmd=601 file_done."""

    def __init__(self, ws_url: str, cookies: str, zpw_ver: str = "687"):
        self.ws_url = ws_url
        self.cookies = cookies
        self.zpw_ver = str(zpw_ver or "687")
        self.cipher_key = ""
        self.ready_ev = threading.Event()
        self.done_ev = threading.Event()
        self.target_file_id = ""
        self.results = {}
        self.err = ""
        self.ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, timeout: float = 10.0) -> bool:
        base = str(self.ws_url or "").rstrip("/")
        sep = "&" if "?" in base else "/?"
        url = f"{base}{sep}zpw_ver={self.zpw_ver}&zpw_type=30&t={int(time.time() * 1000)}"
        ua = zalo_mobile_headers().get("User-Agent", "Mozilla/5.0")
        headers = [
            "Origin: https://chat.zalo.me",
            f"User-Agent: {ua}",
            f"Cookie: {self.cookies}",
        ]

        def on_message(ws, data):
            if not isinstance(data, (bytes, bytearray)) or len(data) < 4:
                return
            ver, cmd, sub_cmd = struct.unpack("<BHB", data[:4])
            try:
                body = data[4:].decode("utf-8", errors="replace").strip()
                if not body:
                    return
                parsed = json.loads(body)
                if ver == 1 and cmd == 1 and sub_cmd == 1 and "key" in parsed:
                    self.cipher_key = str(parsed["key"] or "")
                    self.ready_ev.set()
                elif ver == 1 and cmd == 601 and sub_cmd == 0:
                    ev = _decode_ws_event(parsed, self.cipher_key)
                    controls = ((ev.get("data") or {}).get("controls") or [])
                    for ctrl in controls:
                        content = ctrl.get("content") or {}
                        if isinstance(content, str):
                            try:
                                content = json.loads(content)
                            except Exception:
                                content = {}
                        if isinstance(content, dict) and content.get("act_type") == "file_done":
                            fid = str(content.get("fileId") or "")
                            furl = str(((content.get("data") or {}).get("url")) or "")
                            if fid and furl:
                                self.results[fid] = furl
                            if self.target_file_id and fid == self.target_file_id and furl:
                                self.done_ev.set()
            except Exception as exc:
                self.err = str(exc)

        def on_error(ws, error):
            if not self.err:
                self.err = str(error)
            self.ready_ev.set()

        def on_close(ws, code, msg):
            self.ready_ev.set()

        self.ws = websocket.WebSocketApp(
            url,
            header=headers,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._thread = threading.Thread(target=self.ws.run_forever, daemon=True, name="zalo-file-ws")
        self._thread.start()
        self.ready_ev.wait(timeout)
        return bool(self.cipher_key)

    def wait_file_url(self, file_id: str, timeout: float = 25.0) -> str:
        self.target_file_id = str(file_id or "")
        if self.results.get(self.target_file_id):
            return self.results[self.target_file_id]
        self.done_ev.wait(timeout)
        return self.results.get(self.target_file_id, "")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


def send_file(
    file_bytes: bytes,
    file_name: str,
    to_id: str,
    zpw_enk: str,
    cookies: str,
    imei: str,
    is_group: bool = False,
    zpw_ver: str = None,
    timeout: int = 30,
) -> dict:
    """Upload file theo chunk len Zalo + cho file_done qua WS + gui tin nhan file."""
    file_bytes = file_bytes or b""
    if not file_bytes:
        return {"ok": False, "message": "File trống."}
    if len(file_bytes) > MAX_FILE_BYTES:
        return {"ok": False, "message": f"File vượt quá {MAX_FILE_BYTES // (1024 * 1024)}MB."}
    file_name = os.path.basename(str(file_name or "file.bin").strip()) or "file.bin"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    zpw_ver = get_zpw_ver(zpw_ver)

    try:
        ws_urls = _get_ws_urls(cookies, imei, zpw_ver)
    except Exception as exc:
        return {"ok": False, "message": f"Không lấy được server Zalo WS: {exc}"}
    if not ws_urls:
        return {"ok": False, "message": "Zalo không trả về địa chỉ WebSocket."}

    waiter = _FileDoneWaiter(ws_urls[0], cookies, zpw_ver=zpw_ver)
    try:
        if not waiter.start(timeout=10.0):
            return {"ok": False, "message": f"Không kết nối được Zalo WebSocket ({waiter.err or 'timeout'})."}

        total_size = len(file_bytes)
        chunks = [file_bytes[i:i + CHUNK_SIZE] for i in range(0, total_size, CHUNK_SIZE)]
        total_chunk = len(chunks)
        client_id = int(time.time() * 1000)

        upload_url = FILE_DOMAIN + ("/api/group/asyncfile/upload" if is_group else "/api/message/asyncfile/upload")
        url_type = "11" if is_group else "2"

        headers = zalo_mobile_headers()
        for key in list(headers.keys()):
            if key.lower() == "content-type":
                headers.pop(key)
        cookie_dict = _cookies_str_to_dict(cookies)

        file_id = ""
        client_file_id = client_id
        http_file_url = ""
        for idx, chunk in enumerate(chunks, start=1):
            meta = {
                "totalChunk": total_chunk,
                "fileName": file_name,
                "clientId": client_id,
                "totalSize": total_size,
                "imei": str(imei or ""),
                "isE2EE": 0,
                "jxl": 0,
                "chunkId": idx,
            }
            if is_group:
                meta["grid"] = str(to_id)
            else:
                meta["toid"] = str(to_id)

            enc_params = zalo_encode(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), zpw_enk, url_encode=False)
            resp = zalo_http.post(
                upload_url,
                params={"zpw_ver": zpw_ver, "zpw_type": "30", "type": url_type, "params": enc_params},
                files={"chunkContent": (file_name, chunk, "application/octet-stream")},
                headers=headers,
                cookies=cookie_dict,
                timeout=timeout,
            )
            if resp.status_code != 200:
                return {"ok": False, "message": f"Upload file lỗi HTTP {resp.status_code}"}
            resp_json = resp.json()
            up_err = resp_json.get("error_code")
            if up_err not in (None, 0, 1, "0", "1"):
                up_msg = str(resp_json.get("error_message") or "")
                return {"ok": False, "message": f"Upload file lỗi {up_err}: {up_msg}".strip()}
            decoded = _decode_data_field(resp_json, zpw_enk) or {}
            if isinstance(decoded, dict) and decoded.get("error_code") not in (None, 0, 1, "0", "1"):
                in_msg = str(decoded.get("error_message") or "")
                return {"ok": False, "message": f"Upload file lỗi {decoded.get('error_code')}: {in_msg}".strip()}
            inner = decoded.get("data") if isinstance(decoded.get("data"), dict) else decoded
            if isinstance(inner, dict):
                fid = str(inner.get("fileId") or "")
                if fid and fid != "-1":
                    file_id = fid
                if inner.get("clientFileId"):
                    client_file_id = inner.get("clientFileId")
                if inner.get("url"):
                    http_file_url = str(inner.get("url"))

        if not file_id:
            return {"ok": False, "message": "Zalo không trả về fileId khi upload file."}

        file_url = http_file_url or waiter.wait_file_url(file_id, timeout=25.0)
        if not file_url:
            detail = f" ({waiter.err})" if waiter.err else ""
            return {"ok": False, "message": f"Zalo xử lý file quá lâu (không nhận được file_done){detail}."}
    finally:
        waiter.close()

    checksum = hashlib.md5(file_bytes).hexdigest()
    msg_url = FILE_DOMAIN + ("/api/group/asyncfile/msg" if is_group else "/api/message/asyncfile/msg")
    send_payload = {
        "fileId": file_id,
        "checksum": checksum,
        "checksumSha": "",
        "extention": ext,
        "totalSize": total_size,
        "fileName": file_name,
        "clientId": client_file_id,
        "fType": 1,
        "fileCount": 0,
        "fdata": "{}",
        "fileUrl": file_url,
        "zsource": -1,
        "ttl": 0,
        "imei": str(imei or ""),
    }
    if is_group:
        send_payload["grid"] = str(to_id)
    else:
        send_payload["toid"] = str(to_id)

    enc_send = zalo_encode(json.dumps(send_payload, ensure_ascii=False, separators=(",", ":")), zpw_enk, url_encode=False)
    send_resp = zalo_http.post(
        msg_url,
        params={"zpw_ver": zpw_ver, "zpw_type": "30", "nretry": "0"},
        data={"params": enc_send},
        headers=zalo_mobile_headers(),
        cookies=_cookies_str_to_dict(cookies),
        timeout=timeout,
    )
    try:
        send_json = send_resp.json()
    except Exception:
        return {"ok": False, "message": f"Zalo asyncfile/msg không trả JSON: {send_resp.text[:200]}"}

    decoded_send = _decode_data_field(send_json, zpw_enk)
    error_code = None
    error_msg = ""
    if isinstance(decoded_send, dict):
        error_code = decoded_send.get("error_code", decoded_send.get("errorCode"))
        error_msg = str(decoded_send.get("error_message") or decoded_send.get("errorMessage") or "")
    if error_code is None and isinstance(send_json, dict):
        error_code = send_json.get("error_code", send_json.get("errorCode"))
        error_msg = str(send_json.get("error_message") or send_json.get("errorMessage") or error_msg)

    ok = str(error_code) in ("0", "None") or (isinstance(decoded_send, dict) and bool(decoded_send.get("msgId")))
    return {
        "ok": bool(ok),
        "message": "Đã gửi file" if ok else (error_msg or f"Gửi file lỗi error_code={error_code}"),
        "fileId": file_id,
        "fileUrl": file_url,
        "clientId": client_file_id,
        "decoded": decoded_send,
    }

