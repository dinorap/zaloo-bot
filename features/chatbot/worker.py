"""Worker chatbot chay trong tien trinh Nexus duy nhat.

Poll gia-webhook: moi pollIntervalSec goi get-last-msgs cho cac nhom target,
baseline + dedup + loc dung group + chong loop, tra loi Echo/Gemini ve nhom dich.
"""
import threading
import time
import random
import re
from collections import deque

from . import store as _store
from . import brain as _brain

_lock = threading.RLock()
_thread = None
_stop = threading.Event()
_logs = deque(maxlen=200)
_last_send_ts = 0.0


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _logs.append(line)
    print(f"[chatbot] {msg}", flush=True)


def get_logs(limit: int = 80):
    with _lock:
        items = list(_logs)[-int(limit or 80):]
    return items


def _text_of(m: dict) -> str:
    return str((m or {}).get("text") or "").strip()


def _norm_text(t: str) -> str:
    """Chuan hoa text de so sanh khop (bo khoang trang, lower, cat ngan)."""
    return re.sub(r"\s+", " ", str(t or "").strip().lower())[:160]


_sender_names_cache: dict[str, tuple[str, float]] = {}


def _resolve_sender_name(sender_uid: str, cookies: str = "", zpw_enk: str = "", imei: str = "") -> str:
    """Tra cứu tên hiển thị của người gửi (cache bộ nhớ 24h, unread store, profile API)."""
    s = str(sender_uid or "").strip()
    if not s or s in ("0", ""):
        return ""
    cached = _sender_names_cache.get(s)
    if cached and (time.time() - cached[1]) < 86400:
        return cached[0]

    # Check unread manager senderNames cache
    try:
        from features.messages import unread_manager as _um
        store = _um._load_store()
        sn = str((store.get("senderNames") or {}).get(s) or "").strip()
        if sn:
            _sender_names_cache[s] = (sn, time.time())
            return sn
    except Exception:
        pass

    # Gọi profile Zalo để lấy tên thật
    if cookies and zpw_enk:
        try:
            from features.profiles.get_single_profile import get_single_profile
            res = get_single_profile([s], zpw_enk, cookies, imei=imei)
            if isinstance(res, dict) and s in res:
                prof = res.get(s) or {}
                name = str(prof.get("displayName") or prof.get("zaloName") or prof.get("dName") or "").strip()
                if name:
                    _sender_names_cache[s] = (name, time.time())
                    return name
        except Exception as e:
            log(f"[sender-name] loi lay profile {s}: {e}")

    return ""


