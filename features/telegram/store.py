"""Luu cau hinh chatbot Telegram vao data/telegram_config.json.

Tach rieng kho config Zalo de khach co the bat tat doc lap, nhung van dung
chung tai lieu (RAG) va danh sach Gemini key voi chatbot Zalo.
"""
import json
import os
import threading

try:
    from features.accounts.account_manager import BASE_DIR as _BASE
except Exception:
    _BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(_BASE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "telegram_config.json")
LOG_PATH = os.path.join(DATA_DIR, "telegram_log.json")
SEEN_PATH = os.path.join(DATA_DIR, "telegram_seen_chats.json")
KNOWLEDGE_PATH = os.path.join(DATA_DIR, "telegram_knowledge.txt")

_lock = threading.RLock()

DEFAULTS = {
    # --- Trang thai ---
    "enabled": False,
    "botToken": "",            # Token tu @BotFather
    "botUsername": "",         # Ten bot, tu dong dien khi kiem tra token
    "botName": "",             # Ten hien thi
    "allowedChatIds": [],      # Chi cac chat nay duoc bot tra loi (de trong = cho phep tat ca)
    "pollIntervalSec": 2.0,
    "sendGapSec": 1.0,
    "maxMessageChars": 4000,

    # --- AI ---
    "useSharedGeminiKeys": True,   # Dung chung key voi chatbot Zalo
    "geminiApiKey": "",            # Key rieng (khi tat useSharedGeminiKeys)

    # --- Hanh vi (rieng cho Telegram) ---
    "replyToSource": True,    # Tra loi tai chat nao khach nhac toi
    "includeSenderName": True,
    "onlyPrivateChats": False,
    "ignoreBotMessages": True,

    # --- Rule / Context / RAG (riêng cho bot Telegram) ---
    "systemPrompt": "Bạn là trợ lý Telegram của shop, trả lời ngắn gọn, lịch sự, đúng ý khách.",
    "globalContext": "",
    "strictDoc": False,
    "handoffText": "Dạ câu này em chưa có thông tin chính xác, để em bảo tư vấn viên hỗ trợ Anh/Chị ngay ạ.",
}

# Các trường dùng chung giữa các nền tảng:
# Gồm API key Gemini, thứ tự model dự phòng, kiểu trả lời và từ khóa gọi bot.
# Nội dung tư vấn (tài liệu, system prompt, xưng hô, câu handoff) tách riêng độc lập.
SHARED_KEYS = (
    "mode",
    "geminiApiKey",
    "geminiApiKeys",
    "modelCascade",
    "triggerEnabled",
    "triggerMode",
    "triggerKeywords",
)

KNOWLEDGE_MAX_CHARS = 60000


def load_knowledge() -> dict:
    """Đọc tài liệu RAG riêng cho bot Telegram."""
    try:
        if os.path.exists(KNOWLEDGE_PATH):
            with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
                text = f.read() or ""
            return {"text": text, "chars": len(text), "path": KNOWLEDGE_PATH}
        # Fallback sang chatbot_knowledge nếu telegram_knowledge chưa tạo
        fallback_path = os.path.join(DATA_DIR, "chatbot_knowledge.txt")
        if os.path.exists(fallback_path):
            with open(fallback_path, "r", encoding="utf-8") as f:
                text = f.read() or ""
            return {"text": text, "chars": len(text), "path": KNOWLEDGE_PATH}
    except Exception:
        pass
    return {"text": "", "chars": 0, "path": KNOWLEDGE_PATH}


def save_knowledge(text: str) -> dict:
    t = (text or "")[:KNOWLEDGE_MAX_CHARS]
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KNOWLEDGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, KNOWLEDGE_PATH)
    return {"chars": len(t), "path": KNOWLEDGE_PATH}


def retrieve_knowledge(query: str, top_k: int = 3, chunk: int = 800) -> str:
    """RAG nhẹ cho Telegram: cắt tài liệu riêng thành chunk, chấm điểm từ khóa."""
    try:
        doc = load_knowledge().get("text") or ""
    except Exception:
        return ""
    doc = doc.strip()
    if not doc or not (query or "").strip():
        return ""
    import re as _re
    chunks = [doc[i:i + chunk] for i in range(0, len(doc), chunk) if doc[i:i + chunk].strip()]
    if not chunks:
        return ""
    qwords = [w for w in _re.findall(r"[a-zA-ZÀ-ỹ0-9]+", query.lower()) if len(w) > 1]
    if not qwords:
        return "\n---\n".join(chunks[:top_k])
    scored = []
    for c in chunks:
        cl = c.lower()
        score = sum(1 for w in set(qwords) if w in cl)
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] == 0:
        return "\n---\n".join(chunks[:top_k])
    return "\n---\n".join(c for _, c in scored[:top_k])

# Do dai toi da cho phep luu trong config (token 40 ky tu, van con an toan)
MAX_TOKEN_CHARS = 200


