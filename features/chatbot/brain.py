"""Chatbot Zalo trong Nexus: poll gia-webhook + tra loi Echo/Gemini."""
import re
import time
import json
import os
import threading

try:
    from . import store as _store
    from .gemini_client import answer as gemini_answer, answer_with_status as gemini_answer_with_status
except Exception:  # pragma: no cover
    _store = None

    def gemini_answer(text, api_key="", **kwargs):
        return ""

    def gemini_answer_with_status(text, api_key="", **kwargs):
        out = gemini_answer(text, api_key=api_key, **kwargs)
        return {"text": out, "limited": False, "reason": ""}


def parse_trigger_keywords(raw) -> list[str]:
    """Tach danh sach tu khoa/lenh goi bot (phan tach bang dau phay, cham phay hoac xuong dong)."""
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[,;\r\n]+", str(raw or ""))
    out = []
    seen = set()
    for p in parts:
        kw = str(p).strip()
        if not kw:
            continue
        low = kw.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(kw)
    return out


def extract_triggered_query(text: str, cfg: dict | None = None) -> tuple[bool, str, str]:
    """Kiem tra tin nhan co khop tu khoa kich hoat hay khong va tach phan cau hoi.

    Returns:
        (matched, cleaned_query, matched_keyword)
    """
    src = (text or "").strip()
    if not src:
        return False, "", ""
    c = dict(cfg or {})
    if not bool(c.get("triggerEnabled", True)):
        return True, src, ""
    keywords = parse_trigger_keywords(c.get("triggerKeywords"))
    if not keywords:
        return False, "", ""
    keywords.sort(key=len, reverse=True)
    mode = str(c.get("triggerMode") or "prefix").strip().lower()

    if mode == "contains":
        for kw in keywords:
            prefix_bound = r"(?<!\w)" if (kw[0].isalnum() or kw[0] == "_") else ""
            suffix_bound = r"(?!\w)" if (kw[-1].isalnum() or kw[-1] == "_") else ""
            pat = re.compile(prefix_bound + re.escape(kw) + suffix_bound, re.IGNORECASE)
            m = pat.search(src)
            if m:
                before = src[:m.start()].rstrip(" \t,:;-–—")
                after = src[m.end():].lstrip(" \t,:;-–—")
                cleaned = re.sub(r"\s{2,}", " ", f"{before} {after}".strip())
                return True, cleaned, kw
        return False, "", ""

    low_src = src.lower()
    for kw in keywords:
        low_kw = kw.lower()
        if not low_src.startswith(low_kw):
            continue
        rest = src[len(kw):]
        # Tranh bat nham tien to giong nhau (vd /bot bat nham /botnet)
        if (kw[-1].isalnum() or kw[-1] == "_") and rest and (rest[0].isalnum() or rest[0] == "_"):
            continue
        cleaned = re.sub(r"^[\s,:;\-–—]+", "", rest).strip()
        return True, cleaned, kw
    return False, "", ""


def echo_answer(text: str, sender: str = "", group_id: str = "", sender_name: str = "") -> str:
    t = (text or "").strip()
    low = t.lower()
    salutation = f"Dạ Anh/Chị {sender_name}, " if sender_name else ""
    if low in ("ping", "hi", "hello", "alo"):
        return f"{salutation}Bot Zalo Jimmy online. Gửi tin bất kỳ để test."
    if low.startswith("/help"):
        return f"{salutation}Lệnh: ping | /help | /time | /id"
    if low.startswith("/time"):
        return f"{salutation}Bây giờ là {time.strftime('%Y-%m-%d %H:%M:%S')}"
    if low.startswith("/id"):
        return f"{salutation}group={group_id} sender={sender} msg=ok"
    return f"{salutation}Echo: {t}"


def answer(text: str, mode: str = "echo", api_key: str = "", sender: str = "",
           group_id: str = "", system_prompt: str = "", global_context: str = "",
           knowledge: str = "", models=None, strict_doc: bool = False,
           handoff_text: str = "", cfg: dict | None = None, sender_name: str = "") -> str:
    t = (text or "").strip()
    if not t:
        return ""
    c = dict(cfg or {})
    if _store is not None and not c:
        try:
            c = _store.load_config()
        except Exception:
            c = {}
    mode = str(mode or c.get("mode") or "echo").strip().lower()
    api_key = (api_key or c.get("geminiApiKey") or "").strip()
    system_prompt = system_prompt or str(c.get("systemPrompt") or "")
    global_context = global_context or str(c.get("globalContext") or "")
    models = models or c.get("modelCascade") or None
    strict_doc = bool(c.get("strictDoc", strict_doc))
    handoff_text = handoff_text or str(c.get("handoffText") or "")
    if knowledge == "" and _store is not None:
        try:
            knowledge = _store.retrieve_knowledge(t, top_k=3)
        except Exception:
            knowledge = ""

    # Thêm tên người gửi vào câu hỏi và bổ sung chỉ thị xưng hô cho AI
    query_text = t
    s_name = str(sender_name or "").strip()
    if s_name:
        query_text = f"[Người hỏi: {s_name}] {t}"
        name_instruction = (
            f" Khách hàng đang nhắn tin hỏi bạn tên là '{s_name}'. "
            f"Hãy xưng hô lịch sự và gọi tên khách hàng trong câu trả lời "
            f"(ví dụ: 'Dạ Anh/Chị {s_name}...', 'Thưa Anh/Chị {s_name}...'). "
            f"Điều này giúp trả lời đúng người và sát với từng khách hơn khi trong nhóm có nhiều thành viên."
        )
        system_prompt = (system_prompt + "\n" + name_instruction).strip()

    if mode == "gemini":
        key_items = []
        if _store is not None:
            try:
                key_items = _store.get_usable_api_keys(c)
            except Exception:
                key_items = []
        if not key_items and api_key:
            key_items = [{"label": "Key", "key": api_key, "limited": False}]
        for item in key_items:
            kval = str((item or {}).get("key") or "").strip()
            klabel = str((item or {}).get("label") or "Key").strip()
            if not kval:
                continue
            try:
                res = gemini_answer_with_status(
                    query_text, api_key=kval, system_prompt=system_prompt,
                    global_context=global_context, knowledge=knowledge,
                    models=models, strict_doc=strict_doc,
                    handoff_text=handoff_text,
                )
                out = str((res or {}).get("text") or "").strip()
                if out:
                    return out[:900] if len(out) > 900 else out
                if (res or {}).get("limited") and _store is not None:
                    reason = str((res or {}).get("reason") or "Hết limit")
                    try:
                        _store.mark_key_limited(kval, reason=reason)
                    except Exception:
                        pass
                    try:
                        from . import worker as _w
                        _w.log(f"[gemini] {klabel} (...{kval[-4:]}) {reason} -> danh dau limit & xoay key moi")
                    except Exception:
                        pass
            except Exception:
                pass
        return echo_answer(t, sender, group_id, sender_name=s_name)
    return echo_answer(t, sender, group_id, sender_name=s_name)