def _loop():
    global _last_send_ts
    from features.accounts.account_manager import get_account
    from features.messaging.last_messages import fetch_last_messages
    from features.messaging.send_link import send_message_smart
    from core.zalo.zalo_config import get_zpw_ver

    log("worker started")
    recent_replies_by_group = {}
    # Lop chong loop bo sung: khop text (de biet chinh xac noi dung) + cooldown.
    recent_reply_keys: dict[str, deque] = {}
    last_reply_at: dict[str, float] = {}
    backoff = 0
    while not _stop.is_set():
        try:
            cfg = _store.load_config()
            if not cfg.get("enabled"):
                time.sleep(1.0)
                continue
            account_id = str(cfg.get("accountId") or "").strip()
            groups = [str(g).strip() for g in (cfg.get("targetGroupIds") or []) if str(g).strip()]
            # destGroupId: để trống = trả lời đúng nhóm nguồn (khuyên dùng cho multi-nhóm).
            # Nếu đặt 1 ID cố định thì mọi nhóm nguồn đều dồn về đó (chế độ gom cũ).
            dest_fixed = str(cfg.get("destGroupId") or "").strip()
            reply_to_source = bool(cfg.get("replyToSource", True))
            if not account_id or not groups:
                time.sleep(2.0)
                continue
            acc = get_account(account_id)
            if not acc:
                log(f"khong tim thay account {account_id[:8]}")
                time.sleep(5.0)
                continue
            cookies = str(acc.get("cookies") or "")
            zpw_enk = str(acc.get("zpwEnk") or "")
            imei = str(acc.get("imei") or "")
            own_uid = str(acc.get("uid") or "").strip()
            import re as _re
            has_sek = bool(_re.search(r"(?:^|;\s*)zpw_sek=", cookies or ""))
            if not (cookies and zpw_enk and imei and has_sek):
                log("thieu session (cookies/zpwEnk/imei/zpw_sek) - mo lai tai khoan o trang Tai khoan")
                time.sleep(5.0)
                continue

            cursor = _store.load_cursor()
            thread_map = {f"{gid}_1": str((cursor.get(gid) or {}).get("msgId") or "0") for gid in groups}
            res = fetch_last_messages(thread_map, cookies, zpw_enk, imei, zpw_ver=get_zpw_ver())
            if not res.get("ok"):
                backoff = min(backoff + 1, 6)
                wait = min(60.0, float(cfg.get("pollIntervalSec") or 1.5) * (2 ** backoff))
                log(f"loi poll: {res.get('message')} -> nghi {wait:.1f}s")
                time.sleep(wait)
                continue
            backoff = 0
            allowed = set(groups)
            changed = False
            for m in res.get("groupMsgs", []) or []:
                gid = str(m.get("groupId") or m.get("idTo") or "").strip()
                if gid not in allowed:
                    continue
                msg_id = str(m.get("msgId") or "").strip()
                ts = int(m.get("ts") or 0)
                if not gid or not msg_id:
                    continue
                prev = cursor.get(gid) or {}
                if not prev.get("msgId"):
                    cursor[gid] = {"msgId": msg_id, "ts": ts}
                    log(f"baseline {gid} -> {msg_id}")
                    changed = True
                    continue
                if msg_id == prev.get("msgId") or ts <= int(prev.get("ts") or 0):
                    continue
                cursor[gid] = {"msgId": msg_id, "ts": ts}
                changed = True
                sender = str(m.get("senderUid") or m.get("uidFrom") or "").strip()
                text = _text_of(m)
                is_self = sender in ("", "0") or (own_uid and sender == own_uid)
                if is_self and not bool(cfg.get("allowSelfReply", True)):
                    continue
                if not text:
                    continue
                # chong loop theo từng nhóm: bỏ qua tin giống hệt tin bot vừa gửi vào
                # CHÍNH nhóm đó (<120s) + tin echo/bot. Không dùng 1 biến chung
                # cho mọi nhóm (sẽ bỏ sót tin trùng nội dung ở nhóm khác).
                low = text.lower()
                if text.startswith("Echo:") or low.startswith("bot zalo nexus") or text.startswith("🤖"):
                    log(f"[skip] tin cua bot -> bo qua")
                    continue
                now_ts = time.time()
                _recent = recent_replies_by_group.get(gid)
                if _recent and any(text == prev_txt and (now_ts - prev_ts) < 180 for prev_txt, prev_ts in _recent):
                    log(f"[skip] trung tin bot vua gui o nhom {gid} -> bo qua")
                    continue

                # --- Lop chong loop bo sung (THOI_DIEN) ---
                # 1) Cooldown moi nhom: tranh bot tra loi lien tuc trong 1 nhóm.
                _last_reply_ts = last_reply_at.get(gid, 0.0)
                if (now_ts - _last_reply_ts) < float(cfg.get("replyCooldownSec") or 3.0):
                    continue
                # 2) Khop text (khong phan biet hoa/thuong, bo khoang trang thua).
                _key = _norm_text(text)
                if _key and _key in recent_reply_keys.get(gid, ()):  # noqa: F821
                    log(f"[skip] noi dung trung tin bot vua gui o nhom {gid} -> bo qua")
                    continue
                # 3) Tin chinh acc gui -> bo qua (Zalo echo lai). Bat len mac dinh
                #    de tranh vong lap vo han (bot -> tin -> bot -> tin ...).
                if own_uid and sender == own_uid and not bool(cfg.get("allowSelfReply", True)):
                    continue

                matched, query_text, matched_kw = _brain.extract_triggered_query(text, cfg)
                if not matched:
                    log(f"[skip] group={gid} khong co tu khoa kich hoat -> bo qua")
                    continue

                # Nhóm đích: mặc định trả lời đúng nhóm nguồn (multi-nhóm).
                # Chỉ dồn về 1 nhóm cố định khi user tắt "trả lời đúng nhóm nguồn".
                dest = gid if reply_to_source else (dest_fixed or gid)
                sender_name = ""
                if bool(cfg.get("includeSenderName", True)):
                    sender_name = _resolve_sender_name(sender, cookies=cookies, zpw_enk=zpw_enk, imei=imei)

                if matched_kw and not query_text:
                    log(f"[trigger] group={gid} kw={matched_kw} thieu cau hoi -> gui huong dan")
                    name_prefix = f"Chào Anh/Chị {sender_name}! " if sender_name else ""
                    reply = f"🤖 {name_prefix}Bạn hãy nhập câu hỏi sau lệnh {matched_kw} (vd: {matched_kw} giá sản phẩm này bao nhiêu) để mình hỗ trợ nhé."
                else:
                    sender_label = f"{sender} ({sender_name})" if sender_name else sender
                    log(f"[new] group={gid} sender={sender_label} -> dest={dest} kw={matched_kw or '*'} text={query_text[:160]}")
                    reply = _brain.answer(query_text, mode=cfg.get("mode") or "echo",
                                          api_key=cfg.get("geminiApiKey") or "",
                                          sender=sender, group_id=gid, cfg=cfg,
                                          sender_name=sender_name)
                if not reply:
                    continue
                # Neu mode=gemini ma van ra Echo -> Gemini loi, bao ro de user biet
                if str(cfg.get("mode") or "").strip().lower() == "gemini" and (
                        reply.startswith("Echo:") or reply.startswith("Bot Zalo Nexus online")):
                    log(f"[warn] Gemini loi -> fallback echo. Kiem tra key (phai bat dau bang AIza) o log [gemini] trong terminal.")
                # gian cach gui chong ban / trung clientId
                gap = max(0.5, float(cfg.get("sendGapSec") or 1.2))
                wait_gap = gap - (time.time() - _last_send_ts)
                if wait_gap > 0:
                    time.sleep(wait_gap)
                try:
                    out = send_message_smart(dest, reply, zpw_enk, cookies, imei, is_group=True)
                    _last_send_ts = time.time()
                    ok = bool(out.get("ok"))
                    log(f"[reply] src={gid} -> dest={dest} ok={ok} err={out.get('error') or ''} rep={reply[:120]}")
                    if ok:
                        q = recent_replies_by_group.setdefault(gid, deque(maxlen=15))
                        q.append((reply, time.time()))
                        # Luu khop text de chong loop kiem tra chinh xac noi dung.
                        kq = recent_reply_keys.setdefault(gid, deque(maxlen=15))
                        kq.append(_norm_text(reply))
                        last_reply_at[gid] = time.time()
                except Exception as e:
                    log(f"[reply] loi: {e}")
            if changed:
                _store.save_cursor(cursor)
        except Exception as e:
            log(f"loop loi: {e}")
        try:
            base = max(0.5, float(_store.load_config().get("pollIntervalSec") or 1.5))
        except Exception:
            base = 1.5
        _stop.wait(max(0.3, base + random.uniform(0, 0.4)))
    log("worker stopped")


