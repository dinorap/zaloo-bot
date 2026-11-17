"""Client Telegram Bot API - goi REST truc tiep, khong can thu vien.

Chay bang long polling (getUpdates) nen khach khong can mo port, khong can HTTPS.

Toi uu: dung connection pool (core.zalo.http) -> giu TLS session giua cac
lan goi, thay vi bat tay TLS moi lan.
"""
import time

from core.zalo import http as zalo_http

API_BASE = "https://api.telegram.org"

# Telegram chat type
CHAT_PRIVATE = "private"
CHAT_GROUP = "group"
CHAT_SUPERGROUP = "supergroup"
CHAT_CHANNEL = "channel"


def _log(msg: str):
    try:
        print(f"[telegram] {msg}", flush=True)
    except Exception:
        pass


def _post(token: str, method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    """Tra ve {ok, result, error, error_code}."""
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "error": "Chua nhap Bot Token", "error_code": 0}
    url = f"{API_BASE}/bot{token}/{method}"
    try:
        r = zalo_http.post(url, json=payload or {}, timeout=timeout, retries=1)
    except Exception as e:
        return {"ok": False, "error": f"Khong ket noi duoc Telegram: {str(e)[:160]}", "error_code": 0}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": f"Telegram tra ve phan hoi khong doc duoc (HTTP {r.status_code})", "error_code": r.status_code}
    if not data.get("ok"):
        desc = str(data.get("description") or "Loi khong ro")
        return {"ok": False, "error": desc, "error_code": int(data.get("error_code") or 0)}
    return {"ok": True, "result": data.get("result")}


def get_me(token: str) -> dict:
    """Kiem tra token va lay thong tin bot. Tra them thong tin doc cho nguoi dung."""
    res = _post(token, "getMe")
    if not res.get("ok"):
        return res
    me = res.get("result") or {}
    return {
        "ok": True,
        "result": me,
        "id": me.get("id"),
        "username": me.get("username") or "",
        "firstName": me.get("first_name") or "",
        "name": me.get("first_name") or me.get("username") or "",
    }


def get_chat(token: str, chat_id: str) -> dict:
    return _post(token, "getChat", {"chat_id": str(chat_id or "")})


def get_updates(token: str, offset: int = 0, timeout: int = 25) -> dict:
    """Long polling. offset = update_id cuoi + 1 de khong nhan trung."""
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "error": "Chua nhap Bot Token", "error_code": 0, "result": []}
    url = f"{API_BASE}/bot{token}/getUpdates"
    try:
        r = zalo_http.post(url, json={
            "offset": int(offset or 0),
            "timeout": int(max(0, min(50, timeout))),
            "allowed_updates": ["message"],
        }, timeout=int(timeout) + 15, retries=0)
    except Exception as e:
        return {"ok": False, "error": f"Khong ket noi duoc Telegram: {str(e)[:160]}", "error_code": 0, "result": []}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": f"Phan hoi khong doc duoc (HTTP {r.status_code})", "error_code": r.status_code, "result": []}
    if not data.get("ok"):
        desc = str(data.get("description") or "Loi khong ro")
        # 409 = co tien trinh khac dang poll cung token
        return {"ok": False, "error": desc, "error_code": int(data.get("error_code") or 0), "result": []}
    return {"ok": True, "result": data.get("result") or []}


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "", reply_to_message_id: int | str | None = None) -> dict:
    payload = {"chat_id": str(chat_id or ""), "text": str(text or "")}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id:
        try:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        except Exception:
            pass
    return _post(token, "sendMessage", payload)


def set_my_commands(token: str, commands: list | None = None) -> dict:
    """Dang ky lenh "/" cho bot, hien thi trong menu cua Telegram."""
    cmds = commands or [
        {"command": "start", "description": "Bat dau tro chuyen"},
        {"command": "help", "description": "Huong dan su dung bot"},
        {"command": "ping", "description": "Kiem tra bot da online"},
    ]
    return _post(token, "setMyCommands", {"commands": cmds})


def describe_chat(chat: dict) -> str:
    """Ten hien thi cua chat nguoi gui."""
    chat = chat or {}
    t = str(chat.get("type") or "")
    if t == CHAT_PRIVATE:
        first = str(chat.get("first_name") or "").strip()
        last = str(chat.get("last_name") or "").strip()
        full = " ".join(x for x in (first, last) if x)
        return full or str(chat.get("username") or "Nguoi dung")
    if t in (CHAT_GROUP, CHAT_SUPERGROUP):
        return str(chat.get("title") or "Nhom") or "Nhom"
    return str(chat.get("title") or chat.get("username") or "Kenh")


def extract_text(message: dict) -> str:
    """Lay noi dung text cua message, bo qua message khong phai text."""
    m = message or {}
    if "text" in m and m.get("text"):
        return str(m.get("text") or "")
    if "caption" in m and m.get("caption"):
        return str(m.get("caption") or "")
    return ""


def parse_update(update: dict) -> dict:
    """Chuyen update Telegram thanh ban ghi don gian cho worker xu ly."""
    u = update or {}
    msg = u.get("message") or u.get("edited_message") or {}
    if not msg:
        return {}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    return {
        "updateId": u.get("update_id"),
        "messageId": msg.get("message_id"),
        "chatId": str(chat.get("id") or ""),
        "chatType": str(chat.get("type") or ""),
        "chatTitle": describe_chat(chat),
        "senderId": str(sender.get("id") or ""),
        "senderName": " ".join(
            x for x in (str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip()) if x
        ) or str(sender.get("username") or ""),
        "senderIsBot": bool(sender.get("is_bot")),
        "text": extract_text(msg),
        "date": msg.get("date"),
    }
