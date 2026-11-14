"""Auto-managed Cloudflare Quick Tunnel for 1-click Public sharing."""
import atexit
import os
import re
import shutil
import subprocess
import threading
import time

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
BIN_DIR = os.path.join(DATA_DIR, "bin")
CLOUDFLARED_EXE = os.path.join(BIN_DIR, "cloudflared.exe" if os.name == "nt" else "cloudflared")
DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    if os.name == "nt"
    else "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
)
_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_lock = threading.RLock()
_proc = None
_status = "stopped"  # stopped | downloading | starting | running | error
_url = ""
_error = ""
_progress_pct = 0
_gen = 0


def find_cloudflared() -> str:
    """Return path to cloudflared binary if installed locally or on PATH."""
    found = shutil.which("cloudflared")
    if found and os.path.isfile(found):
        return found
    candidates = [CLOUDFLARED_EXE]
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\cloudflared\cloudflared.exe",
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        ])
    for path in candidates:
        if path and os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
            return path
    return ""


def _set_public_url(url: str):
    try:
        from features.gateway import config as gateway_config
        patch = {"publicUrl": url}
        if url:
            patch["allowWan"] = True
        gateway_config._save_file_cfg(patch)
    except Exception:
        pass


def _download_binary(gen: int) -> str:
    global _status, _error, _progress_pct
    import requests

    os.makedirs(BIN_DIR, exist_ok=True)
    part_path = CLOUDFLARED_EXE + ".part"
    with _lock:
        if gen != _gen:
            return ""
        _status = "downloading"
        _progress_pct = 0
        _error = ""

    try:
        with requests.get(DOWNLOAD_URL, stream=True, timeout=(15, 120), allow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(part_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    with _lock:
                        if gen != _gen:
                            return ""
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        with _lock:
                            _progress_pct = min(99, int(downloaded * 100 / total))
        os.replace(part_path, CLOUDFLARED_EXE)
        if os.name != "nt":
            os.chmod(CLOUDFLARED_EXE, 0o755)
        with _lock:
            _progress_pct = 100
        return CLOUDFLARED_EXE
    except Exception as exc:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        with _lock:
            if gen == _gen:
                _status = "error"
                _error = f"Không tải được Cloudflare Tunnel: {str(exc)[:180]}"
        return ""


def _watch_process(proc: subprocess.Popen, gen: int):
    global _status, _url, _error, _proc
    last_lines = []
    try:
        if proc.stdout:
            for raw in proc.stdout:
                line = (raw or "").strip()
                if not line:
                    continue
                last_lines.append(line)
                if len(last_lines) > 15:
                    last_lines.pop(0)
                match = _URL_RE.search(line)
                if match:
                    found_url = match.group(0).rstrip("/")
                    with _lock:
                        if gen != _gen:
                            return
                        _url = found_url
                        _status = "running"
                        _error = ""
                    _set_public_url(found_url)
        rc = proc.wait()
    except Exception as exc:
        rc = -1
        last_lines.append(str(exc))

    with _lock:
        if gen != _gen:
            return
        old_url = _url
        _proc = None
        if _status != "stopped":
            _status = "error"
            tail = " | ".join(last_lines[-3:]) if last_lines else f"exit code {rc}"
            _error = f"Tunnel đã dừng ({tail[:180]})"
        _url = ""
    if old_url:
        try:
            from features.gateway import config as gateway_config
            if gateway_config.get_public_url() == old_url:
                _set_public_url("")
        except Exception:
            pass


def _worker_start(port: int, gen: int):
    global _proc, _status, _error, _url
    bin_path = find_cloudflared()
    if not bin_path:
        bin_path = _download_binary(gen)
        if not bin_path:
            return

    with _lock:
        if gen != _gen:
            return
        _status = "starting"
        _error = ""
        _url = ""

    cmd = [bin_path, "tunnel", "--url", f"http://127.0.0.1:{int(port)}", "--no-autoupdate"]
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        with _lock:
            if gen == _gen:
                _status = "error"
                _error = f"Không chạy được cloudflared: {str(exc)[:180]}"
        return

    with _lock:
        if gen != _gen:
            try:
                proc.terminate()
            except Exception:
                pass
            return
        _proc = proc

    threading.Thread(target=_watch_process, args=(proc, gen), daemon=True, name="gw-tunnel-watch").start()

    # Wait briefly in case cloudflared takes too long without producing a URL
    deadline = time.time() + 30
    while time.time() < deadline:
        with _lock:
            if gen != _gen or _status in ("running", "error", "stopped"):
                return
        time.sleep(0.3)

    with _lock:
        if gen == _gen and _status == "starting" and not _url:
            _error = "Đang chờ Cloudflare cấp link hơi lâu — kiểm tra kết nối mạng."


def start_tunnel(port: int = 5000, force: bool = False) -> dict:
    """Start Cloudflare Quick Tunnel asynchronously if not already running."""
    global _gen, _status, _error
    if force:
        stop_tunnel()
    with _lock:
        if _status == "running" and _proc and _proc.poll() is None and _url:
            return get_status()
        if _status in ("downloading", "starting"):
            return get_status()
        _gen += 1
        gen = _gen
        _status = "starting" if find_cloudflared() else "downloading"
        _error = ""

    threading.Thread(target=_worker_start, args=(port, gen), daemon=True, name="gw-tunnel-start").start()
    return get_status()


def get_effective_public_url(saved_public_url: str = "") -> str:
    """Return active tunnel URL, or custom saved URL (ignoring dead trycloudflare links)."""
    st = get_status()
    if st.get("running") and st.get("url"):
        return st["url"]
    u = str(saved_public_url or "").strip().rstrip("/")
    if u.endswith(".trycloudflare.com"):
        return ""
    return u


def stop_tunnel() -> dict:
    """Stop active tunnel process and clear temporary trycloudflare publicUrl."""
    global _gen, _proc, _status, _url, _error, _progress_pct
    with _lock:
        _gen += 1
        proc = _proc
        old_url = _url
        _proc = None
        _status = "stopped"
        _url = ""
        _error = ""
        _progress_pct = 0

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if old_url:
        try:
            from features.gateway import config as gateway_config
            if gateway_config.get_public_url() == old_url:
                _set_public_url("")
        except Exception:
            pass
    return get_status()


def get_status() -> dict:
    with _lock:
        alive = bool(_proc and _proc.poll() is None)
        if _status == "running" and not alive:
            running = False
        else:
            running = bool(_status == "running" and alive and _url)
        return {
            "running": running,
            "busy": _status in ("downloading", "starting"),
            "status": _status,
            "url": _url,
            "error": _error,
            "progressPct": _progress_pct,
            "hasBinary": bool(find_cloudflared()),
        }


atexit.register(stop_tunnel)