def ensure_started():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="nexus-chatbot")
        _thread.start()
        return True


def is_running() -> bool:
    with _lock:
        return bool(_thread and _thread.is_alive())


def status() -> dict:
    cfg = _store.load_config()
    keys = cfg.get("geminiApiKeys") or []
    limited_cnt = sum(1 for x in keys if isinstance(x, dict) and x.get("limited"))
    usable_cnt = max(0, len(keys) - limited_cnt)
    return {
        "threadAlive": is_running(),
        "enabled": bool(cfg.get("enabled")),
        "accountId": cfg.get("accountId") or "",
        "targetGroupIds": cfg.get("targetGroupIds") or [],
        "destGroupId": cfg.get("destGroupId") or "",
        "replyToSource": bool(cfg.get("replyToSource", True)),
        "mode": cfg.get("mode") or "echo",
        "pollIntervalSec": cfg.get("pollIntervalSec"),
        "hasGeminiKey": usable_cnt > 0 or bool((cfg.get("geminiApiKey") or "").strip()),
        "totalGeminiKeys": len(keys),
        "usableGeminiKeys": usable_cnt,
        "limitedGeminiKeys": limited_cnt,
        "triggerEnabled": bool(cfg.get("triggerEnabled", True)),
        "triggerMode": cfg.get("triggerMode") or "prefix",
        "triggerKeywords": cfg.get("triggerKeywords") or "",
    }
