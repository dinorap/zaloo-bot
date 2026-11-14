"""Luu cau hinh chatbot vao data/chatbot_config.json (khong chua cookies)."""
import json
import os
import time
import threading

try:
    from features.accounts.account_manager import BASE_DIR as _BASE
except Exception:
    _BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(_BASE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "chatbot_config.json")
CURSOR_PATH = os.path.join(DATA_DIR, "chatbot_cursor.json")
KNOWLEDGE_PATH = os.path.join(DATA_DIR, "chatbot_knowledge.txt")

_lock = threading.RLock()

# --- Cache theo mtime ---
# Worker chatbot goi load_config()/load_cursor() MOI VONG POLL (1.5s).
# Truoc day moi vong deu mo + parse lai file tu dia -> I/O thua.
# Bay gio cache theo (mtime, size) -> chi doc lai khi file thay doi that.
_cfg_cache: tuple[float, int, dict] | None = None
_cursor_cache: tuple[float, int, dict] | None = None


def _stat_of(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def invalidate_caches() -> None:
    """Xoa cache (dung khi file bi doi ngoai process)."""
    global _cfg_cache, _cursor_cache
    with _lock:
        _cfg_cache = None
        _cursor_cache = None

DEFAULTS = {
    "enabled": False,
    "accountId": "",
    "targetGroupIds": [],
    "destGroupId": "",
    "mode": "echo",  # echo | gemini
    "geminiApiKey": "",
    "geminiApiKeys": [],
    "geminiKeyIndex": 0,
    "pollIntervalSec": 1.5,
    "allowSelfReply": True,
    "sendGapSec": 1.2,
    # Gian cach toi thieu giua 2 lan tra loi cung 1 nhom (chong loop / spam).
    "replyCooldownSec": 3.0,
    # Multi-nhóm: True = trả lời đúng nhóm nguồn (khuyên dùng).
    # False = dồn mọi nhóm nguồn về 1 destGroupId cố định (chế độ gom cũ).
    "replyToSource": True,
    # Thêm tên người gửi vào câu hỏi để AI nhận biết và xưng hô sát với từng khách
    "includeSenderName": True,
    # --- Rule / Context / RAG (riêng cho bot Zalo: globalContext + systemPrompt + tài liệu) ---
    "systemPrompt": "Bạn là trợ lý Zalo của shop, trả lời ngắn gọn, lịch sự, đúng ý khách.",
    "globalContext": "",
    "modelCascade": ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash"],
    "strictDoc": False,  # True = CHI tra loi theo tai lieu, khong co thi bao handoff
    "handoffText": "Dạ câu này em chưa có thông tin chính xác, để em báo tư vấn viên hỗ trợ Anh/Chị ngay ạ.",
    # --- Từ khóa kích hoạt (vd: /bot <câu hỏi>) ---
    "triggerEnabled": True,
    "triggerMode": "prefix",  # prefix | contains
    "triggerKeywords": "/bot",
}

KNOWLEDGE_MAX_CHARS = 60000

# ─────────────────────────────────────────────────────────────────────────────
# Cấu hình DÙNG CHUNG cho mọi nền tảng (Zalo, Telegram, …)
# Gồm API key Gemini, thứ tự model dự phòng, kiểu trả lời và từ khóa gọi bot.
# Nội dung tư vấn (tài liệu, system prompt, xưng hô, câu handoff) tách riêng
# cho từng nền tảng để tùy biến độc lập.
# ─────────────────────────────────────────────────────────────────────────────
SHARED_KEYS = (
    "mode",
    "geminiApiKey",
    "geminiApiKeys",
    "modelCascade",
    "triggerEnabled",
    "triggerMode",
    "triggerKeywords",
)


def load_shared_config() -> dict:
    """Trả về phần cấu hình dùng chung (bộ não bot) cho mọi nền tảng."""
    cfg = load_config()
    return {k: cfg.get(k) for k in SHARED_KEYS}


def save_shared_config(patch: dict) -> dict:
    """Lưu phần cấu hình dùng chung. Chỉ nhận các key thuộc SHARED_KEYS."""
    sub = {}
    for k in SHARED_KEYS:
        if k in (patch or {}):
            sub[k] = patch[k]
    return save_config(sub)


def load_knowledge() -> dict:
    """Doc tai lieu RAG (text thuong, copy tu SocAI / rule san pham)."""
    try:
        if os.path.exists(KNOWLEDGE_PATH):
            with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
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
    """RAG nhe: cat tai lieu thanh chunk, cham diem overlap tu khoa voi cau hoi."""
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
    # neu khong chunk nao khop tu nao -> lay dau tai lieu de model co context
    if scored[0][0] == 0:
        return "\n---\n".join(chunks[:top_k])
    return "\n---\n".join(c for _, c in scored[:top_k])


def normalize_api_keys(raw_keys, legacy_key: str = "", prev_keys: list | None = None) -> list:
    """Chuẩn hóa danh sách API key Gemini, giữ nguyên trạng thái limit của key cũ."""
    prev_map = {}
    for item in (prev_keys or []):
        if isinstance(item, dict):
            k = str(item.get("key") or "").strip()
            if k:
                prev_map[k] = item

    candidates = []
    if isinstance(raw_keys, str):
        import re as _re
        for part in _re.split(r"[,;\r\n]+", raw_keys):
            k = part.strip()
            if k:
                candidates.append({"key": k})
    elif isinstance(raw_keys, list):
        candidates = list(raw_keys)

    if not candidates and str(legacy_key or "").strip():
        candidates = [{"key": str(legacy_key).strip()}]

    out = []
    seen = set()
    for idx, item in enumerate(candidates, start=1):
        if isinstance(item, str):
            item = {"key": item}
        if not isinstance(item, dict):
            continue
        k = str(item.get("key") or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        old = prev_map.get(k) or {}
        limited = bool(item.get("limited")) if "limited" in item else bool(old.get("limited", False))
        limited_at = int(item.get("limitedAt") or (old.get("limitedAt") if limited else 0) or 0)
        reason = str(item.get("limitReason") if "limitReason" in item else (old.get("limitReason") if limited else "") or "").strip()
        if not limited:
            limited_at = 0
            reason = ""
        label = str(item.get("label") or old.get("label") or f"Key {len(out) + 1}").strip()
        out.append({
            "label": label,
            "key": k,
            "limited": limited,
            "limitedAt": limited_at,
            "limitReason": reason[:160],
        })
    return out


def _sync_key_fields(cfg: dict, prev_keys: list | None = None):
    keys = normalize_api_keys(cfg.get("geminiApiKeys"), cfg.get("geminiApiKey") or "", prev_keys=prev_keys)
    cfg["geminiApiKeys"] = keys
    try:
        idx = int(cfg.get("geminiKeyIndex") or 0)
    except Exception:
        idx = 0
    if keys:
        idx = idx % len(keys)
    else:
        idx = 0
    cfg["geminiKeyIndex"] = idx
    usable = [x["key"] for x in keys if not x.get("limited")]
    cfg["geminiApiKey"] = usable[0] if usable else (keys[0]["key"] if keys else "")


def get_usable_api_keys(cfg: dict | None = None) -> list:
    """Trả về danh sách dict key chưa bị đánh dấu limit, bắt đầu từ geminiKeyIndex."""
    c = dict(cfg) if isinstance(cfg, dict) else load_config()
    keys = normalize_api_keys(c.get("geminiApiKeys"), c.get("geminiApiKey") or "")
    if not keys:
        return []
    n = len(keys)
    try:
        start = int(c.get("geminiKeyIndex") or 0) % n
    except Exception:
        start = 0
    ordered = keys[start:] + keys[:start]
    return [item for item in ordered if not item.get("limited")]


def mark_key_limited(api_key: str, reason: str = "") -> dict:
    """Đánh dấu 1 API key bị hết limit và xoay geminiKeyIndex sang key sẵn sàng tiếp theo."""
    target = str(api_key or "").strip()
    with _lock:
        cfg = load_config()
        keys = cfg.get("geminiApiKeys") or []
        now_ms = int(time.time() * 1000)
        for item in keys:
            if item.get("key") == target:
                item["limited"] = True
                item["limitedAt"] = now_ms
                item["limitReason"] = str(reason or "Hết quota / limit")[:160]
        if keys:
            n = len(keys)
            cur = int(cfg.get("geminiKeyIndex") or 0) % n
            for step in range(1, n + 1):
                cand = (cur + step) % n
                if not keys[cand].get("limited"):
                    cfg["geminiKeyIndex"] = cand
                    break
        return save_config({
            "geminiApiKeys": keys,
            "geminiKeyIndex": cfg.get("geminiKeyIndex", 0),
        })


def unmark_key_limited(api_key: str = "") -> dict:
    """Bỏ đánh dấu limit cho 1 key cụ thể hoặc tất cả key (khi api_key rỗng / '__all__')."""
    target = str(api_key or "").strip()
    clear_all = (not target) or (target == "__all__")
    with _lock:
        cfg = load_config()
        keys = cfg.get("geminiApiKeys") or []
        for item in keys:
            if clear_all or item.get("key") == target:
                item["limited"] = False
                item["limitedAt"] = 0
                item["limitReason"] = ""
        return save_config({"geminiApiKeys": keys})


def _normalize_config(cfg: dict) -> dict:
    """Chuan hoa cau hinh ( tach rieng de dung chung cache + doc file )."""
    # chuan hoa
    if not isinstance(cfg.get("targetGroupIds"), list):
        cfg["targetGroupIds"] = []
    cfg["targetGroupIds"] = [str(x).strip() for x in cfg["targetGroupIds"] if str(x).strip()]
    try:
        cfg["pollIntervalSec"] = max(0.5, float(cfg.get("pollIntervalSec") or 1.5))
    except Exception:
        cfg["pollIntervalSec"] = 1.5
    try:
        cfg["sendGapSec"] = max(0.5, float(cfg.get("sendGapSec") or 1.2))
    except Exception:
        cfg["sendGapSec"] = 1.2
    try:
        cfg["replyCooldownSec"] = max(0.0, float(cfg.get("replyCooldownSec") or 3.0))
    except Exception:
        cfg["replyCooldownSec"] = 3.0
    # chuan hoa model cascade (hoc SocAI: fallback routing)
    mc = cfg.get("modelCascade")
    if isinstance(mc, str):
        mc = [x.strip() for x in mc.split(",") if x.strip()]
    if not isinstance(mc, list) or not mc:
        mc = list(DEFAULTS["modelCascade"])
    mc = [str(x).strip() for x in mc if str(x).strip()]

    # Loai model da bi Google rut/khong con hoat dong.
    # Truoc day cascade luu trong config van giu "gemini-3.8-flash" (da rut)
    # -> moi cau hoi ton ~50s cho model do truoc khi fallback.
    try:
        from features.chatbot.gemini_client import KNOWN_DEAD_MODELS
        alive = [m for m in mc if m not in KNOWN_DEAD_MODELS]
        if alive and len(alive) != len(mc):
            dropped = [m for m in mc if m in KNOWN_DEAD_MODELS]
            _dropped = ", ".join(dropped)
            try:
                print(f"[chatbot] Bo model khong con hoat dong khoi cascade: {_dropped}")
            except Exception:
                pass
            mc = alive
    except Exception:
        pass

    cfg["modelCascade"] = mc
    cfg["strictDoc"] = bool(cfg.get("strictDoc", False))
    cfg["triggerEnabled"] = bool(cfg.get("triggerEnabled", True))
    tmode = str(cfg.get("triggerMode") or "prefix").strip().lower()
    cfg["triggerMode"] = tmode if tmode in ("prefix", "contains") else "prefix"
    tkw = cfg.get("triggerKeywords")
    if isinstance(tkw, list):
        tkw = ", ".join(str(x).strip() for x in tkw if str(x).strip())
    elif not isinstance(tkw, str):
        tkw = str(tkw or "")
    cfg["triggerKeywords"] = tkw.strip()
    for k in ("systemPrompt", "globalContext", "handoffText"):
        if not isinstance(cfg.get(k), str):
            cfg[k] = str(cfg.get(k) or "")
    _sync_key_fields(cfg)
    return cfg


def load_config() -> dict:
    global _cfg_cache
    with _lock:
        stat = _stat_of(CONFIG_PATH)
        if stat is not None and _cfg_cache is not None \
                and _cfg_cache[0] == stat[0] and _cfg_cache[1] == stat[1]:
            return dict(_cfg_cache[2])

        cfg = dict(DEFAULTS)
        try:
            if stat is not None:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in DEFAULTS:
                        if k in data:
                            cfg[k] = data[k]
        except Exception:
            pass
        _normalize_config(cfg)
        if stat is not None:
            _cfg_cache = (stat[0], stat[1], dict(cfg))
        else:
            _cfg_cache = None
        return cfg


def save_config(patch: dict) -> dict:
    global _cfg_cache
    with _lock:
        cfg = load_config()
        prev_keys = list(cfg.get("geminiApiKeys") or [])
        for k in DEFAULTS:
            if k in (patch or {}):
                cfg[k] = patch[k]
        # chuan hoa lai
        if "targetGroupIds" in (patch or {}):
            v = patch.get("targetGroupIds")
            if isinstance(v, str):
                v = [x.strip() for x in v.split(",")]
            cfg["targetGroupIds"] = [str(x).strip() for x in (v or []) if str(x).strip()]
        if "geminiApiKey" in (patch or {}) and "geminiApiKeys" not in (patch or {}):
            cfg["geminiApiKeys"] = []
        _sync_key_fields(cfg, prev_keys=prev_keys)
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        # Cap nhat cache ngay (stat lai sau khi ghi).
        stat = _stat_of(CONFIG_PATH)
        _cfg_cache = (stat[0], stat[1], dict(cfg)) if stat else None
        return cfg


def load_cursor() -> dict:
    global _cursor_cache
    with _lock:
        stat = _stat_of(CURSOR_PATH)
        if stat is not None and _cursor_cache is not None \
                and _cursor_cache[0] == stat[0] and _cursor_cache[1] == stat[1]:
            return dict(_cursor_cache[2])
        cur = {}
        try:
            if stat is not None:
                with open(CURSOR_PATH, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    cur = d
        except Exception:
            pass
        _cursor_cache = (stat[0], stat[1], dict(cur)) if stat else None
        return cur


def save_cursor(cur: dict):
    global _cursor_cache
    with _lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CURSOR_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur or {}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CURSOR_PATH)
            stat = _stat_of(CURSOR_PATH)
            _cursor_cache = (stat[0], stat[1], dict(cur or {})) if stat else None
        except Exception:
            pass
