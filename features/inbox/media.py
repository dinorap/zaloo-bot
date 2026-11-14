"""Media local: luu file dinh kem + phuc vu qua link token (hoc zca-bridge)."""
import hashlib
import json
import os
import secrets
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
MEDIA_DIR = os.path.join(DATA_DIR, "inbox_media")
MAP_PATH = os.path.join(DATA_DIR, "inbox_media.json")
_lock = threading.RLock()

TOKEN_TTL_DAYS = 7


def _read_map():
    try:
        if os.path.exists(MAP_PATH):
            with open(MAP_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {}


def _write_map(d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = MAP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MAP_PATH)
    except Exception:
        pass


def save_media(data: bytes, filename: str = "file.bin", staff: str = "") -> str:
    data = data or b""
    if not data:
        return ""
    tok = secrets.token_urlsafe(16)
    ext = ""
    fn = str(filename or "")
    if "." in fn:
        ext = "." + fn.rsplit(".", 1)[-1][:10]
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, tok + ext)
    with open(path, "wb") as f:
        f.write(data)
    with _lock:
        m = _read_map()
        m[tok] = {"file": os.path.basename(path), "name": fn[:120],
                  "size": len(data), "staff": staff,
                  "sha256": hashlib.sha256(data).hexdigest()[:16],
                  "at": int(time.time())}
        _write_map(m)
    return tok


def resolve(token: str):
    token = str(token or "").strip()
    if not token:
        return None
    with _lock:
        m = _read_map()
        rec = m.get(token)
    if not isinstance(rec, dict):
        return None
    # het han
    try:
        if time.time() - int(rec.get("at") or 0) > TOKEN_TTL_DAYS * 86400:
            return None
    except Exception:
        pass
    path = os.path.join(MEDIA_DIR, str(rec.get("file") or ""))
    if not os.path.isfile(path):
        return None
    return {"path": path, "name": rec.get("name") or "file", "size": rec.get("size") or 0}
