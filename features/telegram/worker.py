"""Worker chatbot Telegram chay trong tien trinh Jimmy BOT CHAT.

Long polling getUpdates -> loc chat duoc phep -> tra loi bang bo nao Gemini
dung chung voi chatbot Zalo (features.chatbot.brain) va tai lieu RAG chung.
"""
import threading
import time
from collections import deque

from . import store as _store
from . import telegram_client as _tg

try:
    from features.chatbot import store as _zalo_store
    from features.chatbot import brain as _brain
except Exception:  # pragma: no cover
    _zalo_store = None
    _brain = None

_lock = threading.RLock()
_thread = None
_stop = threading.Event()
_logs = deque(maxlen=200)
_last_send_ts = 0.0
_offset = 0


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _logs.append(line)
    _store.append_log({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": str(msg)})
    print(f"[telegram] {msg}", flush=True)


def get_logs(limit: int = 120):
    with _lock:
        items = list(_logs)[-int(limit or 120):]
    return items


def _resolve_ai_config(cfg: dict) -> dict:
    """Lay cau hinh AI cho Telegram.

    Uu tien: systemPrompt, globalContext, strictDoc, handoffText luu rieng
    trong cau hinh Telegram (khong dung chung voi Zalo).
    API key, cascade, trigger dung chung tu kho shared hoac key rieng Telegram.
    """
    c = dict(cfg or {})
    shared = {}
    if _zalo_store is not None:
        try:
            shared = _zalo_store.load_shared_config()
        except Exception:
            shared = {}

    ai = {
        "mode": shared.get("mode") or c.get("mode") or "gemini",
        "api_key": str(c.get("geminiApiKey") or "").strip(),
        "systemPrompt": str(c.get("systemPrompt") or shared.get("systemPrompt") or "Bạn là trợ lý Telegram của shop, trả lời ngắn gọn, lịch sự, đúng ý khách."),
        "globalContext": str(c.get("globalContext") or shared.get("globalContext") or ""),
        "models": shared.get("modelCascade") or c.get("modelCascade") or None,
        "strictDoc": bool(c.get("strictDoc", shared.get("strictDoc"))),
        "handoffText": str(c.get("handoffText") or shared.get("handoffText") or "Dạ câu này em chưa có thông tin chính xác, để em bảo tư vấn viên hỗ trợ Anh/Chị ngay ạ."),
    }
    # Key rieng cua Telegram (khi khach chon "dung key rieng") duoc uu tien.
    if not ai["api_key"] and _zalo_store is not None:
        try:
            usable = _zalo_store.get_usable_api_keys()
        except Exception:
            usable = []
        if usable:
            ai["api_key"] = str((usable[0] or {}).get("key") or "")
        else:
            ai["api_key"] = str(shared.get("geminiApiKey") or "")
    return ai


def _answer(text: str, cfg: dict, chat_title: str, sender_name: str, cmd: dict | None = None) -> str:
    """Tra loi mot tin nhan Telegram bang tai lieu va phong cach rieng cua Telegram."""
    ai = _resolve_ai_config(cfg)
    knowledge = ""
    try:
        knowledge = _store.retrieve_knowledge(text, top_k=3)
    except Exception:
        pass
    if not knowledge and _zalo_store is not None:
        try:
            knowledge = _zalo_store.retrieve_knowledge(text, top_k=3)
        except Exception:
            knowledge = ""
    if _brain is None:
        return f"Jimmy BOT CHAT nhan duoc: {text}"

    # Xử lý Custom Command nếu có
    if cmd and isinstance(cmd, dict):
        resp_type = str(cmd.get("responseType") or "ai").strip().lower()
        fixed = str(cmd.get("fixedReply") or "").strip()
        cmd_kw = str(cmd.get("command") or "").strip()
        cmd_name = str(cmd.get("name") or cmd_kw).strip()
        if resp_type == "fixed" or (not text and fixed):
            salutation = f"Dạ Anh/Chị {sender_name}, " if sender_name else ""
            reply_body = fixed or str(cmd.get("context") or "").strip()
            if reply_body:
                return f"{salutation}{reply_body}"

        cmd_rule = str(cmd.get("rule") or "").strip()
        if cmd_rule:
            ai["systemPrompt"] = (
                f"{str(ai.get('systemPrompt') or '')}\n\n"
                f"[QUY TẮC & CHỈ THỊ RIÊNG CHO LỆNH {cmd_kw} - {cmd_name}]:\n"
                f"{cmd_rule}\n"
                f"Hãy áp dụng triệt để quy tắc trên cho câu trả lời này."
            ).strip()

        cmd_ctx = str(cmd.get("context") or "").strip()
        if cmd_ctx:
            if knowledge:
                knowledge = f"[TÀI LIỆU CHUYÊN SÂU LỆNH {cmd_kw} - {cmd_name}]:\n{cmd_ctx}\n\n---\n[TÀI LIỆU KHÁC]:\n{knowledge}".strip()
            else:
                knowledge = f"[TÀI LIỆU CHUYÊN SÂU LỆNH {cmd_kw} - {cmd_name}]:\n{cmd_ctx}".strip()

        if not text:
            text = f"Khách hàng vừa gọi lệnh {cmd_kw} ({cmd_name}). Hãy chào hỏi và giới thiệu ngắn gọn các thông tin chính theo tài liệu trên, gợi mở khách đặt câu hỏi."

    mode = str(ai.get("mode") or "echo").strip().lower()
    if mode == "gemini":
        # Xay dung prompt rieng cho Telegram de co context nguoi gui + ten chat,
        # roi goi Gemini truc tiep qua gemini_client (co xoay key + fallback model).
        try:
            from features.chatbot import gemini_client as _gc
        except Exception:
            _gc = None
        if _gc is not None:
            prefix_parts = []
            system_prompt = str(ai.get("systemPrompt") or "")
            if cfg.get("includeSenderName", True) and sender_name:
                prefix_parts.append(f"Nguoi gui: {sender_name}")
                name_instruction = (
                    f" Khách hàng đang nhắn tin hỏi bạn tên là '{sender_name}'. "
                    f"Hãy xưng hô lịch sự và gọi tên khách hàng trong câu trả lời (ví dụ: 'Dạ Anh/Chị {sender_name}...', 'Thưa Anh/Chị {sender_name}...')."
                )
                system_prompt = (system_prompt + "\n" + name_instruction).strip()
            if chat_title:
                prefix_parts.append(f"Ten chat: {chat_title}")
            full_query = (" | ".join(prefix_parts) + " | " + text) if prefix_parts else text
            key = str(ai.get("api_key") or "").strip()
            if key:
                res = _gc.answer_with_status(
                    full_query,
                    api_key=key,
                    system_prompt=system_prompt,
                    global_context=ai.get("globalContext") or "",
                    knowledge=knowledge,
                    models=ai.get("models"),
                    strict_doc=bool(ai.get("strictDoc")),
                    handoff_text=ai.get("handoffText") or "",
                )
                out = str((res or {}).get("text") or "").strip()
                if out:
                    return out
                if (res or {}).get("limited"):
                    log(f"Gemini het limit: {(res or {}).get('reason')} - thu key khac hoac bat che do Echo")
                    if _zalo_store is not None:
                        try:
                            _zalo_store.mark_key_limited(key, reason=str((res or {}).get("reason") or "Het limit"))
                        except Exception:
                            pass
    # fallback: dung chung brain cua chatbot Zalo (echo + phan tich trigger)
    try:
        out = _brain.answer(
            text,
            mode=mode,
            api_key=str(ai.get("api_key") or ""),
            sender=sender_name,
            group_id=chat_title,
            system_prompt=ai.get("systemPrompt") or "",
            global_context=ai.get("globalContext") or "",
            knowledge=knowledge,
            models=ai.get("models"),
            strict_doc=bool(ai.get("strictDoc")),
            handoff_text=ai.get("handoffText") or "",
            cmd=cmd,
        )
        if out:
            return str(out)
    except Exception as e:
        log(f"Loi khi tra loi: {str(e)[:160]}")
    return f"Jimmy BOT CHAT nhan duoc: {text}"


def _is_allowed(cfg: dict, rec: dict) -> bool:
    allowed = [str(x) for x in (cfg.get("allowedChatIds") or []) if str(x).strip()]
    if not allowed:
        return True
    return str(rec.get("chatId") or "") in allowed


def _chat_allowed_by_type(cfg: dict, rec: dict) -> bool:
    t = str(rec.get("chatType") or "")
    if cfg.get("onlyPrivateChats") and t != _tg.CHAT_PRIVATE:
        return False
    return True


def _apply_trigger(cfg: dict, text: str) -> tuple[bool, str]:
    """Neu bat trigger, tin nhan phai chua tu khoa. Tra (match, cau hoi da cat).

    Luon dung bo trigger DUNG CHUNG voi chatbot Zalo, nhung cho phep
    tat rieng cho Telegram qua truong "triggerEnabledTelegram".
    """
    shared = {}
    if _zalo_store is not None:
        try:
            shared = _zalo_store.load_shared_config()
        except Exception:
            shared = {}

    on = bool(shared.get("triggerEnabled", True)) if shared else bool(cfg.get("triggerEnabled", True))
    # Neu khach bat trigger chung, tung nen tang co the tat rieng.
    if not on and cfg.get("triggerEnabled") is not None:
        on = bool(cfg.get("triggerEnabled"))
    kws_raw = shared.get("triggerKeywords") if shared else cfg.get("triggerKeywords")
    if kws_raw is None:
        kws_raw = cfg.get("triggerKeywords")

    if not on:
        return True, text
    kws = [k.strip() for k in str(kws_raw or "").split(",") if k.strip()]
    if not kws:
        return True, text
    low = text.lower()
    mode = str((shared.get("triggerMode") if shared else cfg.get("triggerMode")) or "prefix").strip().lower()
    for kw in sorted(kws, key=len, reverse=True):
        low_kw = kw.lower()
        if mode == "contains":
            if low_kw in low:
                return True, text.replace(kw, "", 1).strip() or text
        elif low.startswith(low_kw):
            return True, text[len(kw):].lstrip(" ,.:;-").strip() or text
    return False, text


def _loop():
    global _last_send_ts, _offset
    log("worker started")
    while not _stop.is_set():
        try:
            cfg = _store.load_config()
            if not cfg.get("enabled"):
                time.sleep(1.0)
                continue
            token = str(cfg.get("botToken") or "").strip()
            if not token:
                time.sleep(1.5)
                continue

            res = _tg.get_updates(token, offset=_offset, timeout=20)
            if not res.get("ok"):
                err = str(res.get("error") or "")
                code = int(res.get("error_code") or 0)
                if code == 409:
                    log("Token dang bi mot tien trinh khac dung (loi 409). Hay tat bot o may khac hoac dung chung token nay.")
                    time.sleep(8)
                    continue
                if code == 401:
                    log("Bot Token khong hop le. Kiem tra lai token trong tab Telegram.")
                    time.sleep(8)
                    continue
                log(f"getUpdates loi: {err[:160]}")
                time.sleep(3)
                continue

            updates = res.get("result") or []
            for upd in updates:
                try:
                    uid = upd.get("update_id")
                    if uid is not None:
                        _offset = max(_offset, int(uid) + 1)
                except Exception:
                    pass
                rec = _tg.parse_update(upd)
                if not rec or not rec.get("chatId"):
                    continue
                # Ghi nho chat vua nhan tin: khach dung nut "Quet Chat ID" de lay ma.
                try:
                    _store.remember_chat(rec.get("chatId"), rec.get("chatTitle"), rec.get("chatType"))
                except Exception:
                    pass
                if cfg.get("ignoreBotMessages", True) and rec.get("senderIsBot"):
                    continue
                text = str(rec.get("text") or "").strip()
                if not text:
                    continue
                if not _is_allowed(cfg, rec):
                    log(f"Bo qua tin nhan tu chat khong duoc phep: {rec.get('chatTitle')} (id {rec.get('chatId')})")
                    continue
                if not _chat_allowed_by_type(cfg, rec):
                    continue

                custom_cmd = None
                if _brain is not None:
                    try:
                        cmds = (shared.get("customCommands") if shared else None) or cfg.get("customCommands")
                        custom_cmd, cmd_query = _brain.find_matching_custom_command(text, cmds)
                    except Exception:
                        custom_cmd = None

                if custom_cmd:
                    matched = True
                    query = cmd_query
                    matched_cmd = custom_cmd
                else:
                    matched, query = _apply_trigger(cfg, text)
                    matched_cmd = None

                if not matched:
                    continue

                answer = _answer(query, cfg, rec.get("chatTitle") or "", rec.get("senderName") or "", cmd=matched_cmd)
                if not answer:
                    continue

                limit = int(cfg.get("maxMessageChars") or 4000)
                if len(answer) > limit:
                    answer = answer[:limit - 20] + "…"

                gap = float(cfg.get("sendGapSec") or 1.0)
                wait = gap - (time.time() - _last_send_ts)
                if wait > 0:
                    _stop.wait(wait)

                s_name = str(rec.get("senderName") or "").strip()
                tagged_answer = answer
                if s_name and not tagged_answer.startswith(f"@{s_name}"):
                    tagged_answer = f"@{s_name}\n{answer}"

                send_res = _tg.send_message(
                    token,
                    rec["chatId"],
                    tagged_answer,
                    reply_to_message_id=rec.get("messageId"),
                )
                _last_send_ts = time.time()
                if send_res.get("ok"):
                    log(f"Da tra loi cho {rec.get('chatTitle')} ({rec.get('chatId')}): {text[:60]}")
                else:
                    log(f"Gui tin that bai: {str(send_res.get('error'))[:160]}")
        except Exception as e:
            log(f"Loi vong lap: {str(e)[:200]}")
            time.sleep(2)


def ensure_started():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="jimmy-telegram-bot")
        _thread.start()
        return True


def is_running() -> bool:
    with _lock:
        return bool(_thread and _thread.is_alive())


def stop():
    _stop.set()


def status() -> dict:
    cfg = _store.load_config()
    shared = {}
    if _zalo_store is not None:
        try:
            shared = _zalo_store.load_shared_config()
        except Exception:
            shared = {}
    return {
        "threadAlive": is_running(),
        "enabled": bool(cfg.get("enabled")),
        "hasToken": bool(str(cfg.get("botToken") or "").strip()),
        "botUsername": cfg.get("botUsername") or "",
        "botName": cfg.get("botName") or "",
        "mode": shared.get("mode") or cfg.get("mode") or "gemini",
        "allowedChatIds": cfg.get("allowedChatIds") or [],
        "pollIntervalSec": cfg.get("pollIntervalSec"),
        "onlyPrivateChats": bool(cfg.get("onlyPrivateChats")),
        "triggerEnabled": bool(shared.get("triggerEnabled", True)) if shared else bool(cfg.get("triggerEnabled")),
        "triggerKeywords": shared.get("triggerKeywords") if shared else cfg.get("triggerKeywords"),
    }
