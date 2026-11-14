"""Hang doi gui FIFO per-account cho Gateway Host.

- Chi Host om session that (cookies/zpwEnk/imei), Client khong bao gio cam key.
- Moi account 1 queue FIFO + worker rieng, gian cach >= sendGapSec (~1.2s)
  de khong trung clientId va khong bi Zalo rate-limit.
- Circuit breaker: 429 / 431 / loi session lien tiep -> mo mach, nghi
  cooldown roi tu dong thu lai. Dem duoc persist trong data/gateway_state.json.
"""
import json
import os
import threading
import time
import queue as queue_mod

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
STATE_PATH = os.path.join(DATA_DIR, "gateway_state.json")

_lock = threading.RLock()
_queues: dict = {}
_workers: dict = {}
_last_send: dict = {}
_breaker: dict = {}  # accountId -> {fail429, failSession, openUntil, openedAt}
_state_cache: dict = {}

DEFAULT_GAP = 1.2
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_COOLDOWN_SEC = 60.0


def _load_state() -> dict:
    global _state_cache
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    _state_cache = d
                    return d
    except Exception:
        pass
    return _state_cache or {}


def _save_state(patch: dict | None = None):
    global _state_cache
    with _lock:
        cur = _load_state()
        if patch:
            cur.update(patch)
        _state_cache = cur
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass


def _log(msg: str):
    try:
        print(f"[gateway] {msg}", flush=True)
    except Exception:
        pass
    try:
        from app import sse_broadcast as _b
        _b(f"[gateway] {msg}", "info")
    except Exception:
        pass


def _get_breaker(account_id: str) -> dict:
    b = _breaker.get(account_id)
    if not b:
        b = {"fail429": 0, "failSession": 0, "openUntil": 0.0, "openedAt": 0.0, "reason": ""}
        _breaker[account_id] = b
    return b


def circuit_status(account_id: str) -> dict:
    b = _get_breaker(str(account_id or "").strip())
    now = time.time()
    return {
        "open": bool(b.get("openUntil", 0) > now),
        "openUntil": b.get("openUntil", 0),
        "fail429": int(b.get("fail429") or 0),
        "failSession": int(b.get("failSession") or 0),
        "reason": b.get("reason") or "",
    }


def _record_success(account_id: str):
    b = _get_breaker(account_id)
    b["fail429"] = 0
    b["failSession"] = 0
    b["openUntil"] = 0.0
    b["reason"] = ""


def _record_failure(account_id: str, kind: str, detail: str = ""):
    b = _get_breaker(account_id)
    if kind == "rate":
        b["fail429"] = int(b.get("fail429") or 0) + 1
    else:
        b["failSession"] = int(b.get("failSession") or 0) + 1
    total = int(b.get("fail429") or 0) + int(b.get("failSession") or 0)
    if total >= CIRCUIT_FAIL_THRESHOLD:
        b["openUntil"] = time.time() + CIRCUIT_COOLDOWN_SEC
        b["openedAt"] = time.time()
        b["reason"] = detail[:180] or kind
        _log(f"circuit OPEN acc={account_id[:8]} ly do={b['reason']} nghi {CIRCUIT_COOLDOWN_SEC:.0f}s (429/431/session lien tiep)")
        _save_state({"breaker": {k: {"openUntil": v.get("openUntil"), "reason": v.get("reason")} for k, v in _breaker.items()}})


def _is_rate_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ("429", "431", "rate limit", "too many", "slow down", "error_code\": 429", "error_code\":431"))


def _is_session_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ("zpw_sek", "zpw enk", "zpwenk", "session", "phien dang nhap", "not login", "require login", "error_code\": -201", "error_code\":201"))