def _mask_token(token: str) -> str:
    t = str(token or "").strip()
    if not t:
        return ""
    if len(t) <= 10:
        return t[:2] + "*" * (len(t) - 2)
    return t[:6] + "*" * 6 + t[-4:]


def load_config() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in DEFAULTS:
                        if k in data:
                            cfg[k] = data[k]
        except Exception:
            pass

        # chuan hoa
        if not isinstance(cfg.get("allowedChatIds"), list):
            cfg["allowedChatIds"] = []
        cfg["allowedChatIds"] = [str(x).strip() for x in cfg["allowedChatIds"] if str(x).strip()]
        cfg["botToken"] = str(cfg.get("botToken") or "").strip()[:MAX_TOKEN_CHARS]
        for k in ("botUsername", "botName"):
            cfg[k] = str(cfg.get(k) or "")
        cfg["enabled"] = bool(cfg.get("enabled"))
        cfg["replyToSource"] = bool(cfg.get("replyToSource", True))
        cfg["onlyPrivateChats"] = bool(cfg.get("onlyPrivateChats"))
        cfg["ignoreBotMessages"] = bool(cfg.get("ignoreBotMessages", True))
        cfg["useSharedGeminiKeys"] = bool(cfg.get("useSharedGeminiKeys", True))
        try:
            cfg["pollIntervalSec"] = max(1.0, float(cfg.get("pollIntervalSec") or 2.0))
        except Exception:
            cfg["pollIntervalSec"] = 2.0
        try:
            cfg["sendGapSec"] = max(0.3, float(cfg.get("sendGapSec") or 1.0))
        except Exception:
            cfg["sendGapSec"] = 1.0
        try:
            cfg["maxMessageChars"] = max(200, min(4000, int(cfg.get("maxMessageChars") or 4000)))
        except Exception:
            cfg["maxMessageChars"] = 4000

        # Noi phan "bo nao bot" dung chung tu chatbot Zalo vao.
        # Luu y: danh sach API key chi dung chung khi khach chon "dung chung key".
        try:
            from features.chatbot import store as _shared_store
            shared = _shared_store.load_shared_config()
        except Exception:
            shared = {}
        for k in SHARED_KEYS:
            if k in ("geminiApiKey", "geminiApiKeys"):
                continue
            if k in shared:
                cfg[k] = shared[k]
        if cfg.get("useSharedGeminiKeys", True):
            cfg["geminiApiKey"] = shared.get("geminiApiKey") or ""
            cfg["geminiApiKeys"] = shared.get("geminiApiKeys") or []
        else:
            # Key rieng cho Telegram: giu nguyen gia tri da luu trong file rieng.
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    own = json.load(f)
                cfg["geminiApiKey"] = str((own or {}).get("geminiApiKey") or "")
                cfg["geminiApiKeys"] = (own or {}).get("geminiApiKeys") or []
            except Exception:
                cfg["geminiApiKey"] = ""
                cfg["geminiApiKeys"] = []
        return cfg


def save_config(patch: dict) -> dict:
    with _lock:
        cfg = load_config()
        for k in DEFAULTS:
            if k in (patch or {}):
                cfg[k] = patch[k]
        if "allowedChatIds" in (patch or {}):
            v = patch.get("allowedChatIds")
            if isinstance(v, str):
                v = [x.strip() for x in v.split(",")]
            cfg["allowedChatIds"] = [str(x).strip() for x in (v or []) if str(x).strip()]
        if "botToken" in (patch or {}):
            cfg["botToken"] = str(patch.get("botToken") or "").strip()[:MAX_TOKEN_CHARS]
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return cfg


def public_config(cfg: dict | None = None) -> dict:
    """Cau hinh an token, dung cho UI."""
    c = dict(cfg if isinstance(cfg, dict) else load_config())
    token = str(c.get("botToken") or "")
    c["botTokenMasked"] = _mask_token(token)
    c["hasToken"] = bool(token)
    c["botToken"] = ""
    return c


def load_log() -> list:
    try:
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data[-300:]
    except Exception:
        pass
    return []


def append_log(entry: dict) -> None:
    with _lock:
        items = load_log()
        items.append(entry)
        items = items[-300:]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = LOG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, LOG_PATH)
        except Exception:
            pass


def load_seen_chats() -> list:
    """Danh sach chat da nhan tin nhan, dung cho nut 'Quet Chat ID'."""
    try:
        if os.path.exists(SEEN_PATH):
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data[-100:]
    except Exception:
        pass
    return []


def remember_chat(chat_id: str, title: str, chat_type: str = "") -> None:
    """Ghi nho chat vua nhan tin de khach chon duoc Chat ID ma khong can doan."""
    cid = str(chat_id or "").strip()
    if not cid:
        return
    with _lock:
        items = load_seen_chats()
        items = [x for x in items if str(x.get("chatId") or "") != cid]
        items.append({
            "chatId": cid,
            "title": str(title or ""),
            "type": str(chat_type or ""),
        })
        items = items[-100:]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = SEEN_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SEEN_PATH)
        except Exception:
            pass
