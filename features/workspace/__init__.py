"""Workspace kieu zDesk: moi acc 1 khong gian doc lap (Web / PC / Mobile).

- Web: dung san Chrome profile rieng (profiles/<accountId>) — da co.
- PC: mo Zalo PC voi --user-data-dir rieng (Electron ho tro) de chay song song.
- Mobile: placeholder huong dan (can gia lap), khong tu mo.
- Backup/restore: zip 1-click tung workspace.
"""
import os
import sys
import json
import time
import shutil
import subprocess
import threading
import zipfile
import tempfile

_running_pc = {}  # account_id -> Popen
_lock = threading.RLock()


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
ZALOPC_ROOT = os.path.join(BASE_DIR, "profiles-zalopc")
os.makedirs(ZALOPC_ROOT, exist_ok=True)


def find_zalo_exe():
    """Tim file chay Zalo PC. Tra ve path hoac ''."""
    cands = []
    # Bien moi truong + duong dan pho bien
    for raw in [
        r"%ProgramFiles%\Zalo\Zalo.exe",
        r"%ProgramFiles(x86)%\Zalo\Zalo.exe",
        r"%LOCALAPPDATA%\Zalo\Zalo.exe",
        r"%LOCALAPPDATA%\Programs\Zalo\Zalo.exe",
    ]:
        try:
            cands.append(os.path.expandvars(raw))
        except Exception:
            pass
    # Registry uninstall
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            try:
                                with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                    name, _ = winreg.QueryValueEx(sk, "DisplayName")
                                    if "zalo" in str(name).lower():
                                        try:
                                            loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                            if loc:
                                                cands.append(os.path.join(str(loc), "Zalo.exe"))
                                        except Exception:
                                            pass
                            except Exception:
                                continue
                except Exception:
                    continue
    except Exception:
        pass
    for p in cands:
        try:
            if p and os.path.isfile(p):
                return p
        except Exception:
            continue
    return ""


def zalopc_data_dir(account_id: str) -> str:
    d = os.path.join(ZALOPC_ROOT, str(account_id or "").strip() or "default")
    os.makedirs(d, exist_ok=True)
    return d


def pc_status(account_id: str) -> dict:
    with _lock:
        proc = _running_pc.get(account_id)
        running = bool(proc is not None and proc.poll() is None)
        if proc is not None and proc.poll() is not None:
            _running_pc.pop(account_id, None)
    return {
        "running": running,
        "pid": proc.pid if running else None,
        "dataDir": zalopc_data_dir(account_id),
        "exe": find_zalo_exe(),
    }


def pc_launch(account_id: str) -> dict:
    exe = find_zalo_exe()
    if not exe:
        raise FileNotFoundError("Chua tim thay Zalo PC (Zalo.exe). Cai Zalo PC truoc roi thu lai.")
    with _lock:
        proc = _running_pc.get(account_id)
        if proc is not None and proc.poll() is None:
            return {"running": True, "pid": proc.pid}
    data_dir = zalopc_data_dir(account_id)
    # Electron/Chromium chap nhan --user-data-dir de tach khong gian
    args = [exe, f"--user-data-dir={os.path.abspath(data_dir)}"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)
    with _lock:
        _running_pc[account_id] = proc
    return {"running": True, "pid": proc.pid, "dataDir": data_dir, "exe": exe}


def pc_close(account_id: str) -> dict:
    with _lock:
        proc = _running_pc.pop(account_id, None)
    if proc is None:
        return {"running": False}
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass
    return {"running": False}


def workspace_backup(account_id: str) -> str:
    """Zip 1-click: profiles/<id> (Web) + profiles-zalopc/<id> (PC) + thong tin acc. Tra ve path file zip."""
    from features.accounts.account_manager import get_account
    acc = get_account(account_id) or {"accountId": account_id}
    tmp = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        # Web profile
        web_dir = os.path.join(PROFILES_DIR, account_id)
        if os.path.isdir(web_dir):
            for root, _dirs, files in os.walk(web_dir):
                for fn in files:
                    # bo file lock de nhe + tranh khoa
                    if fn.lower() in ("singletonlock", "singletonsocket", "lockfile", "singletoncookie"):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        z.write(fp, os.path.join("profiles", account_id, os.path.relpath(fp, web_dir)))
                    except Exception:
                        continue
        # PC data
        pc_dir = zalopc_data_dir(account_id)
        if os.path.isdir(pc_dir):
            for root, _dirs, files in os.walk(pc_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        z.write(fp, os.path.join("profiles-zalopc", account_id, os.path.relpath(fp, pc_dir)))
                    except Exception:
                        continue
        z.writestr("account.json", json.dumps(acc, ensure_ascii=False, indent=2))
        z.writestr("Huong-dan.txt", "Giai nen de khoi phuc workspace.\nDung chuc nang Khoi phuc trong /workspace de nap lai.\n")
    return tmp


def workspace_restore(zip_path: str) -> dict:
    """Nap lai workspace tu file zip backup. Tra ve accountId."""
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        # an toan: chan path traversal
        for n in names:
            if ".." in n.replace("\\", "/").split("/"):
                raise ValueError("File zip khong hop le.")
        tmpdir = tempfile.mkdtemp(prefix="wsrestore_")
        z.extractall(tmpdir)
        acc_id = ""
        try:
            with open(os.path.join(tmpdir, "account.json"), "r", encoding="utf-8") as f:
                acc = json.load(f)
                acc_id = str(acc.get("accountId") or "").strip()
        except Exception:
            acc = None
        # copy profiles
        for sub, dest_root in (("profiles", PROFILES_DIR), ("profiles-zalopc", ZALOPC_ROOT)):
            src_root = os.path.join(tmpdir, sub)
            if os.path.isdir(src_root):
                for item in os.listdir(src_root):
                    s = os.path.join(src_root, item)
                    d = os.path.join(dest_root, item)
                    try:
                        if os.path.isdir(s):
                            shutil.rmtree(d, ignore_errors=True)
                            shutil.copytree(s, d)
                            if not acc_id:
                                acc_id = item
                        else:
                            os.makedirs(dest_root, exist_ok=True)
                            shutil.copy2(s, d)
                    except Exception:
                        continue
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"accountId": acc_id}