def _worker_loop(account_id: str, send_gap: float):
    from features.gateway.config import get_send_gap
    q = _queues[account_id]
    _log(f"queue worker start acc={account_id[:8]} gap={send_gap}s")
    while True:
        try:
            job = q.get()
        except Exception:
            time.sleep(0.2)
            continue
        try:
            b = _get_breaker(account_id)
            now = time.time()
            if b.get("openUntil", 0) > now:
                wait = b["openUntil"] - now
                _log(f"circuit con mo acc={account_id[:8]} bo job hien tai, doi {wait:.0f}s (job tra ve loi 429 tam)")
                try:
                    job["result"]["ok"] = False
                    job["result"]["error"] = f"Gateway dang ngat mach (429/431/session), thu lai sau {wait:.0f}s."
                    job["done"].set()
                except Exception:
                    pass
                time.sleep(min(wait, 5.0))
                continue
            # gian cach chong trung clientId
            gap = max(0.5, float(send_gap or get_send_gap()))
            wait_gap = gap - (time.time() - _last_send.get(account_id, 0.0))
            if wait_gap > 0:
                time.sleep(wait_gap)
            # thuc hien gui bang session that cua Host
            out = _do_send(account_id, job)
            _last_send[account_id] = time.time()
            try:
                job["result"].update(out)
            except Exception:
                pass
            if out.get("ok"):
                _record_success(account_id)
            else:
                err = str(out.get("error") or out.get("message") or "")
                if _is_rate_error(err) or _is_rate_error(json.dumps(out.get("decoded") or {}, ensure_ascii=False)[:500]):
                    _record_failure(account_id, "rate", err or "429/431")
                elif _is_session_error(err):
                    _record_failure(account_id, "session", err or "session")
                    # co gang cuu session 1 lan nhu app.py
                    try:
                        from app import _refresh_account_session_blocking as _refresh
                        _log(f"thu cuu session acc={account_id[:8]} sau loi session...")
                        _refresh(account_id)
                    except Exception as e:
                        _log(f"cuu session loi: {e}")
                else:
                    _record_failure(account_id, "other", err)
        except Exception as e:
            try:
                job["result"]["ok"] = False
                job["result"]["error"] = str(e)[:300]
                job["done"].set()
            except Exception:
                pass
            _log(f"worker loi acc={account_id[:8]}: {e}")
        finally:
            try:
                job["done"].set()
            except Exception:
                pass


def _extract_zalo_ids(decoded, fallback_cli=""):
    msg_id = ""
    cli_id = str(fallback_cli or "").strip()
    if isinstance(decoded, dict):
        inner = decoded.get("data") if isinstance(decoded.get("data"), dict) else decoded
        msg_id = str(inner.get("msgId") or decoded.get("msgId") or "").strip()
        if not cli_id:
            cli_id = str(inner.get("cliMsgId") or inner.get("clientId") or decoded.get("_clientId") or "").strip()
    return msg_id, cli_id


