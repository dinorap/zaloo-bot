# -*- coding: utf-8 -*-
"""Thu hồi (gỡ) tin nhắn Zalo 1-1 và nhóm qua API Zalo Web."""
import json
import time

from core.zalo.enc import zalo_encode
from core.zalo.dec import zalo_decode
from core.zalo.zalo_config import get_zpw_ver
from core.zalo.zalo_headers import zalo_mobile_headers
from core.zalo import http as zalo_http

UNDO_USER_URL = "https://tt-chat3-wpa.chat.zalo.me/api/message/undo"
UNDO_GROUP_URL = "https://tt-group-wpa.chat.zalo.me/api/group/undomsg"


def _cookies_str_to_dict(cookies: str) -> dict:
    out = {}
    for item in (cookies or "").split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _decode_data(resp_json: dict, zpw_enk: str):
    data_field = (resp_json or {}).get("data", "")
    if isinstance(data_field, str) and data_field:
        try:
            decoded = zalo_decode(data_field, zpw_enk)
        except Exception:
            return None
        if isinstance(decoded, str):
            try:
                return json.loads(decoded)
            except Exception:
                return None
        return decoded
    return data_field


def undo_message(
    cookies: str,
    zpw_enk: str,
    target_id: str,
    imei: str,
    msg_id: str,
    cli_msg_id: str,
    is_group: bool = False,
    zpw_ver: str = None,
    timeout: int = 20,
) -> dict:
    """Thu hồi tin nhắn đã gửi trên Zalo (1-1 hoặc nhóm)."""
    msg_id = str(msg_id or "").strip()
    cli_msg_id = str(cli_msg_id or "").strip()
    target_id = str(target_id or "").strip()
    if not msg_id or not cli_msg_id or not target_id:
        return {"ok": False, "error": "Thiếu msgId / cliMsgId / targetId để thu hồi."}

    zpw_ver = get_zpw_ver(zpw_ver)
    payload = {
        "msgId": msg_id,
        "clientId": int(time.time() * 1000),
        "cliMsgIdUndo": cli_msg_id,
    }
    if is_group:
        url = UNDO_GROUP_URL
        payload["grid"] = target_id
        payload["visibility"] = 0
        payload["imei"] = str(imei or "")
    else:
        url = UNDO_USER_URL
        payload["toid"] = target_id

    enc_params = zalo_encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        zpw_enk,
        url_encode=False,
    )
    response = zalo_http.post(
        url,
        params={"zpw_ver": zpw_ver, "zpw_type": "30", "nretry": "0"},
        data={"params": enc_params},
        headers=zalo_mobile_headers(),
        cookies=_cookies_str_to_dict(cookies),
        timeout=timeout,
    )
    try:
        resp_json = response.json()
    except Exception as exc:
        return {"ok": False, "error": f"Zalo trả về không phải JSON: {response.text[:200]}"}

    decoded = _decode_data(resp_json, zpw_enk)
    error_code = None
    error_msg = ""
    if isinstance(decoded, dict):
        error_code = decoded.get("error_code", decoded.get("errorCode"))
        error_msg = str(decoded.get("error_message") or decoded.get("errorMessage") or "")
    if error_code is None and isinstance(resp_json, dict):
        error_code = resp_json.get("error_code", resp_json.get("errorCode"))
        error_msg = str(resp_json.get("error_message") or resp_json.get("errorMessage") or error_msg)

    ok = str(error_code) in ("0", "None") and response.status_code == 200
    if not ok and not error_msg:
        error_msg = f"Zalo từ chối thu hồi (error_code={error_code})"
    return {"ok": bool(ok), "error": "" if ok else error_msg, "decoded": decoded, "response": resp_json}

