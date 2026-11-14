"""Gateway routes store: cau hinh forward + con tro persist (P4)."""
import json
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "gateway_forwarder.json")
CURSOR_PATH = os.path.join(DATA_DIR, "gateway_cursor.json")

_lock = threading.RLock()

DEFAULTS = {
    "enabled": False,
    "accountId": "",
    "routes": [],  # [{id, dest_group_id, source_group_ids: []}]
    "intervalSec": 15,
}


def load_config() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k in DEFAULTS:
                        if k in d:
                            cfg[k] = d[k]
        except Exception:
            pass
        if not isinstance(cfg.get("routes"), list):
            cfg["routes"] = []
        clean = []
        for i, r in enumerate(cfg["routes"]):
            if not isinstance(r, dict):
                continue
            dest = str(r.get("dest_group_id") or "").strip()
            srcs = sorted({str(x or "").strip() for x in (r.get("source_group_ids") or []) if str(x or "").strip() and str(x or "").strip() != dest})
            clean.append({"id": str(r.get("id") or f"r{i+1}"), "dest_group_id": dest, "source_group_ids": srcs})
        cfg["routes"] = clean
        try:
            cfg["intervalSec"] = max(5, int(cfg.get("intervalSec") or 15))
        except Exception:
            cfg["intervalSec"] = 15
        return cfg


def save_config(patch: dict) -> dict:
    with _lock:
        cfg = load_config()
        for k in DEFAULTS:
            if k in (patch or {}):
                cfg[k] = patch[k]
        if "routes" in (patch or {}):
            raw = patch.get("routes") or []
            clean = []
            for i, r in enumerate(raw if isinstance(raw, list) else []):
                if not isinstance(r, dict):
                    continue
                dest = str(r.get("dest_group_id") or "").strip()
                srcs = sorted({str(x or "").strip() for x in (r.get("source_group_ids") or []) if str(x or "").strip() and str(x or "").strip() != dest})
                clean.append({"id": str(r.get("id") or f"r{i+1}"), "dest_group_id": dest, "source_group_ids": srcs})
            cfg["routes"] = clean
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return cfg


def load_cursor() -> dict:
    try:
        if os.path.exists(CURSOR_PATH):
            with open(CURSOR_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {}


def save_cursor(cur: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CURSOR_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur or {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CURSOR_PATH)
    except Exception:
        pass
