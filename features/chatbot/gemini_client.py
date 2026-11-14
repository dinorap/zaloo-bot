"""Goi Gemini qua REST, khong can thu vien google-genai.

Hoc theo SocAI (packages/ai):
- globalContext (brand/rule chung) prepend truoc moi prompt
- systemPrompt (role) + context-compiler style
- tai lieu training / RAG: chi tra loi theo tai lieu, khong bia
- model cascade fallback (giong GeminiClient SocAI)

Toi uu / sua loi:
- Dung connection pool (core.zalo.http) thay ``requests.post`` moi lan.
- FAIL FAST: key sai (400/401/403) -> dung ngay, khong thu tiep 8 model
  trong cascade. Truoc day 1 key invalid = 8 request x 25s timeout = treo
  gan 3 phut moi cau hoi.
- Cache cau hoi trung lap trong khoang ngan (Gemini ton tien + cham).
"""
import hashlib
import threading
import time

from core.zalo import http as zalo_http

# Cache: key -> (timestamp, text)
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 180.0     # 3 phut
_CACHE_MAX = 100
_cache_lock = threading.Lock()

# Ma HTTP bao loi sai key -> dung ngay, khong thu model tiep theo.
FATAL_KEY_STATUS = (400, 401, 403)

DEFAULT_CASCADE = (
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
)

# Tien to key Gemini hop le. Google dang cap nhat dinh dang key: co ca
# "AIza..." (cu) va "AQ..." (moi). KHONG chặn theo prefix - chi can bao
# loi that tu HTTP response (400/401/403).
KNOWN_KEY_PREFIXES = ("AIza", "AQ.")

# Model da bi Google rut / khong con ton tai. Cascade luu trong config cu
# van co chung -> moi cau hoi ton ~50s cho cac model nay truoc khi fallback.
# (gemini-3.8-flash: read timeout 25s x2 lan -> ~51s mat)
# LUU Y: gemini-3.1-flash-lite va gemini-3.5-flash-lite VAN con hoat dong
# (do that: 200 OK, 0.7s / 3.9s) -> khong loai.
KNOWN_DEAD_MODELS = frozenset({
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-lite",
})

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Timeout moi model: (connect, read).
# 25s x 2 lan (retry) = 50s cho MOT model da chet -> nguoi dung cho 1p moi
# moi nhan duoc. 15s va KHONG retry la du (cascade da la fallback roi).
MODEL_TIMEOUT = (6, 15)

# Model chet (timeout / 404 / 503) -> bo qua trong khoang nay.
# Truong: Google rut model giua chung, cascade luu trong config cu la.
MODEL_DEAD_TTL = 600.0   # 10 phut
_MODEL_DEAD: dict[str, float] = {}
_model_dead_lock = threading.Lock()


def _is_dead(model: str) -> bool:
    now = time.time()
    with _model_dead_lock:
        ts = _MODEL_DEAD.get(model)
        if ts is None:
            return False
        if now - ts > MODEL_DEAD_TTL:
            _MODEL_DEAD.pop(model, None)
            return False
        return True


def _mark_dead(model: str) -> None:
    with _model_dead_lock:
        _MODEL_DEAD[model] = time.time()
    _log(f"model {model} bi loai tam thoi ({MODEL_DEAD_TTL:.0f}s) - bo qua o cac cau hoi sau")


def _revive_all() -> None:
    """Xoa danh sach model chet (dung khi user doi key/model)."""
    with _model_dead_lock:
        _MODEL_DEAD.clear()


def _log(msg: str):
    try:
        print(f"[gemini] {msg}", flush=True)
    except Exception:
        pass


def _build_prompt(text: str, system_prompt: str = "", global_context: str = "",
                  knowledge: str = "", strict_doc: bool = False,
                  handoff_text: str = "") -> str:
    t = (text or "").strip()
    role = (system_prompt or "").strip() or (
        "Bạn là chatbot Zalo tiếng Việt, thân thiện, ngắn gọn. "
        "Trả lời đúng ngôn ngữ khách dùng trong tin nhắn."
    )
    brand = (global_context or "").strip()
    doc = (knowledge or "").strip()
    parts = []
    if brand:
        parts.append(brand.strip())
        parts.append("")
    parts.append(role)
    parts.append("")
    parts.append("## Quy tắc bắt buộc (học theo SocAI)")
    parts.append("- Không bịa đặt số liệu, giá, chính sách. Không chắc thì nói rõ '(ước tính, cần xác minh)'.")
    parts.append("- Trả lời ngắn gọn, hợp chat Zalo (dưới ~900 ký tự).")
    if doc:
        parts.append("")
        parts.append("TÀI LIỆU CHUẨN (RAG) — nguồn thông tin ƯU TIÊN để trả lời:")
        parts.append("<<<")
        # gioi han prompt ~12000 ky tu de khoi vuot quota
        parts.append(doc[:12000])
        parts.append(">>>")
        parts.append("Quy tắc tài liệu:")
        parts.append("1. Ưu tiên trả lời dựa trên tài liệu trên + lịch sử chat. Không bịa giá/tính năng/chính sách không có trong tài liệu.")
        if strict_doc:
            hf = (handoff_text or "").strip() or "Dạ câu này em chưa có thông tin chính xác, để em báo tư vấn viên hỗ trợ Anh/Chị ngay ạ."
            parts.append(f"2. STRICT: tài liệu KHÔNG có thông tin thì CHỈ trả lời đúng 1 câu sau, không thêm thắt: {hf}")
        else:
            parts.append("2. Câu chào hỏi/cảm ơn/làm rõ nhu cầu thì trả lời bình thường.")
    parts.append("")
    parts.append(f"Tin nhắn của khách: {t}")
    return "\n".join(parts)


