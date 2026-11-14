"""Gateway forwarder worker (P4): chay trong Host, poll + persist cursor + SSE."""
import threading
import time
import random
from collections import deque

from . import store as _store
from . import send_queue as _queue

_lock = threading.RLock()
_thread = None
_stop = threading.Event()
_logs = deque(maxlen=200)


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _logs.append(line)
    print(f"[gateway-fw] {msg}", flush=True)
    try:
        from app import sse_broadcast as _b
        _b(f"[gateway-fw] {msg}", "info")
    except Exception:
        pass


def get_logs(limit: int = 80):
    with _lock:
        return list(_logs)[-int(limit or 80):]


def _loop():
    log("forwarder started")
    while not _stop.is_set():
        try:
            cfg = _store.load_config()
            if not cfg.get("enabled"):
                time.sleep(1.0)
                continue
            account_id = str(cfg.get("accountId") or "").strip()
            routes = [r for r in (cfg.get("routes") or []) if r.get("dest_group_id") and r.get("source_group_ids")]
            if not account_id or not routes:
                time.sleep(2.0)
                continue
            # gop tat ca source de quet 1 lan
            by_source: dict = {}
            for r in routes:
                for gid in r.get("source_group_ids") or []:
                    by_source.setdefault(str(gid), []).append(r)
            sources = sorted(by_source.keys())
            try:
                from app import _get_account_credentials
                from features.messaging.last_messages import fetch_last_messages
                from core.zalo.zalo_config import get_zpw_ver
                _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
            except Exception as e:
                log(f"thieu session acc={account_id[:8]}: {e}")
                time.sleep(5.0)
                continue
            cursor = _store.load_cursor()
            thread_map = {f"{gid}_1": str((cursor.get(gid) or {}).get("msgId") or "0") for gid in sources}
            try:
                res = fetch_last_messages(thread_map, cookies, zpw_enk, imei, zpw_ver=get_zpw_ver())
            except Exception as e:
                msg = str(e)
                if "429" in msg or "431" in msg:
                    _queue._record_failure(account_id, "rate", msg[:150])
                    log(f"429/431 khi poll -> nghi 60s: {msg[:150]}")
                    time.sleep(60.0)
                else:
                    log(f"poll loi: {msg[:200]}")
                    time.sleep(5.0)
                continue
            if not res.get("ok"):
                log(f"poll loi: {res.get('message')}")
                time.sleep(5.0)
                continue
            changed = False
            for m in res.get("groupMsgs", []) or []:
                gid = str(m.get("groupId") or m.get("idTo") or "").strip()
                if gid not in by_source:
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
                text = str((m or {}).get("text") or "").strip()
                if not text:
                    continue
                if text.startswith("Echo:") or text.startswith("🤖"):
                    continue
                log(f"[new] {gid} -> {text[:120]}")
                for route in by_source[gid]:
                    dest = route.get("dest_group_id")
                    out = _queue.enqueue(account_id, "group", dest, text, timeout=60)
                    log(f"[forward] {gid} -> {dest} ok={out.get('ok')} err={out.get('error') or ''}")
            if changed:
                _store.save_cursor(cursor)
        except Exception as e:
            log(f"loop loi: {e}")
        try:
            base = max(5, int(_store.load_config().get("intervalSec") or 15))
        except Exception:
            base = 15
        _stop.wait(base + random.uniform(0, 1.0))
    log("forwarder stopped")


def ensure_started():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="gateway-fw")
        _thread.start()
        return True


def is_running() -> bool:
    with _lock:
        return bool(_thread and _thread.is_alive())