def _do_send(account_id: str, job: dict) -> dict:
    """Goi thang Zalo bang session Host. Khong bao gio tra key ra ngoai."""
    kind = job.get("kind") or "group"
    text = str(job.get("message") or "")
    target = str(job.get("target") or "")
    try:
        import sys
        # import muon de tranh circular khi worker chay trong app
        from app import _get_account_credentials
        from core.zalo.zalo_config import get_zpw_ver
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
        is_group = (kind != "sms")
        photo = job.get("photo_bytes")
        if photo:
            from features.messaging.send_photo import send_photo as _send_photo
            res = _send_photo(photo, job.get("photo_name") or "image.jpg", target, zpw_enk, cookies, imei, desc="", is_group=is_group, zpw_ver=get_zpw_ver())
            if res.get("ok") and text:
                from features.messaging.send_link import send_message_smart
                tr = send_message_smart(target, text, zpw_enk, cookies, imei, is_group=is_group, zpw_ver=get_zpw_ver())
                mid, cid = _extract_zalo_ids(tr.get("decoded"), tr.get("clientId") or "")
                return {"ok": bool(tr.get("ok")), "sentAsLink": bool(tr.get("sentAsLink")), "error": tr.get("error") or "", "decoded": tr.get("decoded"), "withPhoto": True, "zaloMsgId": mid, "cliMsgId": cid}
            mid, cid = _extract_zalo_ids(res.get("decoded"), res.get("clientId") or "")
            return {"ok": bool(res.get("ok")), "error": res.get("message") or "", "decoded": res.get("decoded"), "withPhoto": True, "zaloMsgId": mid, "cliMsgId": cid}
        file_b = job.get("file_bytes")
        if file_b:
            from features.messaging.send_file import send_file as _send_file
            res = _send_file(file_b, job.get("file_name") or "file.bin", target, zpw_enk, cookies, imei, is_group=is_group, zpw_ver=get_zpw_ver())
            if res.get("ok") and text:
                from features.messaging.send_link import send_message_smart
                tr = send_message_smart(target, text, zpw_enk, cookies, imei, is_group=is_group, zpw_ver=get_zpw_ver())
                mid, cid = _extract_zalo_ids(tr.get("decoded"), tr.get("clientId") or "")
                return {"ok": bool(tr.get("ok")), "sentAsLink": bool(tr.get("sentAsLink")), "error": tr.get("error") or "", "decoded": tr.get("decoded"), "withFile": True, "fileUrl": res.get("fileUrl") or "", "zaloMsgId": mid, "cliMsgId": cid}
            mid, cid = _extract_zalo_ids(res.get("decoded"), res.get("clientId") or "")
            return {"ok": bool(res.get("ok")), "error": res.get("message") or "", "decoded": res.get("decoded"), "withFile": True, "fileUrl": res.get("fileUrl") or "", "zaloMsgId": mid, "cliMsgId": cid}
        from features.messaging.send_link import send_message_smart
        res = send_message_smart(target, text, zpw_enk, cookies, imei, is_group=is_group, zpw_ver=get_zpw_ver())
        if res.get("ok"):
            mid, cid = _extract_zalo_ids(res.get("decoded"), res.get("clientId") or "")
            return {"ok": True, "sentAsLink": bool(res.get("sentAsLink")), "decoded": res.get("decoded"), "zaloMsgId": mid, "cliMsgId": cid}
        # mo rong thong tin loi de fuse nhan dien 429/431/session
        detail = str(res.get("error") or "") + " " + json.dumps(res.get("decoded") or {}, ensure_ascii=False)[:400]
        return {"ok": False, "error": (res.get("error") or "Zalo tu choi gui.") + (f" | {json.dumps(res.get('decoded') or {}, ensure_ascii=False)[:200]}" if res.get("decoded") else ""), "decoded": res.get("decoded"), "_raw": detail}
    except ValueError as e:
        return {"ok": False, "error": str(e)[:300]}
    except Exception as e:
        msg = str(e)[:300]
        # requests HTTPError mang status
        if "429" in msg or "431" in msg:
            return {"ok": False, "error": f"429/431 rate-limit: {msg}"}
        return {"ok": False, "error": msg}


def enqueue(account_id: str, kind: str, target: str, message: str = "", photo_bytes: bytes | None = None, photo_name: str = "", timeout: float = 60.0, file_bytes: bytes | None = None, file_name: str = "") -> dict:
    account_id = str(account_id or "").strip()
    if not account_id:
        return {"ok": False, "error": "Thieu accountId."}
    with _lock:
        if account_id not in _queues:
            _queues[account_id] = queue_mod.Queue()
        if account_id not in _workers or not _workers[account_id].is_alive():
            from features.gateway.config import get_send_gap
            t = threading.Thread(target=_worker_loop, args=(account_id, get_send_gap()), daemon=True, name=f"gw-send-{account_id[:6]}")
            _workers[account_id] = t
            t.start()
    job = {"kind": kind, "target": target, "message": message,
           "photo_bytes": photo_bytes, "photo_name": photo_name,
           "file_bytes": file_bytes, "file_name": file_name,
           "result": {"ok": False, "error": "chua xu ly"}, "done": threading.Event(), "at": time.time()}
    _queues[account_id].put(job)
    ok = job["done"].wait(timeout=max(5.0, float(timeout or 60)))
    if not ok:
        return {"ok": False, "error": "Hang doi qua tai, thu lai sau.", "queued": True}
    return dict(job["result"])


def queue_depths() -> dict:
    with _lock:
        return {aid: q.qsize() for aid, q in _queues.items()}