def _detect_limit_reason(status_code: int, body_text: str) -> str:
    low = str(body_text or "").lower()
    if status_code == 429 or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
        return f"Hết limit/quota (HTTP {status_code})"
    if status_code in (400, 401, 403) and any(
        tok in low for tok in ("api_key_invalid", "api key not valid", "api key expired", "suspended", "disabled", "limit")
    ):
        return f"Key không dùng được (HTTP {status_code})"
    return ""


def _cache_key(key: str, prompt: str) -> str:
    h = hashlib.sha1()
    h.update(key[:8].encode("utf-8", "ignore"))
    h.update(b"|")
    h.update(prompt.encode("utf-8", "ignore"))
    return h.hexdigest()


def _cache_get(ck: str) -> str | None:
    now = time.time()
    with _cache_lock:
        hit = _CACHE.get(ck)
        if not hit:
            return None
        ts, val = hit
        if now - ts > _CACHE_TTL:
            _CACHE.pop(ck, None)
            return None
        return val


def _cache_put(ck: str, value: str) -> None:
    with _cache_lock:
        if len(_CACHE) >= _CACHE_MAX:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
        _CACHE[ck] = (time.time(), value)


def clear_cache() -> None:
    with _cache_lock:
        _CACHE.clear()


def answer_with_status(text: str, api_key: str = "", system_prompt: str = "",
                       global_context: str = "", knowledge: str = "",
                       models=None, strict_doc: bool = False,
                       handoff_text: str = "") -> dict:
    key = (api_key or "").strip()
    t = (text or "").strip()
    if not key or not t:
        if not key:
            _log("thieu API key -> fallback echo")
        return {"text": "", "limited": False, "reason": ""}

    # Prefix la chi so (khong phai loai chat): Google dang cap nhat dinh dang
    # key (AIza... cu, AQ... moi). Chi can bao neu qua la khong dung format.
    if not key.startswith(KNOWN_KEY_PREFIXES):
        _log(f"key co tien to la (prefix={key[:4]}...). Hay lay key moi o "
             "aistudio.google.com/app/apikey neu API tra loi 400/403.")

    prompt = _build_prompt(t, system_prompt, global_context, knowledge, strict_doc, handoff_text)

    ck = _cache_key(key, prompt)
    cached = _cache_get(ck)
    if cached is not None:
        return {"text": cached, "limited": False, "reason": ""}

    cascade = [str(m).strip() for m in (models or list(DEFAULT_CASCADE)) if str(m).strip()]
    if not cascade:
        cascade = list(DEFAULT_CASCADE)

    last_err = ""
    limit_reason = ""
    for model in cascade:
        # Model da bi loai (timeout/404) -> bo qua ngay, khong ton thoi gian.
        if _is_dead(model):
            continue
        try:
            r = zalo_http.post(
                GEMINI_URL.format(model=model),
                params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=MODEL_TIMEOUT,
                retries=0,          # cascade da la fallback -> khong retry them
            )
        except Exception as e:
            last_err = f"{model} exception: {str(e)[:200]}"
            _log(last_err)
            # Timeout/loi ket noi -> model co the da chet -> loai tam thoi
            # de cac cau hoi sau khong ton thoi gian cho no nua.
            _mark_dead(model)
            continue

        if r.status_code != 200:
            last_err = f"{model} HTTP {r.status_code}: {r.text[:200]}"
            _log(last_err)
            limit_reason = _detect_limit_reason(r.status_code, r.text)
            if limit_reason:
                return {"text": "", "limited": True, "reason": limit_reason}
            # Key sai -> dung ngay, khong thu them model nao nua.
            if r.status_code in FATAL_KEY_STATUS:
                return {"text": "", "limited": True,
                        "reason": f"API key bi tu choi (HTTP {r.status_code}). "
                                  f"Kiem tra key tai trang Chatbot."}
            # 404 = model khong ton tai -> loai tam thoi.
            # 503 = server dap trong -> cung loai de khong chan cau hoi sau.
            if r.status_code in (404, 503):
                _mark_dead(model)
            continue

        try:
            data = r.json()
        except Exception:
            last_err = f"{model}: non-json"
            _log(last_err)
            continue

        cands = data.get("candidates") or []
        if not cands:
            last_err = f"{model}: khong co candidates"
            _log(last_err)
            continue
        parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
        out = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
        if out:
            _cache_put(ck, out)
            return {"text": out, "limited": False, "reason": ""}
        last_err = f"{model}: tra ve rong"
        _log(last_err)

    if last_err:
        _log(f"tat ca model loi -> fallback echo. Loi cuoi: {last_err[:220]}")
    return {"text": "", "limited": False, "reason": last_err[:160]}


def answer(text: str, api_key: str = "", system_prompt: str = "",
           global_context: str = "", knowledge: str = "",
           models=None, strict_doc: bool = False,
           handoff_text: str = "") -> str:
    res = answer_with_status(
        text, api_key=api_key, system_prompt=system_prompt,
        global_context=global_context, knowledge=knowledge,
        models=models, strict_doc=strict_doc, handoff_text=handoff_text,
    )
    return res.get("text") or ""
