import sys
import os

from flask import Flask, render_template, request, jsonify, Response, redirect, url_for
import json
import time
import queue
import threading
import requests
import re
import uuid
import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta

# ─── Get App Root (PyInstaller compatible) ─────────────────────────────────────
def get_app_root():
    """Get application root directory (works both in source and PyInstaller frozen mode)."""
    if getattr(sys, "frozen", False):
        # PyInstaller --onefile: data/profiles tạo cùng folder exe gốc, không trong temp folder
        # Nên dùng sys.executable directory (nơi exe được chạy)
        return os.path.dirname(sys.executable)
    else:
        # Running as Python script
        return os.path.dirname(os.path.abspath(__file__))

def get_base_path():
    """Get the resource directory used by Flask.

    In a PyInstaller one-file build the bundled templates/static files are
    extracted into ``sys._MEIPASS``.  Before Flask starts, overlay any external
    ``templates`` and ``static`` folders placed next to Nexus.exe onto that
    extracted directory.  This keeps the EXE self-contained by default while
    allowing a small UI patch ZIP to take effect without rebuilding the whole
    application again.
    """
    if getattr(sys, "frozen", False):
        bundled_root = sys._MEIPASS
        external_root = os.path.dirname(sys.executable)

        for resource_name in ("templates", "static"):
            external_dir = os.path.join(external_root, resource_name)
            bundled_dir = os.path.join(bundled_root, resource_name)
            if not os.path.isdir(external_dir):
                continue
            try:
                os.makedirs(bundled_dir, exist_ok=True)
                shutil.copytree(external_dir, bundled_dir, dirs_exist_ok=True)
                print(f"[UI] Loaded external {resource_name} override from {external_dir}")
            except Exception as error:
                # A failed optional override must never stop Nexus from opening.
                print(f"[UI] Could not load external {resource_name} override: {error}")

        return bundled_root

    # Running as Python source.
    return os.path.dirname(os.path.abspath(__file__))

app_root = get_app_root()

# ─── Single activity log file ─────────────────────────────────────────────────
# Mỗi lần ứng dụng khởi động, file này được xóa trắng và toàn bộ stdout/stderr
# cùng log tác vụ SSE được ghi vào đúng một file duy nhất.
ACTIVITY_LOG_FILE = os.path.join(app_root, "Nexus.log")
_ACTIVITY_LOG_LOCK = threading.RLock()
_ACTIVITY_LOG_INITIALIZED = False
# File handle giữ mở thay vì mở/đóng cho mỗi dòng log. Trước đây mỗi dòng
# (bao gồm cả access log của werkzeug) đều tạo một lần open+close file -> rất
# nặng khi app phục vụ hàng trăm request/phút.
_ACTIVITY_LOG_FILE_HANDLE = None


def _get_activity_log_handle():
    """Lấy (hoặc mở) handle ghi log dùng chung."""
    global _ACTIVITY_LOG_FILE_HANDLE
    if _ACTIVITY_LOG_FILE_HANDLE is not None:
        return _ACTIVITY_LOG_FILE_HANDLE
    try:
        os.makedirs(os.path.dirname(ACTIVITY_LOG_FILE), exist_ok=True)
        _ACTIVITY_LOG_FILE_HANDLE = open(
            ACTIVITY_LOG_FILE, "a", encoding="utf-8", newline="\n"
        )
    except Exception:
        _ACTIVITY_LOG_FILE_HANDLE = None
    return _ACTIVITY_LOG_FILE_HANDLE


def _close_activity_log_handle():
    global _ACTIVITY_LOG_FILE_HANDLE
    if _ACTIVITY_LOG_FILE_HANDLE is not None:
        try:
            _ACTIVITY_LOG_FILE_HANDLE.flush()
            _ACTIVITY_LOG_FILE_HANDLE.close()
        except Exception:
            pass
        _ACTIVITY_LOG_FILE_HANDLE = None


def _append_activity_log(message, level="INFO"):
    """Append one normalized entry to the single activity log file."""
    text = str(message or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized_level = str(level or "INFO").upper().replace("TASK:", "TASK-")
    lines = text.split("\n")
    try:
        with _ACTIVITY_LOG_LOCK:
            log_file = _get_activity_log_handle()
            if log_file is None:
                return
            for line in lines:
                if line:
                    log_file.write(f"[{timestamp}] [{normalized_level}] {line}\n")
            log_file.flush()
    except Exception:
        # Logging must never stop the application from starting.
        pass


class _ActivityLogWriter:
    """Line-buffered stdout/stderr writer backed by Nexus.log."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, level="INFO", original_stream=None):
        self.level = level
        self.original_stream = original_stream
        self._buffer = ""

    def write(self, data):
        if data is None:
            return 0
        if isinstance(data, bytes):
            text = data.decode(self.encoding, errors=self.errors)
        else:
            text = str(data)
        if not text:
            return 0

        if self.original_stream:
            try:
                self.original_stream.write(text)
                self.original_stream.flush()
            except Exception:
                pass

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                _append_activity_log(line, self.level)
        return len(text)

    def flush(self):
        if self._buffer.strip():
            _append_activity_log(self._buffer, self.level)
        self._buffer = ""
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writable(self):
        return True


def _initialize_activity_logging():
    """Clear the previous run and redirect all process output to one log file."""
    global _ACTIVITY_LOG_INITIALIZED
    if _ACTIVITY_LOG_INITIALIZED:
        return
    try:
        os.makedirs(os.path.dirname(ACTIVITY_LOG_FILE), exist_ok=True)
        with _ACTIVITY_LOG_LOCK:
            with open(ACTIVITY_LOG_FILE, "w", encoding="utf-8", newline="\n"):
                pass
    except Exception:
        pass
    _close_activity_log_handle()

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _ActivityLogWriter("INFO", original_stdout)
    sys.stderr = _ActivityLogWriter("ERROR", original_stderr)
    _ACTIVITY_LOG_INITIALIZED = True
    _append_activity_log("Nexus started. Previous activity log was cleared.", "SYSTEM")


if __name__ == "__main__":
    # Đảm bảo worker import `from app import ...` dùng chung module `__main__` (cùng _inbox_sse_clients / _sse_clients).
    sys.modules.setdefault("app", sys.modules["__main__"])
    _initialize_activity_logging()

base_path = get_base_path()
template_path = os.path.join(base_path, 'templates')
static_path = os.path.join(base_path, 'static')

app = Flask(__name__,
            template_folder=template_path,
            static_folder=static_path)
app.secret_key = "zalo-tool-secret-key"
app.config['JSON_AS_ASCII'] = False  # Allow non-ASCII characters in JSON
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
# UI patches must be visible immediately after overwrite/restart.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ─── Giảm log access ồn ào của werkzeug ──────────────────────────────────────
# Mặc định werkzeug in MỘT dòng cho MỌI request ra stderr. Ứng dụng này bị
# frontend poll liên tục (mỗi 10s gọi /api/group-copy/jobs, SSE, static asset)
# nên Nexus.log bị ngập dòng "[ERROR] 127.0.0.1 - - [GET /...]" làm file phình
# to nhanh, đọc log chậm và tốn I/O. Chỉ giữ lại các request lỗi (>=400) và
# các request chậm (>5s) để vẫn còn dấu vết khi cần debug.
class _QuietAccessLogFilter:
    """Lọc access log của werkzeug: bỏ request 2xx/3xx thường."""

    def filter(self, record):
        try:
            status = getattr(record, "status", None)
            if status is None:
                response = getattr(record, "response", None)
                status = getattr(response, "status_code", None)
            status = int(status) if status is not None else 0
        except Exception:
            return True
        if status >= 500:
            return True
        if status >= 400:
            return True
        # Giữ lại request chậm để phát hiện nghẽn hiệu năng.
        try:
            if float(getattr(record, "request_duration", 0) or 0) > 5.0:
                return True
        except Exception:
            pass
        return False


try:
    from werkzeug.logging import logging as _werkzeug_logging

    _werkzeug_logging.getLogger("werkzeug").setLevel(_werkzeug_logging.WARNING)
    _werkzeug_logging.getLogger("werkzeug").addFilter(_QuietAccessLogFilter())
except Exception:
    pass

# ─── Setup sys.path for source/packaged modules ───────────────────────────────
if getattr(sys, "frozen", False):
    # PyInstaller: packages are extracted in sys._MEIPASS
    module_root = sys._MEIPASS
    auth_path = os.path.join(sys._MEIPASS, "authencation")
else:
    # Source mode: app root contains core/, features/, authencation/
    module_root = os.path.dirname(__file__)
    auth_path = os.path.join(module_root, "authencation")

if module_root not in sys.path:
    sys.path.insert(0, module_root)
if auth_path not in sys.path:
    sys.path.insert(0, auth_path)

from features.members.group_member_service import fetch_group_members_by_input, format_group_info, normalize_group_input, resolve_group_input_to_group_id
from features.profiles.profile_service import fetch_profiles_with_single_fallback, build_member_rows_from_uids, fetch_friend_relations, fetch_friend_genders
from features.messaging.send_sms import send_sms
from features.messaging.add_friend import send_friend_request
from features.groups.add_group import create_group as _create_group
from features.groups.invite_group import invite_members_to_group as _invite_members_to_group
from features.groups.group_join_leave import join_group_by_link as _join_group_by_link, leave_group as _leave_group
from features.groups.group_manager import start_fetch_group_details_parallel, ensure_group_link_for_account
from features.profiles.get_single_profile import get_single_profile, ProfileRateLimitError
from features.accounts.account_manager import (
    load_accounts,
    create_account,
    open_account,
    delete_account,
    update_account,
    get_account,
    _ensure_dirs,
)
from features.profiles.fetch_userinfo import (
    get_userinfo_quick,
    get_userinfo_batch,
    get_userinfo_v2,
)
from features.schedules.schedule_manager import (
    load_schedules as load_scheds,
    get_schedule,
    create_schedule,
    update_schedule,
    delete_schedule,
    cancel_schedule,
    get_all_schedules,
)
from features.schedules.schedule_worker import start_schedule_worker
from features.groups.group_copy_manager import (
    create_job as create_group_copy_job,
    list_jobs as list_group_copy_jobs,
    get_job as get_group_copy_job,
    cancel_job as cancel_group_copy_job,
    resume_job as resume_group_copy_job,
    request_verification as request_group_copy_verification,
    delete_job as delete_group_copy_job,
    mutate_job as save_group_copy_job_mutation,
)
from features.groups.group_copy_worker import start_group_copy_worker
from features.tasks.task_manager import (
    create_task,
    get_task,
    list_tasks,
    run_task_in_background,
    set_sse_broadcast_func,
)
from features.messages.message_manager import (
    list_conversations as list_message_conversations,
    get_conversation as get_message_conversation,
    create_conversation as create_message_conversation,
    add_message as add_conversation_message,
    update_conversation as update_message_conversation,
    mark_read as mark_message_read,
    get_stats as get_message_stats,
    get_settings as get_message_settings,
    update_settings as update_message_settings,
    log_check_attempt as log_message_check_attempt,
)
from features.messages.unread_manager import (
    list_unread_messages,
    dismiss_unread_message,
    dismiss_group_messages,
    sync_unread_messages,
    get_group_latest_messages,
    start_unread_worker,
)
from core.zalo.zalo_config import get_zpw_ver
from features.chatbot import store as chatbot_store
from features.chatbot import worker as chatbot_worker
from features.telegram import store as telegram_store
from features.telegram import worker as telegram_worker
from features.telegram import telegram_client
from features.gateway import config as gateway_config
from features.gateway import auth as gateway_auth
from features.gateway import send_queue as gateway_queue
from features.gateway import tunnel as gateway_tunnel


# ─── Friend request planning storage ─────────────────────────────────────────
FRIEND_PLANS_FILE = os.path.join(app_root, "data", "friend_invite_plans.json")
INVITE_GROUP_PLANS_FILE = os.path.join(app_root, "data", "group_invite_plans.json")
USER_POLICY_FILE = os.path.join(app_root, "data", "user_policy_acceptance.json")
USER_POLICY_VERSION = "2026-07-28-nexus-masterise-v8-compact-session"
VERSION_FILE = os.path.join(app_root, "VERSION")
APP_VERSION = "1.1.7"
UPDATE_REPO = "AnhTuan2003ml/mkt_zalo"
UPDATE_ASSET_NAME = "Nexus.zip"
UPDATE_HASH_ASSET_NAME = UPDATE_ASSET_NAME + ".sha256"
UPDATE_EXE_NAME = "Nexus.exe"


def _read_app_version():
    try:
        if os.path.isfile(VERSION_FILE):
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                version = f.read().strip()
                if version:
                    return version.lstrip("v")
    except Exception as e:
        print("[updater] read VERSION error=", e)
    return APP_VERSION


def _parse_version_parts(version):
    return [int(x) for x in re.findall(r"\d+", str(version or ""))]


def _is_newer_version(latest, current):
    latest_parts = _parse_version_parts(latest)
    current_parts = _parse_version_parts(current)
    size = max(len(latest_parts), len(current_parts), 1)
    latest_parts += [0] * (size - len(latest_parts))
    current_parts += [0] * (size - len(current_parts))
    return latest_parts > current_parts


def _github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Nexus-Updater",
    }


# Gọi thẳng không qua system proxy: các tool bắt gói (đặt proxy 127.0.0.1:xxxx
# toàn hệ thống) sẽ làm SSL verification thất bại khi kiểm tra cập nhật.
_UPDATE_NO_PROXY = {"http": None, "https": None}


def _fetch_latest_release():
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    res = requests.get(url, headers=_github_headers(), timeout=20, proxies=_UPDATE_NO_PROXY)
    res.raise_for_status()
    release = res.json()
    tag_name = str(release.get("tag_name") or "").strip()
    latest_version = tag_name.lstrip("v")
    assets = release.get("assets") or []
    asset = next((a for a in assets if str(a.get("name") or "").lower() == UPDATE_ASSET_NAME.lower()), None)
    hash_asset = next((a for a in assets if str(a.get("name") or "").lower() == UPDATE_HASH_ASSET_NAME.lower()), None)
    if not latest_version:
        raise ValueError("Release mới nhất không có tag phiên bản.")
    if not asset or not asset.get("browser_download_url"):
        raise ValueError(f"Không tìm thấy asset {UPDATE_ASSET_NAME} trong release {tag_name}.")
    return {
        "version": latest_version,
        "downloadUrl": asset.get("browser_download_url"),
        "size": asset.get("size") or 0,
        "digest": asset.get("digest") or "",
        "hashUrl": hash_asset.get("browser_download_url") if hash_asset else "",
    }


def _download_update_zip(download_url, target_zip):
    part_path = target_zip + ".part"
    if os.path.exists(part_path):
        os.remove(part_path)
    with requests.get(download_url, headers=_github_headers(), timeout=60, stream=True, proxies=_UPDATE_NO_PROXY) as res:
        res.raise_for_status()
        with open(part_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    os.replace(part_path, target_zip)


def _download_text(url):
    if not url:
        return ""
    res = requests.get(url, headers=_github_headers(), timeout=20, proxies=_UPDATE_NO_PROXY)
    res.raise_for_status()
    return res.text


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_sha256(value):
    if not value:
        return ""
    match = re.search(r"\b[a-fA-F0-9]{64}\b", str(value))
    return match.group(0).lower() if match else ""


def _verify_update_hash(update_zip, release):
    expected = _extract_sha256(release.get("digest"))
    if not expected and release.get("hashUrl"):
        expected = _extract_sha256(_download_text(release["hashUrl"]))
    if not expected:
        return
    actual = _sha256_file(update_zip)
    if actual.lower() != expected:
        raise ValueError("SHA-256 của gói cập nhật không khớp release.")


def _get_update_dir():
    return os.path.join(app_root, "update")


def _validate_update_zip(update_zip, expected_size=0):
    if not os.path.isfile(update_zip) or os.path.getsize(update_zip) < 1024 * 1024:
        raise ValueError("Gói cập nhật không hợp lệ hoặc quá nhỏ.")
    if expected_size and os.path.getsize(update_zip) != int(expected_size):
        raise ValueError("Kích thước gói cập nhật không khớp release.")
    with zipfile.ZipFile(update_zip, "r") as zf:
        exe_members = []
        for info in zf.infolist():
            normalized = info.filename.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise ValueError("Gói cập nhật chứa đường dẫn không an toàn.")
            if os.path.basename(normalized).lower() == UPDATE_EXE_NAME.lower():
                exe_members.append(info)
        if not exe_members:
            raise ValueError("Nexus.zip không chứa Nexus.exe.")

def _install_zip_update(update_zip, version):
    install_dir = app_root
    if getattr(sys, "frozen", False):
        install_dir = os.path.dirname(sys.executable)

    os.makedirs(install_dir, exist_ok=True)

    update_dir = os.path.dirname(update_zip)

    script_path = os.path.join(tempfile.gettempdir(), "_nexus_apply_update.bat")
    wait_pid = os.getpid() if getattr(sys, "frozen", False) else 0
    script = r'''@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "PID_TO_WAIT=%~1"
set "ZIP_PATH=%~2"
set "INSTALL_DIR=%~3"
set "UPDATE_DIR=%~4"
set "VERSION_VALUE=%~5"
set "STAGE_DIR=%UPDATE_DIR%\stage"
set "RUN_EXE=%INSTALL_DIR%\Nexus.exe"
set "NEW_EXE=%INSTALL_DIR%\Nexus.exe.new"

if not "%PID_TO_WAIT%"=="0" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { Wait-Process -Id %PID_TO_WAIT% -Timeout 45 -ErrorAction SilentlyContinue } catch {}" >nul 2>&1
)

timeout /t 1 /nobreak >nul

if not exist "%ZIP_PATH%" goto :fail

if exist "%STAGE_DIR%" rmdir /s /q "%STAGE_DIR%"
mkdir "%STAGE_DIR%" || goto :fail

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath $env:ZIP_PATH -DestinationPath $env:STAGE_DIR -Force" >nul 2>&1
if errorlevel 1 goto :fail

set "EXE_PATH=%STAGE_DIR%\Nexus.exe"
if not exist "%EXE_PATH%" (
    for /r "%STAGE_DIR%" %%F in (Nexus.exe) do (
        set "EXE_PATH=%%~fF"
        goto :found_exe
    )
)
:found_exe
if not exist "%EXE_PATH%" goto :fail

if exist "%NEW_EXE%" del /f /q "%NEW_EXE%"
copy /y "%EXE_PATH%" "%NEW_EXE%" >nul 2>&1 || goto :fail

move /y "%NEW_EXE%" "%RUN_EXE%" >nul 2>&1 || (
    if exist "%NEW_EXE%" del /f /q "%NEW_EXE%"
    goto :fail
)

> "%INSTALL_DIR%\VERSION" echo %VERSION_VALUE%
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" /d "%INSTALL_DIR%" "%RUN_EXE%"

timeout /t 1 /nobreak >nul
if exist "%UPDATE_DIR%" rmdir /s /q "%UPDATE_DIR%"
del /f /q "%~f0"
exit /b 0

:fail
if exist "%NEW_EXE%" del /f /q "%NEW_EXE%"
if exist "%RUN_EXE%" (
    set "PYINSTALLER_RESET_ENVIRONMENT=1"
    start "" /d "%INSTALL_DIR%" "%RUN_EXE%"
)
del /f /q "%~f0"
exit /b 1
'''
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    command = [
        "cmd.exe",
        "/d",
        "/c",
        "call",
        script_path,
        str(wait_pid),
        update_zip,
        install_dir,
        update_dir,
        str(version).lstrip("v"),
    ]
    if not getattr(sys, "frozen", False):
        subprocess.check_call(command, cwd=install_dir)
        return {
            "state": "done",
            "message": "Đã giải nén gói cập nhật và chạy lại Nexus.exe.",
        }

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(command, cwd=install_dir, creationflags=creationflags, close_fds=True)

    def _exit_for_update():
        os._exit(0)

    threading.Timer(1.2, _exit_for_update).start()
    return {
        "state": "restart",
        "message": "Đã tải bản cập nhật. Ứng dụng sẽ tự giải nén, xóa gói tải về và chạy lại Nexus.exe.",
    }


def _friend_now_ms():
    return int(time.time() * 1000)


def _ensure_friend_plan_store():
    os.makedirs(os.path.dirname(FRIEND_PLANS_FILE), exist_ok=True)


def _load_friend_plans():
    _ensure_friend_plan_store()
    if not os.path.isfile(FRIEND_PLANS_FILE):
        return []
    try:
        with open(FRIEND_PLANS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        plans = data.get("plans", data if isinstance(data, list) else [])
        return plans if isinstance(plans, list) else []
    except Exception as e:
        print("[_load_friend_plans] error=", e)
        return []


def _save_friend_plans(plans):
    _ensure_friend_plan_store()
    with open(FRIEND_PLANS_FILE, "w", encoding="utf-8") as f:
        json.dump({"plans": plans}, f, ensure_ascii=False, indent=2)


def _ensure_group_invite_plan_store():
    os.makedirs(os.path.dirname(INVITE_GROUP_PLANS_FILE), exist_ok=True)


def _load_group_invite_plans():
    _ensure_group_invite_plan_store()
    if not os.path.isfile(INVITE_GROUP_PLANS_FILE):
        return []
    try:
        with open(INVITE_GROUP_PLANS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        plans = data.get("plans", data if isinstance(data, list) else [])
        return plans if isinstance(plans, list) else []
    except Exception as e:
        print("[_load_group_invite_plans] error=", e)
        return []


def _save_group_invite_plans(plans):
    _ensure_group_invite_plan_store()
    with open(INVITE_GROUP_PLANS_FILE, "w", encoding="utf-8") as f:
        json.dump({"plans": plans}, f, ensure_ascii=False, indent=2)


def _normalize_plan_group(item):
    if isinstance(item, str):
        gid = item.strip()
        return {"groupId": gid, "name": gid, "avatar": "", "memberCount": 0} if gid else None
    if not isinstance(item, dict):
        return None
    gid = str(
        item.get("groupId")
        or item.get("grid")
        or item.get("gridId")
        or item.get("id")
        or ""
    ).strip()
    if not gid:
        return None
    return {
        "groupId": gid,
        "name": str(item.get("name") or item.get("groupName") or gid).strip(),
        "avatar": str(item.get("avatar") or item.get("fullAvt") or item.get("avt") or "").strip(),
        "memberCount": item.get("memberCount", item.get("totalMember", item.get("total", 0))),
    }


def _find_account_name(account_id: str) -> str:
    for acc in load_accounts():
        aid = str(acc.get("accountId") or acc.get("id") or acc.get("account_id") or "").strip()
        if aid == str(account_id).strip():
            return acc.get("name") or aid
    return str(account_id).strip()


def _normalize_plan_member(item):
    if isinstance(item, str):
        uid = item.strip()
        return {"userId": uid, "name": uid, "avatar": ""} if uid else None
    if not isinstance(item, dict):
        return None
    uid = str(
        item.get("userId")
        or item.get("uid")
        or item.get("id")
        or item.get("toId")
        or ""
    ).strip()
    if not uid:
        return None
    return {
        "userId": uid,
        "name": str(item.get("zaloName") or item.get("name") or item.get("displayName") or uid).strip(),
        "avatar": str(item.get("avatar") or item.get("avt") or item.get("avatarUrl") or "").strip(),
        "phone": str(item.get("phone") or "").strip(),
        "isFriend": item.get("isFr", item.get("isFriend", "")),
    }


def _build_friend_plan_batches(members, daily_limit: int, start_date: str):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except Exception:
        start = datetime.now().date()
        start_date = start.isoformat()

    daily_limit = max(1, min(int(daily_limit or 1), 500))
    batches = []
    for index in range(0, len(members), daily_limit):
        day_no = index // daily_limit
        date = (start + timedelta(days=day_no)).isoformat()
        chunk = members[index:index + daily_limit]
        batches.append({
            "day": day_no + 1,
            "date": date,
            "status": "pending_manual",
            "count": len(chunk),
            "members": chunk,
        })
    return batches

# ─── Friend/group invite plan progress helpers ───────────────────────────────
_PLAN_PENDING_STATUSES = {"", "pending", "pending_manual", "planned", "waiting"}
_PLAN_RUNNING_STATUSES = {"running", "processing", "in_progress"}
_PLAN_DONE_STATUSES = {"done", "completed", "success", "sent", "invited", "friend_sent"}
_PLAN_FAILED_STATUSES = {"failed", "error"}
_PLAN_SKIPPED_STATUSES = {"skipped", "cancelled", "canceled"}
_PLAN_TERMINAL_STATUSES = _PLAN_DONE_STATUSES | _PLAN_FAILED_STATUSES | _PLAN_SKIPPED_STATUSES
_ALLOWED_BATCH_STATUS = {"pending_manual", "running", "done", "failed", "skipped"}


def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _normalize_plan_batch_status(status):
    status = str(status or "pending_manual").strip().lower()
    if status in ("complete", "completed", "success", "sent", "invited", "friend_sent"):
        return "done"
    if status in ("error",):
        return "failed"
    if status in ("cancelled", "canceled"):
        return "skipped"
    if status in ("processing", "in_progress"):
        return "running"
    if status not in _ALLOWED_BATCH_STATUS:
        return "pending_manual"
    return status


def _attach_action_plan_progress(plan: dict) -> dict:
    """Gắn progress tổng hợp cho plan. Không gọi API Zalo, chỉ đọc trạng thái batch đã lưu."""
    if not isinstance(plan, dict):
        return plan

    batches = plan.get("batches") or []
    if not isinstance(batches, list):
        batches = []
        plan["batches"] = batches

    total_members = _to_int(plan.get("totalMembers"), 0)
    if total_members <= 0:
        total_members = sum(_to_int(b.get("count"), len(b.get("members") or [])) for b in batches if isinstance(b, dict))

    total_batches = len(batches)
    done_batches = failed_batches = skipped_batches = running_batches = pending_batches = 0
    done_members = failed_members = skipped_members = 0

    for b in batches:
        if not isinstance(b, dict):
            continue
        status = _normalize_plan_batch_status(b.get("status"))
        b["status"] = status
        count = _to_int(b.get("count"), len(b.get("members") or []))

        if status == "done":
            done_batches += 1
            done_members += _to_int(b.get("successCount"), count)
            failed_members += _to_int(b.get("failedCount"), 0)
        elif status == "failed":
            failed_batches += 1
            failed_members += _to_int(b.get("failedCount"), count)
            done_members += _to_int(b.get("successCount"), 0)
        elif status == "skipped":
            skipped_batches += 1
            skipped_members += count
        elif status == "running":
            running_batches += 1
            done_members += _to_int(b.get("successCount"), 0)
            failed_members += _to_int(b.get("failedCount"), 0)
        else:
            pending_batches += 1
            done_members += _to_int(b.get("successCount"), 0)
            failed_members += _to_int(b.get("failedCount"), 0)

    processed_members = max(0, min(total_members, done_members + failed_members + skipped_members))
    progress_percent = round((processed_members * 100 / total_members), 1) if total_members else 0

    if total_batches and done_batches == total_batches:
        display_status = "completed"
    elif total_batches and (done_batches + failed_batches + skipped_batches) == total_batches:
        display_status = "partial" if done_batches else ("failed" if failed_batches else "cancelled")
    elif running_batches:
        display_status = "running"
    else:
        display_status = plan.get("status") or "planned"

    plan["progress"] = {
        "totalMembers": total_members,
        "processedMembers": processed_members,
        "doneMembers": done_members,
        "failedMembers": failed_members,
        "skippedMembers": skipped_members,
        "pendingMembers": max(0, total_members - processed_members),
        "totalBatches": total_batches,
        "doneBatches": done_batches,
        "failedBatches": failed_batches,
        "skippedBatches": skipped_batches,
        "runningBatches": running_batches,
        "pendingBatches": pending_batches,
        "percent": progress_percent,
        "displayStatus": display_status,
    }
    return plan


def _attach_action_plans_progress(plans):
    return [_attach_action_plan_progress(p) for p in (plans or []) if isinstance(p, dict)]


def _load_action_plan_store(plan_type: str):
    if plan_type == "friend":
        return _load_friend_plans()
    if plan_type == "group_invite":
        return _load_group_invite_plans()
    raise ValueError("Loại kế hoạch không hợp lệ")


def _save_action_plan_store(plan_type: str, plans):
    if plan_type == "friend":
        return _save_friend_plans(plans)
    if plan_type == "group_invite":
        return _save_group_invite_plans(plans)
    raise ValueError("Loại kế hoạch không hợp lệ")


def _update_action_plan_batch_status(plan_type: str, plan_id: str, batch_day, payload: dict):
    plans = _load_action_plan_store(plan_type)
    plan = None
    for item in plans:
        if str(item.get("id") or "") == str(plan_id):
            plan = item
            break
    if not plan:
        raise ValueError("Không tìm thấy kế hoạch")

    try:
        batch_day = int(batch_day)
    except Exception:
        raise ValueError("Ngày/batch không hợp lệ")

    batches = plan.get("batches") or []
    batch = None
    for item in batches:
        if isinstance(item, dict) and _to_int(item.get("day"), -1) == batch_day:
            batch = item
            break
    if not batch:
        raise ValueError("Không tìm thấy batch")

    status = _normalize_plan_batch_status(payload.get("status"))
    if status not in _ALLOWED_BATCH_STATUS:
        raise ValueError("Trạng thái batch không hợp lệ")

    now = _friend_now_ms()
    batch["status"] = status
    batch["updatedAt"] = now

    for key in ("note", "error", "result"):
        if key in payload:
            batch[key] = payload.get(key)

    if "successCount" in payload:
        batch["successCount"] = max(0, _to_int(payload.get("successCount"), 0))
    elif status == "done" and "successCount" not in batch:
        batch["successCount"] = _to_int(batch.get("count"), len(batch.get("members") or []))

    if "failedCount" in payload:
        batch["failedCount"] = max(0, _to_int(payload.get("failedCount"), 0))
    elif status in ("done", "skipped") and "failedCount" not in batch:
        batch["failedCount"] = 0

    if status == "failed" and "failedCount" not in batch:
        batch["failedCount"] = _to_int(batch.get("count"), len(batch.get("members") or []))

    plan["updatedAt"] = now
    _attach_action_plan_progress(plan)
    progress = plan.get("progress") or {}
    plan["status"] = progress.get("displayStatus") or plan.get("status") or "planned"

    _save_action_plan_store(plan_type, plans)
    return plan, batch

# ─── Device Activation / Info (optional - for device management) ─────────────
# DEV MODE: tat hoan toan check kich hoat — mo la chay, khong can ma.
DEVICE_TRACKING_ENABLED = False
# ─── Device Activation / Info (Hệ thống kích hoạt máy & License) ─────────────
DEVICE_TRACKING_ENABLED = True

def validate_activation_code():
    return True, 9999, "Dev mode: bo check kich hoat"
try:
    from authencation.send_info_device import (
        validate_activation_code as _device_validate_activation_code,
        save_user_activation_code as _device_save_user_activation_code,
        register_device_on_startup as _real_register_startup,
        register_device_with_activation as _real_register_activation,
        get_activation_duration_options as _real_get_duration_options,
    )
    def validate_activation_code():
        return _device_validate_activation_code()

def save_user_activation_code(code_text):
    return True, "Dev mode: bo check kich hoat"
    def save_user_activation_code(code_text):
        return _device_save_user_activation_code(code_text)

    def register_device_with_activation(*args, **kwargs):
        return _real_register_activation(*args, **kwargs)

    def get_activation_duration_options():
        return _real_get_duration_options()
except Exception:
    def validate_activation_code():
        return True, 9999, "License dev mode"

    def save_user_activation_code(code_text):
        return True, "Kích hoạt dev mode"

    def register_device_with_activation(*args, **kwargs):
        return True

    def get_activation_duration_options():
        return [
            {"key": "3d", "label": "3 Ngày", "days": 3, "icon": "📅"},
            {"key": "7d", "label": "1 Tuần", "days": 7, "icon": "📈"},
            {"key": "10d", "label": "10 Ngày", "days": 10, "icon": "📈"},
            {"key": "1m", "label": "1 Tháng", "days": 30, "icon": "📊"},
            {"key": "2m", "label": "2 Tháng", "days": 60, "icon": "📊"},
            {"key": "3m", "label": "3 Tháng", "days": 90, "icon": "📊"},
            {"key": "6m", "label": "6 Tháng", "days": 180, "icon": "📊"},
            {"key": "lifetime", "label": "Vĩnh viễn", "days": None, "icon": "💎", "is_permanent": True},
        ]

def register_device_on_startup(*args, **kwargs):
    return True

def register_device_with_activation(*args, **kwargs):
    return True

def check_activation_on_startup():
    return True

def send_device_log(action, details=None):
    return False

def get_activation_duration_options():
    return [
        {"key": "lifetime", "label": "Vinh vien (dev)", "icon": "💎"},
    ]

def get_license_plan():
    from features.license import license_manager
    info = license_manager.get_license_info()
    return {
        "activated": info["activated"],
        "planKey": f"tier{info['tier']}",
        "planLabel": info["tierName"],
        "tier": info["tier"],
        "tierBadge": info["tierBadge"],
        "isPermanent": info["isPermanent"],
        "daysRemaining": info["daysRemaining"],
        "maxAccounts": 0,
        "multiAccountExec": True,
        "allowedFeatures": info["allowedFeatures"],
        "defaultUrl": info["defaultUrl"],
    }
try:
    from authencation.send_info_device import (
        register_device_on_startup as _real_register_startup,
        register_device_with_activation as _real_register_activation,
    )
except Exception:
    pass

# ─── Global log queue for SSE streaming ───────────────────────────────────────
_log_queue = queue.Queue()
_sse_clients = []

def sse_broadcast(msg, msg_type="info"):
    """Broadcast message to all SSE clients and persist it in Nexus.log."""
    _append_activity_log(msg, msg_type)
    payload = json.dumps({"msg": msg, "type": msg_type}, ensure_ascii=False)
    for client_queue in _sse_clients[:]:
        try:
            client_queue.put_nowait(payload)
        except:
            _sse_clients.remove(client_queue)


# Setup task manager to use SSE broadcast
set_sse_broadcast_func(sse_broadcast)

# ─── Ensure data directories exist on startup ─────────────────────────────────
_ensure_dirs()

# ─── User policy + activation gates ───────────────────────────────────────────
SCHEDULE_WORKER_STARTED = False
SCHEDULE_WORKER_LOCK = threading.Lock()
POLICY_ACCEPTED_THIS_SESSION = False


def _load_policy_acceptance():
    try:
        if not os.path.isfile(USER_POLICY_FILE):
            return {}
        with open(USER_POLICY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _policy_is_accepted():
    # DEV MODE: luon coi nhu da dong y chinh sach.
    return True


def _save_policy_acceptance():
    global POLICY_ACCEPTED_THIS_SESSION
    os.makedirs(os.path.dirname(USER_POLICY_FILE), exist_ok=True)
    payload = {
        "accepted": True,
        "version": USER_POLICY_VERSION,
        "acceptedAt": datetime.now().isoformat(timespec="seconds"),
    }
    temp_path = USER_POLICY_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, USER_POLICY_FILE)
    POLICY_ACCEPTED_THIS_SESSION = True
    return payload


def ensure_schedule_worker_started():
    """Khởi động các worker một lần sau khi đã đồng ý chính sách và kích hoạt."""
    global SCHEDULE_WORKER_STARTED
    if not _policy_is_accepted():
        print("📜 Chưa đồng ý chính sách người dùng, worker chưa được khởi động.", flush=True)
        return False
    with SCHEDULE_WORKER_LOCK:
        if SCHEDULE_WORKER_STARTED:
            return False
        start_schedule_worker()
        start_group_copy_worker()
        start_unread_worker()
        try:
            chatbot_worker.ensure_started()
        except Exception as e:
            print(f"[chatbot] khong start worker: {e}", flush=True)
        try:
            telegram_worker.ensure_started()
        except Exception as e:
            print(f"[telegram] khong start worker: {e}", flush=True)
        try:
            from features.inbox import worker as _iw
            _iw.ensure_started()
        except Exception as e:
            print(f"[inbox-sync] khong start worker: {e}", flush=True)
        SCHEDULE_WORKER_STARTED = True
        print("✅ Schedule worker, group-copy worker, unread worker, chatbot worker va telegram worker da khoi dong", flush=True)
        return True


def _activation_status_payload():
    from features.license import license_manager
    info = license_manager.get_license_info()
    return {
        "success": True,
        "activated": bool(info["activated"]),
        "tier": info["tier"],
        "tierName": info["tierName"],
        "tierBadge": info["tierBadge"],
        "description": info["description"],
        "days_remaining": int(info["daysRemaining"] or 0),
        "is_permanent": bool(info["isPermanent"]),
        "key": info.get("key", ""),
        "allowed_features": info["allowedFeatures"],
        "all_tiers": info["allTiers"],
        "message": f"License {info['tierName']} đang hoạt động",
    }


@app.context_processor
def inject_license_context():
    """Tự động tiêm thông tin gói bản quyền vào tất cả template Jinja2."""
    from features.license import license_manager
    info = license_manager.get_license_info()
    return {
        "license_info": info,
        "license_tier": info["tier"],
        "allowed_features": set(info["allowedFeatures"]),
        "default_nav_url": info["defaultUrl"],
    }


_CLIENT_ALLOWED_EXACT_PATHS = {
    "/inbox",
    "/favicon.ico",
    "/api/inbox/conversations",
    "/api/inbox/directory",
    "/api/inbox/sync",
    "/api/inbox/messages",
    "/api/inbox/send",
    "/api/inbox/recall",
    "/api/inbox/claim",
    "/api/inbox/read",
    "/api/inbox/presence",
    "/api/inbox/supervisor",
    "/api/inbox/supervisor/action",
    "/api/inbox/audit",
    "/api/inbox/stream",
    "/api/gateway/session/health",
    "/api/gateway/messages/group-latest",
    "/api/gateway/send-group-message",
    "/api/gateway/send-sms",
    "/api/gateway/status",
}
_CLIENT_ALLOWED_PREFIXES = ("/static/", "/media/")


def _is_client_allowed_path(path: str) -> bool:
    p = (path or "").strip() or "/"
    if p in _CLIENT_ALLOWED_EXACT_PATHS:
        return True
    return any(p.startswith(pref) for pref in _CLIENT_ALLOWED_PREFIXES)


@app.before_request
def require_activation_before_use():
    """Kiểm tra điều kiện trước khi xử lý request:
    1. Khởi động background workers.
    2. Cách ly máy Client (chỉ được vào /inbox).
    3. Bảo vệ và chặn các trang/API không thuộc gói License (Loại 1, 2, 3).
    """
    ensure_schedule_worker_started()
    if not gateway_auth.is_host_request():
        path = request.path or "/"
        if not _is_client_allowed_path(path):
            if path.startswith("/api/") or path == "/run" or request.method not in ("GET", "HEAD"):
                return jsonify({"success": False, "error": "Chỉ máy chủ Host mới có quyền truy cập trang/API quản trị."}), 403
            return redirect(url_for("inbox_page"))

    # ─── BẢO VỆ PHÂN HỆ THEO 3 LOẠI KEY LICENSE (LOẠI 1, 2, 3) ───
    from features.license import license_manager
    path = request.path or "/"
    if not license_manager.is_path_allowed(path, endpoint=request.endpoint):
        if path.startswith("/api/") or path == "/run" or request.method not in ("GET", "HEAD"):
            return jsonify({
                "success": False,
                "error": f"Phân hệ này không thuộc {license_manager.get_license_info()['tierName']}.",
                "code": "FEATURE_RESTRICTED_BY_LICENSE",
                "tier": license_manager.get_current_tier(),
            }), 403
        target = license_manager.get_redirect_for_forbidden_path(path)
        return redirect(target)

    return None


@app.route("/policy", methods=["GET"])
def policy_page():
    accepted = _policy_is_accepted()
    return render_template(
        "policy.html",
        policy_version=USER_POLICY_VERSION,
        accepted=accepted,
    )


@app.route("/api/policy/status", methods=["GET"])
def api_policy_status():
    data = _load_policy_acceptance()
    return jsonify({
        "success": True,
        "accepted": _policy_is_accepted(),
        "version": USER_POLICY_VERSION,
        "acceptedAt": data.get("acceptedAt", ""),
    })


@app.route("/api/policy/accept", methods=["POST"])
def api_policy_accept():
    data = request.get_json(silent=True) or request.form or {}
    confirmed = bool(data.get("confirmed") or data.get("accepted") or data.get("agree"))
    if not confirmed:
        return jsonify({
            "success": False,
            "error": "Vui lòng tích xác nhận đã đọc và đồng ý chính sách.",
        }), 400

    acceptance = _save_policy_acceptance()
    activation = _activation_status_payload() if DEVICE_TRACKING_ENABLED else {"activated": True}
    if activation.get("activated"):
        ensure_schedule_worker_started()
    next_url = (url_for("guide_page") + "?welcome=1") if activation.get("activated") else url_for("activation_page")
    return jsonify({
        "success": True,
        "message": "Đã ghi nhận đồng ý chính sách người dùng.",
        "acceptance": acceptance,
        "nextUrl": next_url,
    })


@app.route("/activation", methods=["GET"])
def activation_page():
    status = _activation_status_payload()
    return render_template(
        "activation.html",
        active_page="activation",
        activated=status.get("activated"),
        days_remaining=status.get("days_remaining", 0),
        activation_message=status.get("message", ""),
        activation_options=get_activation_duration_options(),
    )


@app.route("/api/activation/status", methods=["GET"])
def api_activation_status():
    return jsonify(_activation_status_payload())


def _current_plan():
    try:
        return get_license_plan()
    except Exception:
        # Lỗi đọc gói: mở full để không chặn nhầm người dùng hợp lệ.
        return {"activated": True, "planKey": "", "planLabel": "", "isPermanent": True,
                "daysRemaining": 0, "maxAccounts": 0, "multiAccountExec": True}


@app.route("/api/license/plan", methods=["GET"])
def api_license_plan():
    """Quyền tính năng theo gói license (giới hạn tài khoản, chọn nhiều TK)."""
    plan = _current_plan()
    try:
        plan["accountCount"] = len(load_accounts() or [])
    except Exception:
        plan["accountCount"] = 0
    return jsonify({"success": True, **plan})


@app.route("/api/activation/save", methods=["POST"])
def api_activation_save():
    from features.license import license_manager
    data = request.get_json(silent=True) or request.form or {}
    code_text = (data.get("code") or data.get("activation_code") or "").strip()
    if not code_text:
        return jsonify({"success": False, "message": "Vui lòng nhập mã kích hoạt."}), 400

    # 1. Thử xác thực theo định dạng key license 3 loại (JM1-..., JM2-..., JM3-... hoặc JM1-TEST...)
    ok, message, info = license_manager.activate_license_key(code_text)

    # 2. Nếu không khớp dạng JM1/2/3, thử kiểm tra theo cơ chế check key cũ (mã 12 ký tự thiết bị qua email)
    if not ok:
        try:
            dev_ok, dev_msg = save_user_activation_code(code_text)
            if dev_ok:
                ok = True
                message = dev_msg
                # Kích hoạt gói license (mặc định theo tier người dùng chọn, hoặc Loại 1 Full)
                tier = int(data.get("tier") or 1)
                license_manager.activate_license_key(license_manager.generate_license_key(tier=tier, days=30))
                info = license_manager.get_license_info()
            else:
                message = dev_msg
        except Exception as e:
            pass

    if ok and info.get("activated"):
        ensure_schedule_worker_started()

    return jsonify({
        "success": bool(ok),
        "message": message,
        "activated": bool(info.get("activated")),
        "tier": info.get("tier"),
        "tierName": info.get("tierName"),
        "tierBadge": info.get("tierBadge"),
        "days_remaining": info.get("daysRemaining", 0),
        "status_message": message,
        "license_info": info,
    }), 200 if ok else 400


@app.route("/api/activation/switch-tier", methods=["POST"])
def api_activation_switch_tier():
    """Chuyển nhanh giữa 3 loại key license (dành cho quản trị / test giao diện)."""
    from features.license import license_manager
    data = request.get_json(silent=True) or request.form or {}
    tier = int(data.get("tier") or 1)
    if tier not in (1, 2, 3):
        tier = 1
    sample_key = license_manager.generate_license_key(tier=tier, permanent=True)
    ok, message, info = license_manager.activate_license_key(sample_key)
    return jsonify({
        "success": bool(ok),
        "message": f"Đã chuyển sang {info['tierName']}.",
        "license_info": info,
    })


@app.route("/api/activation/generate-sample", methods=["POST"])
def api_activation_generate_sample():
    """Tạo key mẫu theo loại license."""
    from features.license import license_manager
    data = request.get_json(silent=True) or request.form or {}
    tier = int(data.get("tier") or 1)
    days = int(data.get("days") or 9999)
    key = license_manager.generate_license_key(tier=tier, days=days, permanent=(days >= 9999))
    return jsonify({
        "success": True,
        "tier": tier,
        "key": key,
        "tierName": license_manager.TIER_INFO.get(tier, {}).get("name", ""),
    })


@app.route("/api/activation/resend", methods=["POST"])
def api_activation_resend():
    try:
        data = request.get_json(silent=True) or request.form or {}
        duration_key = (data.get("duration_key") or data.get("plan") or "1m").strip()
        options_map = {item["key"]: item for item in get_activation_duration_options()}
        selected = options_map.get(duration_key, options_map.get("1m", {"key": duration_key, "label": duration_key}))
        sent = register_device_with_activation(verbose=True, duration_key=duration_key, force=True)
        status = _activation_status_payload()
        if not sent:
            return jsonify({
                "success": False,
                "sent": False,
                "activated": bool(status.get("activated")),
                "selected_duration": selected,
                "message": (
                    "Không gửi được mã kích hoạt. Kiểm tra cấu hình email trong .env: "
                    "SENDMAIL_USER phải đúng Gmail gửi, SENDMAIL_PASS phải là App Password 16 ký tự "
                    "chứ không phải mật khẩu Gmail thường. Nếu App Password có dấu cách, hệ thống đã tự bỏ khoảng trắng khi đăng nhập SMTP."
                ),
                "status_message": status.get("message", ""),
            }), 400
        return jsonify({
            "success": True,
            "sent": True,
            "activated": bool(status.get("activated")),
            "selected_duration": selected,
            "message": (
                f"Đã gửi mã kích hoạt 12 ký tự cho gói {selected.get('label', duration_key)}. "
                "Lưu ý: mã mới sẽ làm mã cũ hết hiệu lực."
            ),
            "status_message": status.get("message", ""),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/")
def index():
    from flask import redirect
    from features.license import license_manager
    info = license_manager.get_license_info()
    return redirect(info.get("defaultUrl") or url_for("dashboard_page"))


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")


@app.route("/members")
def members_page():
    return render_template("members.html", active_page="members")


@app.route("/get_members")
def get_members_page():
    return render_template("members.html", active_page="members")


@app.route("/accounts")
def accounts_page():
    return render_template("accounts.html", active_page="accounts")


@app.route("/guide")
def guide_page():
    return render_template("guide.html", active_page="guide")


@app.route("/messages")
def messages_page():
    return render_template("messages.html", active_page="messages")


@app.route("/groups")
def groups_page():
    return render_template("groups.html", active_page="groups")


@app.route("/chatbot")
def chatbot_page():
    return render_template("chatbot.html", active_page="chatbot")


@app.route("/telegram")
def telegram_page():
    """Trang Chatbot Telegram — chuyển hướng về tab Telegram trong trang Chatbot."""
    return redirect(url_for("chatbot_page", tab="telegram"))


@app.route("/api/bot/shared", methods=["GET"])
def api_bot_shared():
    """Cau hinh bot dung chung cho moi nen tang (Zalo, Telegram)."""
    try:
        chatbot_worker.ensure_started()
    except Exception:
        pass
    cfg = chatbot_store.load_shared_config()
    return jsonify({"success": True, "config": cfg, "status": chatbot_worker.status()})


@app.route("/api/bot/shared", methods=["POST"])
def api_bot_shared_save():
    """Luu cau hinh bot dung chung. Chi nhan cac key thuoc ve phan dung chung."""
    data = request.get_json(silent=True) or {}
    patch = {}
    for k in chatbot_store.SHARED_KEYS:
        if k in data:
            patch[k] = data[k]
    for k in ("strictDoc", "triggerEnabled"):
        if k in patch:
            patch[k] = bool(patch[k])
    if "modelCascade" in patch and isinstance(patch["modelCascade"], str):
        patch["modelCascade"] = [x.strip() for x in patch["modelCascade"].split(",") if x.strip()]
    cfg = chatbot_store.save_shared_config(patch)
    try:
        chatbot_worker.ensure_started()
    except Exception as e:
        return jsonify({"success": False, "error": f"Luu xong nhung worker loi: {e}"}), 500
    return jsonify({"success": True, "config": cfg, "status": chatbot_worker.status()})


@app.route("/api/chatbot/config", methods=["GET"])
def api_chatbot_config():
    cfg = chatbot_store.load_config()
    safe = dict(cfg)
    key = str(safe.get("geminiApiKey") or "")
    # tra key that cho UI tu dien, chi an khi log
    try:
        chatbot_worker.ensure_started()
    except Exception:
        pass
    return jsonify({"success": True, "config": safe, "status": chatbot_worker.status()})


@app.route("/api/chatbot/config", methods=["POST"])
def api_chatbot_save():
    data = request.get_json(silent=True) or {}
    patch = {}
    for k in ("enabled", "accountId", "targetGroupIds", "destGroupId", "mode",
              "geminiApiKey", "geminiApiKeys", "pollIntervalSec", "allowSelfReply", "sendGapSec",
              "systemPrompt", "globalContext", "modelCascade", "strictDoc", "handoffText",
              "replyToSource", "includeSenderName", "triggerEnabled", "triggerMode", "triggerKeywords"):
        if k in data:
            patch[k] = data[k]
    if "enabled" in patch:
        patch["enabled"] = bool(patch["enabled"])
    if "strictDoc" in patch:
        patch["strictDoc"] = bool(patch["strictDoc"])
    if "includeSenderName" in patch:
        patch["includeSenderName"] = bool(patch["includeSenderName"])
    if "triggerEnabled" in patch:
        patch["triggerEnabled"] = bool(patch["triggerEnabled"])
    if "modelCascade" in patch and isinstance(patch["modelCascade"], str):
        patch["modelCascade"] = [x.strip() for x in patch["modelCascade"].split(",") if x.strip()]
    cfg = chatbot_store.save_config(patch)
    try:
        chatbot_worker.ensure_started()
    except Exception as e:
        return jsonify({"success": False, "error": f"Luu xong nhung worker loi: {e}"}), 500
    return jsonify({"success": True, "config": cfg, "status": chatbot_worker.status()})


@app.route("/api/chatbot/keys/unmark", methods=["POST"])
def api_chatbot_keys_unmark():
    data = request.get_json(silent=True) or {}
    key_val = "" if data.get("all") else str(data.get("key") or "").strip()
    cfg = chatbot_store.unmark_key_limited(key_val)
    chatbot_worker.log("[gemini] da bo danh dau limit cho " + ("tat ca key" if not key_val else f"key ...{key_val[-4:]}"))
    return jsonify({"success": True, "config": cfg, "status": chatbot_worker.status()})


@app.route("/api/chatbot/logs", methods=["GET"])
def api_chatbot_logs():
    try:
        limit = int(request.args.get("limit") or 80)
    except Exception:
        limit = 80
    return jsonify({"success": True, "logs": chatbot_worker.get_logs(limit),
                    "status": chatbot_worker.status()})


@app.route("/api/chatbot/knowledge", methods=["GET"])
def api_chatbot_knowledge():
    try:
        info = chatbot_store.load_knowledge()
        text = info.get("text") or ""
        # UI chi hien 60k ky tu dau de nhe
        return jsonify({"success": True, "chars": info.get("chars") or 0,
                        "text": text[:60000], "path": info.get("path") or ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/chatbot/knowledge", methods=["POST"])
def api_chatbot_knowledge_save():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")
    try:
        info = chatbot_store.save_knowledge(text)
        chatbot_worker.log(f"[knowledge] da luu {info.get('chars')} ky tu vao {info.get('path')}")
        return jsonify({"success": True, **info})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/chatbot/send-test", methods=["POST"])
def api_chatbot_send_test():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip() or "🤖 Test tu trang Chatbot Jimmy BOT CHAT"
    cfg = chatbot_store.load_config()
    account_id = str(cfg.get("accountId") or "").strip()
    dest = str(cfg.get("destGroupId") or "").strip() or ((cfg.get("targetGroupIds") or [""])[0] if (cfg.get("targetGroupIds") or []) else "")
    if not account_id:
        return jsonify({"success": False, "error": "Chua chon tai khoan o trang Chatbot."}), 400
    if not dest:
        return jsonify({"success": False, "error": "Chua co nhom dich. Nhap DEST o trang Chatbot."}), 400
    try:
        account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    try:
        from features.messaging.send_link import send_message_smart
        out = send_message_smart(dest, message, zpw_enk, cookies, imei, is_group=True)
        if out.get("ok"):
            chatbot_worker.log(f"[send-test] -> {dest} ok=True msg={message[:120]}")
            return jsonify({"success": True, "dest": dest})
        return jsonify({"success": False, "error": out.get("error") or "Zalo tu choi gui.", "dest": dest}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM CHATBOT — Bot API long polling, dung chung bo nao Gemini + tai lieu
# voi chatbot Zalo. Token/chat ID do khach tu nhap, huong dan trong trang UI.
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/telegram/config", methods=["GET"])
def api_telegram_config():
    try:
        telegram_worker.ensure_started()
    except Exception:
        pass
    cfg = telegram_store.load_config()
    return jsonify({"success": True, "config": telegram_store.public_config(cfg),
                    "status": telegram_worker.status()})


@app.route("/api/telegram/config", methods=["POST"])
def api_telegram_save():
    """Luu cau hinh RIENG cho Telegram.

    Bao gom ca noi dung tu van (tai lieu, prompt, xung ho, cau handoff) rieng
    cua bot Telegram. Key Gemini, cascade, trigger dung chung qua /api/bot/shared.
    """
    data = request.get_json(silent=True) or {}
    patch = {}
    for k in ("enabled", "botToken", "botUsername", "botName", "allowedChatIds",
              "pollIntervalSec", "sendGapSec", "maxMessageChars",
              "useSharedGeminiKeys", "geminiApiKey", "replyToSource",
              "includeSenderName", "onlyPrivateChats", "ignoreBotMessages",
              "systemPrompt", "globalContext", "strictDoc", "handoffText"):
        if k in data:
            patch[k] = data[k]
    for k in ("enabled", "replyToSource", "includeSenderName",
              "onlyPrivateChats", "ignoreBotMessages", "useSharedGeminiKeys", "strictDoc"):
        if k in patch:
            patch[k] = bool(patch[k])
    if "allowedChatIds" in patch:
        v = patch["allowedChatIds"]
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        patch["allowedChatIds"] = [str(x).strip() for x in (v or []) if str(x).strip()]
    cfg = telegram_store.save_config(patch)
    try:
        telegram_worker.ensure_started()
    except Exception as e:
        return jsonify({"success": False, "error": f"Luu xong nhung worker loi: {e}"}), 500
    return jsonify({"success": True, "config": telegram_store.public_config(cfg),
                    "status": telegram_worker.status()})


@app.route("/api/telegram/knowledge", methods=["GET"])
def api_telegram_knowledge():
    try:
        info = telegram_store.load_knowledge()
        return jsonify({"success": True, "text": info.get("text") or "",
                        "chars": info.get("chars") or 0, "path": info.get("path") or ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "text": "", "chars": 0}), 500


@app.route("/api/telegram/knowledge", methods=["POST"])
def api_telegram_knowledge_save():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")
    try:
        info = telegram_store.save_knowledge(text)
        telegram_worker.log(f"[knowledge] da luu {info.get('chars')} ky tu vao {info.get('path')}")
        return jsonify({"success": True, "chars": info.get("chars") or 0,
                        "path": info.get("path") or ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/telegram/check-token", methods=["POST"])
def api_telegram_check_token():
    """Kiem tra token co hop le khong, tra them username bot de hien thi."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("botToken") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "Chua nhap Bot Token."}), 400
    res = telegram_client.get_me(token)
    if not res.get("ok"):
        return jsonify({"success": False, "error": res.get("error") or "Token khong hop le."}), 400
    return jsonify({"success": True,
                    "botUsername": res.get("username") or "",
                    "botName": res.get("name") or "",
                    "botId": res.get("id")})


@app.route("/api/telegram/check-chat", methods=["POST"])
def api_telegram_check_chat():
    """Kiem tra mot chat ID co truy cap duoc khong (dung de khong doan Chat ID)."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("botToken") or "").strip()
    chat_id = str(data.get("chatId") or "").strip()
    if not token or not chat_id:
        return jsonify({"success": False, "error": "Thieu Bot Token hoac Chat ID."}), 400
    res = telegram_client.get_chat(token, chat_id)
    if not res.get("ok"):
        return jsonify({"success": False, "error": res.get("error") or "Khong tim thay chat."}), 400
    info = res.get("result") or {}
    return jsonify({"success": True,
                    "title": info.get("title") or " ".join(
                        x for x in (str(info.get("first_name") or ""), str(info.get("last_name") or "")) if x),
                    "type": info.get("type") or "",
                    "username": info.get("username") or ""})


@app.route("/api/telegram/send-test", methods=["POST"])
def api_telegram_send_test():
    data = request.get_json(silent=True) or {}
    token = str(data.get("botToken") or "").strip()
    chat_id = str(data.get("chatId") or "").strip()
    message = str(data.get("message") or "").strip() or "Xin chao! Jimmy BOT CHAT da ket noi thanh cong."
    if not token or not chat_id:
        return jsonify({"success": False, "error": "Thieu Bot Token hoac Chat ID."}), 400
    res = telegram_client.send_message(token, chat_id, message)
    if res.get("ok"):
        telegram_worker.log(f"[send-test] -> {chat_id} ok=True msg={message[:120]}")
        return jsonify({"success": True})
    return jsonify({"success": False, "error": res.get("error") or "Telegram tu choi gui."}), 502


@app.route("/api/telegram/scan-chats", methods=["POST"])
def api_telegram_scan_chats():
    """Quet cac chat da nhan tin nhan de khach lay Chat ID ma khong can doan.

    Neu bot dang chay, danh sach chat da duoc worker ghi nho san.
    Neu bot dang tat, poll mot lan lay update gan nhat.
    """
    data = request.get_json(silent=True) or {}
    token = str(data.get("botToken") or "").strip()
    if not token:
        cfg = telegram_store.load_config()
        token = str(cfg.get("botToken") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "Chua co Bot Token."}), 400

    chats = []
    seen = {}
    for item in telegram_store.load_seen_chats():
        cid = str((item or {}).get("chatId") or "").strip()
        if cid and cid not in seen:
            seen[cid] = {
                "chatId": cid,
                "title": str((item or {}).get("title") or ""),
                "type": str((item or {}).get("type") or ""),
            }
            chats.append(seen[cid])

    # Bot chua chay -> poll mot lan de bat update gan nhat (offset -1 khong xoa du lieu).
    if not chats and not telegram_worker.is_running():
        res = telegram_client.get_updates(token, offset=-1, timeout=0)
        if res.get("ok"):
            for upd in (res.get("result") or []):
                rec = telegram_client.parse_update(upd)
                if not rec or not rec.get("chatId"):
                    continue
                cid = str(rec.get("chatId"))
                if cid in seen:
                    continue
                seen[cid] = {
                    "chatId": cid,
                    "title": str(rec.get("chatTitle") or ""),
                    "type": str(rec.get("chatType") or ""),
                }
                chats.append(seen[cid])
        else:
            return jsonify({"success": False, "error": res.get("error") or "Khong doc duoc tin nhan."}), 400

    return jsonify({"success": True, "chats": chats})


@app.route("/api/telegram/logs", methods=["GET"])
def api_telegram_logs():
    try:
        limit = int(request.args.get("limit") or 120)
    except Exception:
        limit = 120
    return jsonify({"success": True, "logs": telegram_worker.get_logs(limit),
                    "status": telegram_worker.status()})


# ═══════════════════════════════════════════════════════════════════════════
# GATEWAY ĐA MÁY (P3+P4): 1 Host ôm session thật, Client gọi qua token nội bộ.
# Client KHÔNG BAO GIỜ cầm cookies/zpwEnk. Host xếp hàng FIFO + chống ban.
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/gateway")
def gateway_page():
    return render_template("gateway.html", active_page="gateway")


def _parse_bool_flag(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(val)


def _ip_priority_key(ip: str, outbound_ip: str = "", preferred_ip: str = ""):
    if preferred_ip and ip == preferred_ip:
        return (0, ip)
    if outbound_ip and ip == outbound_ip:
        return (1, ip)
    if ip.startswith("192.168."):
        return (2, ip)
    if ip.startswith("10."):
        return (3, ip)
    if ip.startswith("169.254."):
        return (5, ip)
    return (4, ip)


def _detect_lan_ips() -> list:
    """Lấy danh sách IP LAN của Host, ưu tiên IP card mạng thật đi ra Internet."""
    import socket as _so
    raw_ips: list = []
    try:
        hn = _so.gethostname()
        for _ip in _so.gethostbyname_ex(hn)[2] or []:
            _ip = str(_ip or "").strip()
            if _ip and not _ip.startswith("127.") and _ip not in raw_ips:
                raw_ips.append(_ip)
    except Exception:
        pass
    outbound_ip = ""
    try:
        _s = _so.socket(_so.AF_INET, _so.SOCK_DGRAM)
        _s.settimeout(1.5)
        _s.connect(("8.8.8.8", 80))
        _lip = str(_s.getsockname()[0] or "").strip()
        _s.close()
        if _lip and not _lip.startswith("127."):
            outbound_ip = _lip
            if _lip not in raw_ips:
                raw_ips.append(_lip)
    except Exception:
        pass
    preferred_ip = ""
    try:
        preferred_ip = gateway_config.get_preferred_lan_ip()
        if preferred_ip and preferred_ip not in raw_ips:
            raw_ips.append(preferred_ip)
    except Exception:
        preferred_ip = ""
    raw_ips.sort(key=lambda x: _ip_priority_key(x, outbound_ip=outbound_ip, preferred_ip=preferred_ip))
    return raw_ips


def _build_share_inbox_link(
    host_url: str,
    token: str,
    is_loopback: bool,
    include_token: bool,
    preset_enabled: bool = False,
    preset_name: str = "",
    staff_sig: str = "",
) -> str:
    if not host_url:
        return ""
    from urllib.parse import urlencode
    params = []
    if include_token and is_loopback and token:
        params.append(("token", token))
    if preset_enabled and preset_name:
        params.append(("name", preset_name))
        sig = staff_sig or (gateway_config.sign_staff_name(preset_name, token) if (is_loopback and token) else "")
        if sig:
            params.append(("nsig", sig))
    base = f"{host_url.rstrip('/')}/inbox"
    return f"{base}?{urlencode(params)}" if params else base


def _build_staff_share_links(
    host_url: str,
    token: str,
    is_loopback: bool,
    include_token: bool,
    names: list,
    profiles: list = None,
) -> list:
    if not is_loopback:
        return []
    prof_map = {}
    for p in (profiles or []):
        if isinstance(p, dict) and p.get("name"):
            prof_map[str(p.get("name")).strip().lower()] = p
    per_staff_pw = gateway_config.get_per_staff_password_enabled()
    out = []
    for nm in names or []:
        sig = gateway_config.sign_staff_name(nm, token)
        p = prof_map.get(str(nm).strip().lower()) or {}
        eff_tok = (str(p.get("password") or "").strip() or token) if per_staff_pw else token
        link = _build_share_inbox_link(
            host_url, eff_tok, is_loopback, include_token,
            preset_enabled=True, preset_name=nm, staff_sig=sig,
        )
        out.append({
            "name": nm,
            "sig": sig,
            "link": link,
            "role": gateway_config.normalize_staff_role(p.get("role")),
            "accountIds": list(p.get("accountIds") or []),
            "password": str(p.get("password") or "") if is_loopback else "",
        })
    return out


@app.route("/api/gateway/config", methods=["GET"])
def api_gateway_config():
    from features.gateway.auth import client_ip, is_loopback as _lb
    tok = gateway_config.get_internal_token()
    ip = client_ip()
    loop = _lb(ip)
    show = tok if loop else (tok[:4] + "***" + tok[-3:] if len(tok) > 8 else "***")
    profiles = gateway_config.get_staff_profiles()
    if not loop:
        profiles = [{k: v for k, v in p.items() if k != "password"} for p in profiles]
    names = gateway_config.get_preset_staff_names()
    sigs = {nm: gateway_config.sign_staff_name(nm, tok) for nm in names} if loop else {}
    return jsonify({"success": True, "internalToken": show, "isLoopback": loop,
                    "allowLan": gateway_config.get_allowed_lan(),
                    "sendGapSec": gateway_config.get_send_gap(),
                    "publicUrl": gateway_tunnel.get_effective_public_url(gateway_config.get_public_url()),
                    "allowWan": gateway_config.get_allow_wan(),
                    "includeTokenInLink": gateway_config.get_include_token_in_link(),
                    "perStaffPassword": gateway_config.get_per_staff_password_enabled(),
                    "presetStaffEnabled": gateway_config.get_preset_staff_enabled(),
                    "presetStaffName": ", ".join(names) if names else gateway_config.get_preset_staff_name(),
                    "presetStaffNames": names,
                    "staffProfiles": profiles,
                    "staffSignatures": sigs,
                    "preferredLanIp": gateway_config.get_preferred_lan_ip(),
                    "sharedAccountIds": gateway_config.get_shared_account_ids(),
                    "clientIp": ip,
                    "tunnel": gateway_tunnel.get_status()})


@app.route("/api/gateway/config", methods=["POST"])
def api_gateway_config_save():
    from features.gateway.auth import client_ip, is_loopback as _lb
    if not _lb(client_ip()):
        return jsonify({"success": False, "error": "Chỉ được đổi cấu hình trên chính máy Host."}), 403
    data = request.get_json(silent=True) or {}
    if data.get("rotateToken"):
        import secrets as _s
        tok = _s.token_urlsafe(32)
        gateway_config._save_file_cfg({"internalToken": tok})
    elif "internalToken" in data:
        tok = str(data.get("internalToken") or "").strip()
        if not tok:
            return jsonify({"success": False, "error": "Mật khẩu không được để trống."}), 400
        gateway_config._save_file_cfg({"internalToken": tok})
    if "allowLan" in data:
        v = data.get("allowLan")
        if isinstance(v, list):
            v = ",".join(v)
        gateway_config._save_file_cfg({"allowLan": str(v or "")})
    if "publicUrl" in data:
        gateway_config._save_file_cfg({"publicUrl": str(data.get("publicUrl") or "").strip().rstrip("/")})
    if "preferredLanIp" in data:
        gateway_config._save_file_cfg({"preferredLanIp": str(data.get("preferredLanIp") or "").strip()})
    if "allowWan" in data:
        gateway_config._save_file_cfg({"allowWan": _parse_bool_flag(data.get("allowWan"), True)})
    if "includeTokenInLink" in data:
        gateway_config._save_file_cfg({"includeTokenInLink": _parse_bool_flag(data.get("includeTokenInLink"), True)})
    if "perStaffPassword" in data:
        gateway_config._save_file_cfg({"perStaffPassword": _parse_bool_flag(data.get("perStaffPassword"), False)})
    if "presetStaffEnabled" in data:
        gateway_config._save_file_cfg({"presetStaffEnabled": _parse_bool_flag(data.get("presetStaffEnabled"), False)})
    if "staffProfiles" in data:
        gateway_config.save_staff_profiles(data.get("staffProfiles"))
    elif "presetStaffNames" in data:
        names = gateway_config.parse_staff_names(data.get("presetStaffNames"))
        old_map = {str(p.get("name") or "").strip().lower(): p for p in gateway_config.get_staff_profiles()}
        rebuilt = []
        for nm in names:
            prev = old_map.get(nm.lower()) or {}
            rebuilt.append({"name": nm, "role": prev.get("role") or "sale", "accountIds": prev.get("accountIds") or []})
        gateway_config.save_staff_profiles(rebuilt)
    elif "presetStaffName" in data:
        names = gateway_config.parse_staff_names(data.get("presetStaffName"))
        old_map = {str(p.get("name") or "").strip().lower(): p for p in gateway_config.get_staff_profiles()}
        rebuilt = []
        for nm in names:
            prev = old_map.get(nm.lower()) or {}
            rebuilt.append({"name": nm, "role": prev.get("role") or "sale", "accountIds": prev.get("accountIds") or []})
        gateway_config.save_staff_profiles(rebuilt)
    if "sharedAccountIds" in data or "shared_account_ids" in data:
        v = data.get("sharedAccountIds", data.get("shared_account_ids"))
        if isinstance(v, str):
            v = [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
        shared_ids = [str(x).strip() for x in (v or []) if str(x).strip()]
        gateway_config._save_file_cfg({"sharedAccountIds": shared_ids})
        if shared_ids:
            try:
                from features.inbox import store as _ib_store
                _ib_store.save_config({"accountId": shared_ids[0]})
            except Exception:
                pass
    if "sendGapSec" in data:
        try:
            gateway_config._save_file_cfg({"sendGapSec": max(0.5, float(data.get("sendGapSec") or 1.2))})
        except Exception:
            pass
    return api_gateway_config()


@app.route("/api/gateway/session/health", methods=["GET", "POST"])
@gateway_auth.gateway_auth_required
def api_gateway_session_health():
    """Client kiểm tra acc sống/chết. KHÔNG trả key thật."""
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form or {}
    else:
        data = request.args
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    try:
        accs = load_accounts()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    def _pub(a):
        _cks = str(a.get("cookies") or "")
        _enk = str(a.get("zpwEnk") or "").strip()
        return {"accountId": a.get("accountId"), "name": a.get("name"),
                "avatarUrl": a.get("avatarUrl") or "",
                "uid": a.get("uid") or "",
                "loginCaptured": bool(a.get("loginCaptured")),
                "ready": bool(a.get("loginCaptured") and _enk),
                "hasSession": bool(_enk and "zpw_sek" in _cks)}
    if account_id:
        a = next((x for x in accs if str(x.get("accountId") or "") == account_id), None)
        if not a:
            return jsonify({"success": False, "error": "Khong tim thay account."}), 404
        return jsonify({"success": True, "account": _pub(a),
                        "circuit": gateway_queue.circuit_status(account_id),
                        "queueDepth": gateway_queue.queue_depths().get(account_id, 0)})
    return jsonify({"success": True, "accounts": [_pub(a) for a in accs]})


@app.route("/api/gateway/messages/group-latest", methods=["GET", "POST"])
@gateway_auth.gateway_auth_required
def api_gateway_group_latest():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form or {}
    else:
        data = request.args
    group_id = str(data.get("groupId") or data.get("group_id") or "").strip()
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    if not group_id or not account_id:
        return jsonify({"success": False, "error": "Thieu groupId/accountId."}), 400
    try:
        # dung session that cua Host, khong tra key ra ngoai
        account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=False)
        from features.messaging.last_messages import fetch_last_messages
        res = fetch_last_messages({f"{group_id}_1": "0"}, cookies, zpw_enk, imei, zpw_ver=get_zpw_ver())
        if not res.get("ok"):
            return jsonify({"success": False, "error": res.get("message") or "Zalo loi."}), 502
        items = [m for m in (res.get("groupMsgs") or []) if str(m.get("groupId") or "") == group_id]
        items.sort(key=lambda x: int(x.get("ts") or 0), reverse=True)
        latest = items[0] if items else None
        if latest:
            latest = {"msgId": latest.get("msgId"), "senderUid": latest.get("senderUid"),
                      "text": latest.get("text"), "ts": latest.get("ts"),
                      "msgType": latest.get("msgType")}
        return jsonify({"success": True, "groupId": group_id, "count": len(items), "latest": latest})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        msg = str(e)
        code = 502
        if "429" in msg or "431" in msg:
            code = 429
        return jsonify({"success": False, "error": msg[:300]}), code


@app.route("/api/gateway/send-group-message", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_gateway_send_group():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
        group_id = str(data.get("group_id") or data.get("groupId") or data.get("grid") or "").strip()
        message = str(data.get("message") or data.get("msg") or "").strip()
        photo_bytes, photo_name = None, ""
    else:
        account_id = str(request.form.get("accountId") or request.form.get("account_id") or "").strip()
        group_id = str(request.form.get("group_id") or request.form.get("groupId") or request.form.get("grid") or "").strip()
        message = str(request.form.get("message") or request.form.get("msg") or "").strip()
        f = request.files.get("photo") or request.files.get("image") or request.files.get("file")
        photo_bytes, photo_name = None, ""
        if f:
            try:
                photo_bytes = f.read()
                photo_name = f.filename or "image.jpg"
                if len(photo_bytes) > 20 * 1024 * 1024:
                    return jsonify({"success": False, "error": "Anh vuot qua 20MB."}), 400
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 400
    import re as _re
    m = _re.search(r"\d{8,}", group_id or "")
    if m:
        group_id = m.group(0)
    if not account_id or not group_id or (not message and not photo_bytes):
        return jsonify({"success": False, "error": "Thieu accountId/group_id/message."}), 400
    out = gateway_queue.enqueue(account_id, "group", group_id, message, photo_bytes, photo_name, timeout=90)
    code = 200 if out.get("ok") else (429 if ("429" in str(out.get("error") or "") or "ngat mach" in str(out.get("error") or "")) else 502)
    return jsonify({"success": bool(out.get("ok")), **out}), code


@app.route("/api/gateway/send-sms", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_gateway_send_sms():
    data = request.get_json(silent=True) or request.form or {}
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    to_uid = str(data.get("to_uid") or data.get("toid") or data.get("toId") or data.get("uid") or "").strip()
    import re as _re2
    mm = _re2.search(r"\d{8,}", to_uid or "")
    if mm:
        to_uid = mm.group(0)
    message = str(data.get("message") or data.get("msg") or "").strip()
    if not account_id or not to_uid or not message:
        return jsonify({"success": False, "error": "Thieu accountId/to_uid/message."}), 400
    out = gateway_queue.enqueue(account_id, "sms", to_uid, message, timeout=90)
    code = 200 if out.get("ok") else (429 if ("429" in str(out.get("error") or "") or "ngat mach" in str(out.get("error") or "")) else 502)
    return jsonify({"success": bool(out.get("ok")), **out}), code


@app.route("/api/gateway/status", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_gateway_status():
    depths = gateway_queue.queue_depths()
    circuits = {aid: gateway_queue.circuit_status(aid) for aid in depths}
    return jsonify({"success": True, "queueDepths": depths, "circuits": circuits,
                    "sendGapSec": gateway_config.get_send_gap(),
                    "tunnel": gateway_tunnel.get_status()})


@app.route("/api/gateway/share-info", methods=["GET"])
def api_gateway_share_info():
    """1-cham share: tra ve IP LAN + token + acc mac dinh + group de Client copy la chay.

    Mo tren chinh Host (loopback) thi tra full token. May LAN chi thay dang che.
    Nếu Host đã cấu hình Public URL (ngrok/cloudflare/tên miền) thì ưu tiên
    URL đó để máy khách ở xa (vài trăm km) chỉ cần mở 1 link + nhập 1 Token.
    """
    from features.gateway.auth import client_ip, is_loopback as _lb
    lan_ips = _detect_lan_ips()
    tok = gateway_config.get_internal_token()
    ip = client_ip()
    loop = _lb(ip)
    try:
        accs = load_accounts()
    except Exception:
        accs = []
    def _ready(a):
        _cks = str(a.get("cookies") or "")
        _enk = str(a.get("zpwEnk") or "").strip()
        return bool(a.get("loginCaptured") and _enk and "zpw_sek" in _cks)
    default_acc = next((a for a in accs if _ready(a)), (accs[0] if accs else None))
    # Danh sách acc được Host tick share (rỗng = acc Inbox đang chạy / acc mặc định).
    shared_ids = gateway_config.get_shared_account_ids()
    shared_accs = []
    try:
        for a in accs:
            aid = str(a.get("accountId") or "")
            if shared_ids and aid not in shared_ids:
                continue
            _cks = str(a.get("cookies") or "")
            _enk = str(a.get("zpwEnk") or "").strip()
            shared_accs.append({
                "accountId": aid,
                "name": str(a.get("name") or aid[:8]),
                "ready": bool(a.get("loginCaptured") and _enk and "zpw_sek" in _cks),
            })
    except Exception:
        pass
    groups = []
    if default_acc:
        for g in (default_acc.get("personalGroups") or [])[:30]:
            groups.append({"groupId": str(g.get("groupId") or ""), "name": str(g.get("name") or "")})
    main_ip = lan_ips[0] if lan_ips else ""
    lan_url = f"http://{main_ip}:5000" if main_ip else ""
    public_url = gateway_tunnel.get_effective_public_url(gateway_config.get_public_url())
    host_url = public_url or lan_url
    include_token = gateway_config.get_include_token_in_link()
    preset_enabled = gateway_config.get_preset_staff_enabled()
    staff_profiles = gateway_config.get_staff_profiles()
    preset_names = gateway_config.get_preset_staff_names()
    preset_name = preset_names[0] if len(preset_names) == 1 else ", ".join(preset_names)
    staff_sigs = {nm: gateway_config.sign_staff_name(nm, tok) for nm in preset_names} if loop else {}
    share_staff_links = _build_staff_share_links(host_url, tok, loop, include_token, preset_names, staff_profiles) if (loop and preset_enabled) else []
    share_inbox_link = _build_share_inbox_link(
        host_url, tok, loop, include_token,
        preset_enabled and len(preset_names) == 1,
        preset_names[0] if len(preset_names) == 1 else "",
    )
    all_accs_meta = []
    for a in accs:
        aid = str(a.get("accountId") or "").strip()
        if aid:
            all_accs_meta.append({
                "accountId": aid,
                "name": str(a.get("name") or aid[:8]),
                "ready": _ready(a),
            })
    return jsonify({
        "success": True,
        "isLoopback": loop,
        "lanIps": lan_ips,
        "mainIp": main_ip,
        "preferredLanIp": gateway_config.get_preferred_lan_ip(),
        "port": 5000,
        "hostUrl": host_url,
        "lanUrl": lan_url,
        "publicUrl": public_url,
        "allowWan": gateway_config.get_allow_wan(),
        "includeTokenInLink": include_token,
        "perStaffPassword": gateway_config.get_per_staff_password_enabled(),
        "presetStaffEnabled": preset_enabled,
        "presetStaffName": preset_name,
        "presetStaffNames": preset_names,
        "staffProfiles": staff_profiles if loop else [{k: v for k, v in p.items() if k != "password"} for p in staff_profiles],
        "staffSignatures": staff_sigs,
        "shareStaffLinks": share_staff_links,
        "sharedAccountIds": shared_ids,
        "sharedAccounts": shared_accs,
        "allAccounts": all_accs_meta,
        "shareInboxLink": share_inbox_link,
        "internalToken": tok if loop else (tok[:4] + "***" + tok[-3:] if len(tok) > 8 else "***"),
        "tokenFull": tok if loop else "",
        "defaultAccountId": str((default_acc or {}).get("accountId") or ""),
        "defaultAccountName": str((default_acc or {}).get("name") or ""),
        "ready": bool(default_acc and _ready(default_acc)),
        "groups": groups,
        "sendGapSec": gateway_config.get_send_gap(),
        "tunnel": gateway_tunnel.get_status(),
    })


@app.route("/api/gateway/tunnel/status", methods=["GET"])
def api_gateway_tunnel_status():
    from features.gateway.auth import client_ip, is_loopback as _lb
    if not _lb(client_ip()):
        return jsonify({"success": False, "error": "Chỉ xem trên chính máy Host."}), 403
    return jsonify({"success": True, **gateway_tunnel.get_status()})


@app.route("/api/gateway/tunnel/start", methods=["POST"])
def api_gateway_tunnel_start():
    from features.gateway.auth import client_ip, is_loopback as _lb
    if not _lb(client_ip()):
        return jsonify({"success": False, "error": "Chỉ bật link Public trên chính máy Host."}), 403
    data = request.get_json(silent=True) or {}
    st = gateway_tunnel.start_tunnel(port=5000, force=bool(data.get("force")))
    return jsonify({"success": True, **st})


@app.route("/api/gateway/tunnel/stop", methods=["POST"])
def api_gateway_tunnel_stop():
    from features.gateway.auth import client_ip, is_loopback as _lb
    if not _lb(client_ip()):
        return jsonify({"success": False, "error": "Chỉ tắt link Public trên chính máy Host."}), 403
    st = gateway_tunnel.stop_tunnel()
    return jsonify({"success": True, **st})


@app.route("/api/gateway/doctor", methods=["GET"])
def api_gateway_doctor():
    """Chẩn đoán 1 chạm cho non-code: Host có nghe 0.0.0.0 không, firewall mở chưa, link share là gì.

    Tất cả hiển thị bằng nút bấm trên /gateway, không cần chạy file .bat lẻ."""
    import socket as _so
    import subprocess as _sp
    from features.gateway.auth import client_ip, is_loopback as _lb
    lan_ips = _detect_lan_ips()
    # 1) Host có nghe 0.0.0.0:5000 không
    listening = ""
    try:
        _t = _so.socket(_so.AF_INET, _so.SOCK_STREAM)
        _t.settimeout(1.0)
        _t.connect(("127.0.0.1", 5000))
        _t.close()
        listening = "OK: Nexus đang nghe port 5000"
    except Exception as e:
        listening = f"LỖI: không nối được 127.0.0.1:5000 ({e}) — app chưa chạy?"
    # 2) Firewall có rule mở 5000 không (chỉ đọc, không cần admin)
    fw_rule = "không rõ"
    fw_open = None
    try:
        _r = _sp.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=Nexus 5000 LAN"],
                     capture_output=True, text=True, timeout=8)
        _out = ((_r.stdout or "") + (_r.stderr or "")).strip()
        if "No rules match" in _out or "Không có quy tắc" in _out or not _out:
            fw_rule = "CHƯA MỞ: chưa có rule Nexus 5000 LAN"
            fw_open = False
        else:
            fw_rule = "ĐÃ MỞ: có rule Nexus 5000 LAN"
            fw_open = True
    except Exception as e:
        fw_rule = f"không kiểm tra được firewall ({e})"
    try:
        tok = gateway_config.get_internal_token()
        token_ok = bool(tok and len(tok) >= 8)
    except Exception:
        token_ok = False
        tok = ""
    main_ip = lan_ips[0] if lan_ips else ""
    try:
        public_url = gateway_tunnel.get_effective_public_url(gateway_config.get_public_url())
    except Exception:
        public_url = ""
    host_url = public_url or (f"http://{main_ip}:5000" if main_ip else "")
    try:
        allow_wan = gateway_config.get_allow_wan()
    except Exception:
        allow_wan = True
    share_link = _build_share_inbox_link(
        host_url,
        tok,
        _lb(client_ip()),
        gateway_config.get_include_token_in_link(),
        gateway_config.get_preset_staff_enabled(),
        gateway_config.get_preset_staff_name(),
    ) if (host_url and token_ok) else (f"{host_url}/inbox" if host_url else "")
    steps = []
    if fw_open is False:
        steps.append("Bấm nút 'Copy lệnh mở firewall' → chuột phải PowerShell → Run as administrator → dán → Enter → quay lại bấm 'Chạy chẩn đoán'.")
    if not main_ip:
        steps.append("Không tìm được IP LAN — kiểm tra WiFi máy Host.")
    if not token_ok:
        steps.append("Chưa có Token — mở lại trang /gateway 1 lần để tự sinh.")
    if not public_url:
        steps.append("Muốn share xa vài trăm km: mở tunnel (cloudflared/ngrok) trỏ về port 5000 rồi dán Public URL ở mục 1.")
    return jsonify({
        "success": True,
        "listening": listening,
        "firewall": fw_rule,
        "firewallOpen": fw_open,
        "lanIps": lan_ips,
        "mainIp": main_ip,
        "hostUrl": host_url,
        "publicUrl": public_url,
        "allowWan": allow_wan,
        "tokenOk": token_ok,
        "shareLink": share_link,
        "clientIp": client_ip(),
        "nextSteps": steps,
        "fixCommand": 'netsh advfirewall firewall add rule name="Nexus 5000 LAN" dir=in action=allow protocol=TCP localport=5000',
    })


@app.route("/api/gateway/firewall/open", methods=["POST"])
def api_gateway_firewall_open():
    """Mở firewall cổng 5000 ngay trên giao diện (tự xin quyền Admin UAC nếu cần)."""
    from features.gateway.auth import client_ip, is_loopback as _lb
    if not _lb(client_ip()):
        return jsonify({"success": False, "error": "Chỉ được mở firewall trên chính máy Host."}), 403
    cmd = 'netsh advfirewall firewall add rule name="Nexus 5000 LAN" dir=in action=allow protocol=TCP localport=5000'
    import subprocess as _sp
    try:
        _r = _sp.run(["netsh", "advfirewall", "firewall", "add", "rule",
                      "name=Nexus 5000 LAN", "dir=in", "action=allow",
                      "protocol=TCP", "localport=5000"],
                     capture_output=True, text=True, timeout=15)
        _out = ((_r.stdout or "") + (_r.stderr or "")).strip()
        if _r.returncode == 0:
            return jsonify({"success": True, "message": "Đã mở firewall port 5000 ✅", "output": _out[:500]})
        if os.name == "nt" and ("elevation" in _out.lower() or "quyền admin" in _out.lower() or "run as administrator" in _out.lower()):
            try:
                _ps = (
                    "Start-Process netsh -ArgumentList "
                    "'advfirewall firewall add rule name=\"Nexus 5000 LAN\" dir=in action=allow protocol=TCP localport=5000' "
                    "-Verb RunAs -Wait"
                )
                _sp.run(["powershell", "-NoProfile", "-Command", _ps],
                        capture_output=True, text=True, timeout=25)
                _chk = _sp.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=Nexus 5000 LAN"],
                               capture_output=True, text=True, timeout=8)
                _chk_out = ((_chk.stdout or "") + (_chk.stderr or "")).strip()
                if _chk.returncode == 0 and "Nexus 5000 LAN" in _chk_out:
                    return jsonify({"success": True, "message": "Đã mở firewall port 5000 ✅"})
            except Exception:
                pass
            return jsonify({"success": False, "needAdmin": True,
                            "error": "Chưa mở được firewall (cần bấm Yes ở hộp thoại quyền Admin hoặc chạy lệnh bên dưới).",
                            "fixCommand": cmd, "output": _out[:500]}), 200
        return jsonify({"success": False, "error": _out[:500] or "Mở firewall thất bại.", "fixCommand": cmd}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:300], "fixCommand": cmd}), 200


@app.route("/friends")
def friends_page():
    return render_template("friends.html", active_page="friends")


@app.route("/api/friends", methods=["GET"])
def api_friends_list():
    """Danh sách bạn bè của tài khoản được chọn (API getfriends của Zalo Web)."""
    account_id = str(request.args.get("accountId") or request.args.get("account_id") or "").strip()
    if not account_id:
        return jsonify({"success": False, "error": "Thiếu accountId."}), 400
    account = get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    cookies = str(account.get("cookies") or "").strip()
    zpw_enk = str(account.get("zpwEnk") or "").strip()
    imei = str(account.get("imei") or "").strip()
    if not cookies or not zpw_enk:
        return jsonify({"success": False, "error": "Tài khoản chưa đủ cookies/zpwEnk. Hãy mở lại tài khoản ở trang Tài khoản."}), 400
    try:
        from features.friends.friend_service import get_friend_list
        friends = get_friend_list(zpw_enk, cookies, imei=imei)
    except Exception as exc:
        message = str(exc)
        if "429" in message:
            message = "Zalo đang giới hạn request (429). Hãy chờ 1-2 phút rồi bấm Làm mới."
        elif len(message) > 220:
            message = message[:220] + "..."
        return jsonify({"success": False, "error": f"Không lấy được danh sách bạn bè: {message}"}), 502
    return jsonify({"success": True, "friends": friends, "total": len(friends)})


@app.route("/api/friends/remove", methods=["POST"])
def api_friends_remove():
    """Xóa kết bạn với một UID theo tài khoản được chọn."""
    data = request.get_json(silent=True) or {}
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    user_id = str(data.get("userId") or data.get("uid") or "").strip()
    if not account_id or not user_id:
        return jsonify({"success": False, "error": "Thiếu accountId hoặc userId."}), 400
    account = get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    cookies = str(account.get("cookies") or "").strip()
    zpw_enk = str(account.get("zpwEnk") or "").strip()
    imei = str(account.get("imei") or "").strip()
    if not all([cookies, zpw_enk, imei]):
        return jsonify({"success": False, "error": "Tài khoản chưa đủ cookies/zpwEnk/IMEI."}), 400
    try:
        from features.messaging.remove_friend import remove_friend
        response_json, decoded = remove_friend(user_id, imei, zpw_enk, cookies, zpw_ver=get_zpw_ver())
    except Exception as exc:
        return jsonify({"success": False, "error": f"Không xóa được bạn bè: {exc}"}), 502

    outer_code = response_json.get("error_code", 0) if isinstance(response_json, dict) else -1
    inner_code = decoded.get("error_code", 0) if isinstance(decoded, dict) else 0
    ok = outer_code in (0, None) and inner_code in (0, None)
    if not ok:
        message = ""
        if isinstance(decoded, dict):
            message = str(decoded.get("error_message") or "")
        if not message and isinstance(response_json, dict):
            message = str(response_json.get("error_message") or "")
        return jsonify({"success": False, "error": message or f"Zalo trả lỗi (code {outer_code}/{inner_code})."}), 502
    return jsonify({"success": True, "message": "Đã xóa kết bạn."})


@app.route("/schedules")
def schedules_page():
    return render_template("schedules.html", active_page="marketing_group", initial_tab="group", page_title="Chiến dịch theo nhóm")


@app.route("/marketing/group")
def marketing_group_page():
    return render_template("schedules.html", active_page="marketing_group", initial_tab="group", page_title="Chiến dịch theo nhóm")


@app.route("/marketing/phone")
def marketing_phone_page():
    return render_template("schedules.html", active_page="marketing_phone", initial_tab="phone", page_title="Chiến dịch theo SĐT")


@app.route("/marketing/personal-groups")
def marketing_personal_groups_page():
    return render_template("schedules.html", active_page="personal_groups", initial_tab="personal-groups", page_title="Nhóm cá nhân")


@app.route("/marketing/schedules")
def marketing_schedules_page():
    return render_template("schedule_monitor.html", active_page="schedules", page_title="Lịch gửi")


@app.route("/marketing/group-copy")
def marketing_group_copy_page():
    return render_template("group_copy.html", active_page="group_copy")


@app.route("/api/messages/conversations", methods=["GET"])
def api_messages_list_conversations():
    """API: danh sách hội thoại đã lưu."""
    try:
        account_id = (request.args.get("account_id") or "").strip() or None
        status = (request.args.get("status") or "all").strip() or "all"
        q = (request.args.get("q") or "").strip() or None
        conversations = list_message_conversations(account_id=account_id, status=status, q=q)
        stats = get_message_stats(account_id=account_id)
        return jsonify({"success": True, "conversations": conversations, "stats": stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/messages/conversations", methods=["POST"])
def api_messages_create_conversation():
    """API: tạo hội thoại thủ công để test UI hoặc import dữ liệu sau này."""
    try:
        data = request.get_json(silent=True) or request.form or {}
        conversation = create_message_conversation(
            account_id=(data.get("account_id") or "").strip(),
            peer_id=(data.get("peer_id") or data.get("uid") or "").strip(),
            peer_name=(data.get("peer_name") or data.get("name") or "").strip(),
            peer_avatar=(data.get("peer_avatar") or data.get("avatar") or "").strip(),
            source=(data.get("source") or "manual").strip(),
            first_message=(data.get("first_message") or data.get("message") or "").strip(),
            direction=(data.get("direction") or "in").strip(),
        )
        return jsonify({"success": True, "conversation": conversation})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/messages/conversations/<conversation_id>", methods=["GET", "PATCH"])
def api_messages_conversation_detail(conversation_id):
    """API: xem/cập nhật hội thoại."""
    try:
        if request.method == "GET":
            conversation = get_message_conversation(conversation_id)
            if not conversation:
                return jsonify({"success": False, "error": "Không tìm thấy hội thoại"}), 404
            return jsonify({"success": True, "conversation": conversation})

        data = request.get_json(silent=True) or request.form or {}
        conversation = update_message_conversation(conversation_id, dict(data))
        return jsonify({"success": True, "conversation": conversation})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/messages/conversations/<conversation_id>/messages", methods=["POST"])
def api_messages_add_message(conversation_id):
    """API: lưu thêm tin nhắn vào hội thoại. Hiện chỉ lưu lịch sử, chưa tự gửi Zalo."""
    try:
        data = request.get_json(silent=True) or request.form or {}
        conversation = add_conversation_message(
            conversation_id,
            text=(data.get("text") or data.get("message") or "").strip(),
            direction=(data.get("direction") or "in").strip(),
            raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
        )
        return jsonify({"success": True, "conversation": conversation})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/messages/conversations/<conversation_id>/read", methods=["POST"])
def api_messages_mark_read(conversation_id):
    """API: đánh dấu hội thoại đã đọc."""
    try:
        conversation = mark_message_read(conversation_id)
        return jsonify({"success": True, "conversation": conversation})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/messages/settings", methods=["GET", "PATCH"])
def api_messages_settings():
    """API: thiết lập khung tự động kiểm tra/phản hồi tin nhắn."""
    try:
        if request.method == "GET":
            return jsonify({"success": True, "settings": get_message_settings()})
        data = request.get_json(silent=True) or request.form or {}
        settings = update_message_settings(dict(data))
        return jsonify({"success": True, "settings": settings})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/messages/check", methods=["POST"])
def api_messages_check():
    """API: quét tin ghim (board pin) của các nhóm để cập nhật tin chưa đọc."""
    try:
        data = request.get_json(silent=True) or request.form or {}
        account_id = (data.get("account_id") or "").strip()
        result = sync_unread_messages(account_id=account_id or None)
        settings = log_message_check_attempt(account_id=account_id, message=result.get("message", ""))
        return jsonify({
            "success": True,
            "synced_count": result.get("newCount", 0),
            "result": result,
            "settings": settings,
            "message": result.get("message", ""),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/messages/unread", methods=["GET"])
def api_messages_unread_list():
    """API: danh sách tin ghim chưa đọc đã lưu trong data/unread_messages.json."""
    try:
        account_id = (request.args.get("account_id") or "").strip() or None
        payload = list_unread_messages(account_id=account_id)
        return jsonify({"success": True, **payload})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/messages/reply", methods=["POST"])
def api_messages_reply():
    """API: trả lời (quote) một tin nhắn đã lưu trong danh sách."""
    data = request.get_json(silent=True) or {}
    item_id = str(data.get("itemId") or "").strip()
    message = str(data.get("message") or "").strip()
    if not item_id:
        return jsonify({"success": False, "error": "Thiếu itemId."}), 400
    if not message:
        return jsonify({"success": False, "error": "Nội dung trả lời không được để trống."}), 400

    payload = list_unread_messages()
    item = next((x for x in payload.get("items", []) if x.get("id") == item_id), None)
    if not item:
        return jsonify({"success": False, "error": "Không tìm thấy tin nhắn để trả lời."}), 404

    account_id = str(item.get("accountId") or "").strip()
    account = get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    cookies = str(account.get("cookies") or "").strip()
    zpw_enk = str(account.get("zpwEnk") or "").strip()
    imei = str(account.get("imei") or "").strip()
    if not all([cookies, zpw_enk, imei]):
        return jsonify({"success": False, "error": "Tài khoản chưa đủ cookies/zpwEnk/IMEI."}), 400

    is_group = item.get("threadType") != "friend"
    try:
        from features.messaging.quote_message import quote_message
        result = quote_message(
            cookies, zpw_enk, imei,
            thread_id=item.get("groupId"),
            message=message,
            is_group=is_group,
            qmsg_owner=item.get("senderUid"),
            qmsg_id=item.get("globalMsgId") or item.get("pinId"),
            qmsg_cli_id=item.get("clientMsgId"),
            qmsg_type=item.get("msgType"),
            qmsg_ts=item.get("createTime"),
            qmsg_text=item.get("title"),
            zpw_ver=get_zpw_ver(),
        )
    except Exception as exc:
        return jsonify({"success": False, "error": f"Không gửi được trả lời: {exc}"}), 502
    if not result.get("ok"):
        return jsonify({"success": False, "error": result.get("message") or "Zalo từ chối gửi trả lời."}), 502
    return jsonify({"success": True, "message": "Đã gửi trả lời."})


@app.route("/api/messages/unread/<path:item_id>", methods=["DELETE"])
def api_messages_unread_delete(item_id):
    """API: xóa một tin đã xem khỏi db; lần quét sau không thêm lại."""
    try:
        if not dismiss_unread_message(item_id):
            return jsonify({"success": False, "error": "Không tìm thấy tin nhắn"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/messages/unread/group/<group_id>", methods=["DELETE"])
def api_messages_unread_delete_group(group_id):
    """API: xóa toàn bộ tin của một nhóm khỏi db (đã xem hết)."""
    try:
        account_id = (request.args.get("account_id") or "").strip()
        removed = dismiss_group_messages(account_id, group_id)
        return jsonify({"success": True, "removed": removed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/messages/group-latest", methods=["GET", "POST"])
def api_messages_group_latest():
    """API tích hợp: lấy tin nhắn ghim mới nhất của một nhóm truyền vào.

    GET  /api/messages/group-latest?groupId=...&account_id=...
    POST /api/messages/group-latest  {"groupId": "...", "account_id": "..."}
    """
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form or {}
        else:
            data = request.args
        group_id = (data.get("groupId") or data.get("group_id") or "").strip()
        account_id = (data.get("account_id") or data.get("accountId") or "").strip()
        payload = get_group_latest_messages(group_id, account_id or None)
        return jsonify({"success": True, **payload})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    """API: Danh sách tài khoản Zalo."""
    try:
        accounts = load_accounts()
        return jsonify({"success": True, "accounts": accounts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    """API: Thêm tài khoản mới. Chỉ mở Chrome khi autoLaunch=true (mặc định false)."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    proxy = str(data.get("proxy") or "").strip()
    auto_launch = bool(data.get("autoLaunch", False))
    # Giới hạn số tài khoản đăng nhập theo gói license.
    plan = _current_plan()
    max_accounts = int(plan.get("maxAccounts") or 0)
    if max_accounts > 0:
        try:
            current = len(load_accounts() or [])
        except Exception:
            current = 0
        if current >= max_accounts:
            return jsonify({
                "success": False,
                "error": f"Gói hiện tại chỉ cho phép đăng nhập tối đa {max_accounts} tài khoản Zalo. "
                         f"Nâng lên gói 6 tháng trở lên để đăng nhập không giới hạn tài khoản.",
                "limitReached": True,
                "maxAccounts": max_accounts,
            }), 403
    try:
        account = create_account(name=name or None, proxy=proxy, auto_launch=auto_launch)
        return jsonify({"success": True, "account": account})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>/open", methods=["POST"])
def api_open_account(account_id):
    """API: Mở Chrome với profile tài khoản."""
    try:
        account = open_account(account_id)
        return jsonify({"success": True, "account": account})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>/close", methods=["POST"])
def api_close_account(account_id):
    """API: Đóng Chrome profile của tài khoản."""
    try:
        from features.accounts.account_manager import close_account
        close_account(account_id)
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>", methods=["PATCH"])
def api_update_account(account_id):
    """API: Cập nhật tài khoản: name/proxy."""
    data = request.get_json(silent=True) or {}

    try:
        fields = {}

        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Tên không được để trống."}), 400
            fields["name"] = name

        if "proxy" in data:
            fields["proxy"] = str(data.get("proxy") or "").strip()

        if not fields:
            return jsonify({"error": "Không có dữ liệu hợp lệ để cập nhật."}), 400

        account = update_account(account_id, **fields)
        if not account:
            return jsonify({"error": "Không tìm thấy tài khoản."}), 404
        return jsonify({"success": True, "account": account})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    """API: Xóa tài khoản và thư mục profile."""
    try:
        delete_account(account_id)
        return jsonify({"success": True, "message": "Đã xóa tài khoản."})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500





@app.route("/workspace")
def workspace_page():
    return render_template("workspace.html", active_page="workspace")


@app.route("/api/workspace", methods=["GET"])
def api_workspace_list():
    """Danh sach khong gian kieu zDesk: Web + PC + Mobile moi acc."""
    try:
        from features.workspace import pc_status, find_zalo_exe
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    try:
        accs = load_accounts()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    zalo_exe = find_zalo_exe()
    out = []
    for a in accs or []:
        aid = str(a.get("accountId") or "")
        _cks = str(a.get("cookies") or "")
        _enk = str(a.get("zpwEnk") or "").strip()
        st = pc_status(aid)
        out.append({
            "accountId": aid,
            "name": a.get("name") or "",
            "avatarUrl": a.get("avatarUrl") or "",
            "uid": a.get("uid") or "",
            "proxy": a.get("proxy") or "",
            "web": {"running": bool(a.get("remoteDebugPort")), "port": a.get("remoteDebugPort"),
                    "ready": bool(a.get("loginCaptured") and _enk),
                    "hasSession": bool(_enk and "zpw_sek" in _cks)},
            "pc": {"running": st.get("running"), "pid": st.get("pid"),
                   "dataDir": st.get("dataDir"), "exe": bool(zalo_exe)},
            "mobile": {"running": False, "note": "Can gia lap — mo thu cong"},
        })
    return jsonify({"success": True, "workspaces": out, "zaloExe": zalo_exe,
                    "zaloInstalled": bool(zalo_exe)})


@app.route("/api/workspace/<account_id>/pc/open", methods=["POST"])
def api_workspace_pc_open(account_id):
    try:
        from features.workspace import pc_launch
        return jsonify({"success": True, **pc_launch(account_id)})
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/workspace/<account_id>/pc/close", methods=["POST"])
def api_workspace_pc_close(account_id):
    try:
        from features.workspace import pc_close
        return jsonify({"success": True, **pc_close(account_id)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/workspace/<account_id>/backup", methods=["GET"])
def api_workspace_backup(account_id):
    """Tai zip 1-click: profiles/<id> + profiles-zalopc/<id> + account.json."""
    try:
        from flask import send_file
        from features.workspace import workspace_backup
        tmp = workspace_backup(account_id)
        return send_file(tmp, mimetype="application/zip", as_attachment=True,
                         download_name=f"workspace-{account_id[:8]}.zip")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/workspace/restore", methods=["POST"])
def api_workspace_restore():
    """Nap lai workspace tu file zip backup."""
    try:
        from features.workspace import workspace_restore
        f = request.files.get("file")
        if f is None:
            return jsonify({"success": False, "error": "Thieu file zip."}), 400
        import tempfile as _tf
        tmp = _tf.mktemp(suffix=".zip")
        f.save(tmp)
        out = workspace_restore(tmp)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return jsonify({"success": True, **out})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


        return jsonify({"success": True, **out})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
# INBOX CHUNG 5 MAY: 1 Host om session, 5 client dung chung giao dien web.
# Realtime qua SSE rieng, claim chong tra loi trung, presence, audit.
# ═══════════════════════════════════════════════════════════════════════════
_inbox_sse_clients = []


def inbox_broadcast(payload: str, kind: str = "inbox"):
    data = json.dumps({"inbox": payload, "kind": kind}, ensure_ascii=False)
    for q in _inbox_sse_clients[:]:
        try:
            q.put_nowait(data)
        except Exception:
            try:
                _inbox_sse_clients.remove(q)
            except Exception:
                pass
    try:
        sse_broadcast(f"[inbox] {kind}", "info")
    except Exception:
        pass


def _extract_candidate_staff():
    """Lấy (name, sig) ứng viên theo thứ tự ưu tiên: query -> JSON/form -> header -> cookie."""
    h_name = str(request.headers.get("X-Staff") or "").strip()
    h_sig = str(request.headers.get("X-Staff-Sig") or "").strip()
    c_name = str(request.cookies.get("ib_staff") or "").strip()
    c_sig = str(request.cookies.get("ib_staff_sig") or "").strip()

    def _fallback_sig(nm: str, sig: str) -> str:
        if sig:
            return sig
        low = nm.lower()
        if h_sig and (not h_name or h_name.lower() == low):
            return h_sig
        if c_sig and (not c_name or c_name.lower() == low):
            return c_sig
        return ""

    q_name = str(request.args.get("name") or request.args.get("staff") or "").strip()
    q_sig = str(request.args.get("nsig") or "").strip()
    if q_name:
        return q_name, _fallback_sig(q_name, q_sig)
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict):
        b_name = str(body.get("staff") or body.get("name") or "").strip()
        b_sig = str(body.get("staffSig") or body.get("nsig") or "").strip()
        if b_name:
            return b_name, _fallback_sig(b_name, b_sig)
    f_name = str(request.form.get("staff") or request.form.get("name") or "").strip()
    f_sig = str(request.form.get("staffSig") or request.form.get("nsig") or "").strip()
    if f_name:
        return f_name, _fallback_sig(f_name, f_sig)
    if h_name:
        return h_name, _fallback_sig(h_name, h_sig)
    return c_name, c_sig


def _resolve_client_staff():
    """Trả về (staff_name, is_locked, error_msg) có xác thực link riêng từng nhân viên."""
    from features.inbox import store as _st
    cand_name, cand_sig = _extract_candidate_staff()
    if gateway_auth.is_host_request():
        if not cand_name or cand_name.strip().lower() in ("nv1", "admin"):
            return "Super Admin", False, ""
        return cand_name, False, ""
    if gateway_config.get_preset_staff_enabled():
        allowed = gateway_config.get_preset_staff_names()
        if cand_name and cand_sig and gateway_config.verify_staff_name(cand_name, cand_sig):
            std = gateway_config.resolve_allowed_staff_name(cand_name)
            if not std:
                return "", True, f"Nhân viên '{cand_name}' đã bị gỡ khỏi danh sách được cấp quyền."
            mod = _st.staff_moderation_status(std)
            if mod.get("banned"):
                return "", True, f"Tài khoản '{std}' đã bị cấm truy cập hệ thống."
            if mod.get("locked"):
                return "", True, f"Tài khoản '{std}' đang bị tạm khóa bởi quản trị viên."
            return std, True, ""
        if len(allowed) == 1:
            mod = _st.staff_moderation_status(allowed[0])
            if mod.get("banned"):
                return "", True, f"Tài khoản '{allowed[0]}' đã bị cấm truy cập hệ thống."
            if mod.get("locked"):
                return "", True, f"Tài khoản '{allowed[0]}' đang bị tạm khóa bởi quản trị viên."
            return allowed[0], True, ""
        return "", True, "Vui lòng mở đúng link riêng được cấp cho bạn (thiếu tên nhân viên hợp lệ)."
    resolved = cand_name or "nv1"
    mod = _st.staff_moderation_status(resolved)
    if mod.get("banned"):
        return "", True, f"Tài khoản '{resolved}' đã bị cấm truy cập hệ thống."
    if mod.get("locked"):
        return "", True, f"Tài khoản '{resolved}' đang bị tạm khóa bởi quản trị viên."
    return resolved, False, ""


def _locked_client_staff_name() -> str:
    staff, locked, _err = _resolve_client_staff()
    return staff if locked else ""


def _inbox_scope_for_staff(staff: str, is_host: bool, fallback_acc: str = "") -> dict:
    """Xác định vai trò ('super_admin'|'manager'|'sale') và danh sách tài khoản Zalo được phép truy cập."""
    from features.inbox import store as _st
    role = "super_admin" if is_host else _st.staff_role(staff)
    if not is_host and role in ("admin", "super_admin"):
        role = "manager"
    can_supervise = is_host or role in ("super_admin", "admin", "manager")

    all_accs = []
    acc_meta = {}
    try:
        for a in (load_accounts() or []):
            aid = str(a.get("accountId") or "").strip()
            if not aid:
                continue
            _cks = str(a.get("cookies") or "")
            _enk = str(a.get("zpwEnk") or "").strip()
            ready = bool(a.get("loginCaptured") and _enk and "zpw_sek" in _cks)
            meta = {
                "id": aid,
                "name": str(a.get("name") or aid[:8]),
                "avatar": str(a.get("avatarUrl") or ""),
                "ready": ready,
                "running": bool(a.get("running")),
            }
            acc_meta[aid] = meta
            all_accs.append(meta)
    except Exception:
        pass

    shared_ids = gateway_config.get_shared_account_ids()
    assigned_ids = [] if can_supervise else gateway_config.get_staff_account_ids(staff)
    profile_acc_ids = []
    for p in (gateway_config.get_staff_profiles() or []):
        for aid in (p.get("accountIds") or []):
            if aid and aid not in profile_acc_ids:
                profile_acc_ids.append(aid)

    if can_supervise:
        allowed = []
        for aid in (shared_ids + profile_acc_ids + [a["id"] for a in all_accs if a.get("ready")] + ([fallback_acc] if fallback_acc else [])):
            s = str(aid or "").strip()
            if s and s not in allowed:
                allowed.append(s)
        if not allowed:
            allowed = [a["id"] for a in all_accs]
    else:
        if assigned_ids:
            allowed = list(assigned_ids)
        elif shared_ids:
            allowed = list(shared_ids)
        elif fallback_acc:
            allowed = [fallback_acc]
        else:
            allowed = [a["id"] for a in all_accs if a.get("ready")] or [a["id"] for a in all_accs[:1]]

    allowed_list = [acc_meta.get(aid) or {"id": aid, "name": aid[:8], "avatar": "", "ready": False, "running": False} for aid in allowed]
    return {
        "role": role,
        "canSupervise": can_supervise,
        "canTakeover": can_supervise,
        "allowedIds": allowed,
        "allowedAccounts": allowed_list,
        "allAccounts": all_accs,
        "accountMeta": acc_meta,
    }


@app.route("/inbox")
def inbox_page():
    is_host = gateway_auth.is_host_request()
    staff, locked, err = _resolve_client_staff()
    preset_staff = staff if (locked and not is_host and staff) else ""
    preset_sig = gateway_config.sign_staff_name(preset_staff) if preset_staff else ""
    eff_staff = staff or ("admin" if is_host else "nv1")
    scope = _inbox_scope_for_staff(eff_staff, is_host)
    html = render_template(
        "inbox.html",
        active_page="inbox",
        is_host=is_host,
        preset_staff=preset_staff,
        preset_staff_sig=preset_sig,
        staff_role=scope["role"],
        can_supervise=scope["canSupervise"],
        staff_link_required=bool(locked and not is_host and not staff and err),
    )
    resp = app.make_response(html)
    if preset_staff and preset_sig:
        resp.set_cookie("ib_staff", preset_staff, max_age=30 * 86400, samesite="Lax")
        resp.set_cookie("ib_staff_sig", preset_sig, max_age=30 * 86400, samesite="Lax")
    return resp


def _inbox_staff():
    staff, _locked, _err = _resolve_client_staff()
    return staff or "nv1"


@app.route("/api/inbox/conversations", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_inbox_conversations():
    try:
        from features.inbox import store as _st
        from features.inbox import worker as _iw
        _iw.ensure_started()
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("admin" if is_host else "nv1")
        _st.ensure_staff(staff)
        _st.heartbeat(staff)
        cfg = _st.get_config()
        active_acc = str(cfg.get("accountId") or "").strip()
        if not active_acc:
            try:
                active_acc = str(_iw._pick_account() or "").strip()
                if active_acc:
                    cfg = _st.save_config({"accountId": active_acc})
            except Exception:
                pass
        scope = _inbox_scope_for_staff(staff, is_host, fallback_acc=active_acc)
        req_acc = str(request.args.get("accountId") or request.args.get("account_id") or "").strip()
        if req_acc and req_acc != "all":
            if scope["allowedIds"] and req_acc not in scope["allowedIds"]:
                return jsonify({"success": False, "error": "Bạn không có quyền xem tài khoản Zalo này."}), 403
            filter_aids = [req_acc]
            dir_acc = req_acc
        else:
            filter_aids = scope["allowedIds"] or ([active_acc] if active_acc else [])
            dir_acc = filter_aids[0] if filter_aids else active_acc
        gids = list(cfg.get("onlyGroupIds") or [])
        fids = list(cfg.get("onlyFriendIds") or [])
        hide_empty = bool(cfg.get("hideEmpty"))
        convs = _st.list_conversations(
            active_acc, gids, fids, hide_empty,
            account_ids=filter_aids, account_meta=scope["accountMeta"],
        )
        return jsonify({
            "success": True,
            "conversations": convs,
            "directory": _st.get_directory(dir_acc),
            "config": cfg,
            "accountId": active_acc,
            "effectiveStaff": staff,
            "staffRole": scope["role"],
            "canSupervise": scope["canSupervise"],
            "canTakeover": scope["canTakeover"],
            "allowedAccounts": scope["allowedAccounts"],
            "staffProfiles": gateway_config.get_staff_profiles() if scope["canSupervise"] else [],
            "staffLocked": bool(locked),
            "staffSig": (gateway_config.sign_staff_name(staff) if locked else ""),
            "online": _st.online_staff(),
            "presenceDetails": _st.presence_details(),
            "users": _st.users_list(),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/directory", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_inbox_directory():
    try:
        from features.inbox import store as _st
        from features.inbox import worker as _iw
        _iw.ensure_started()
        acc = ""
        try:
            acc = _iw._pick_account()
            _iw._sync_directory(acc)
        except Exception:
            pass
        return jsonify({"success": True, "directory": _st.get_directory(acc)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/sync", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_sync():
    try:
        from features.inbox import worker as _iw
        data = request.get_json(silent=True) or request.form or {}
        account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
        return jsonify({"success": True, **_iw.force_sync(account_id)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/messages", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_inbox_messages():
    try:
        from features.inbox import store as _st
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("admin" if is_host else "nv1")
        conv_id = str(request.args.get("convId") or request.args.get("conv_id") or "").strip()
        limit = int(request.args.get("limit") or 80)
        out = _st.get_messages(conv_id, limit)
        if out is None:
            return jsonify({"success": False, "error": "Khong tim thay hoi thoai."}), 404
        scope = _inbox_scope_for_staff(staff, is_host)
        conv_acc = str(((out.get("conv") or {}).get("accountId")) or "").strip()
        if not scope["canSupervise"] and scope["allowedIds"] and conv_acc and conv_acc not in scope["allowedIds"]:
            return jsonify({"success": False, "error": "Bạn không có quyền xem hội thoại của tài khoản này."}), 403
        return jsonify({"success": True, **out})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/send", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_send():
    try:
        from features.inbox import store as _st
        data = request.get_json(silent=True) or request.form or {}
        conv_id = str(data.get("convId") or data.get("conv_id") or "").strip()
        text = str(data.get("message") or data.get("msg") or data.get("text") or "")
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("admin" if is_host else "nv1")
        _st.ensure_staff(staff)
        scope = _inbox_scope_for_staff(staff, is_host)
        conv = None
        if conv_id:
            from features.inbox.store import _load_inbox as _li
            db = _li()
            conv = (db.get("conversations") or {}).get(conv_id)
        if conv is None:
            return jsonify({"success": False, "error": "Khong tim thay hoi thoai."}), 404
        conv_acc = str((conv or {}).get("accountId") or "").strip()
        if not scope["canSupervise"] and scope["allowedIds"] and conv_acc and conv_acc not in scope["allowedIds"]:
            return jsonify({"success": False, "error": "Bạn không có quyền nhắn trên tài khoản Zalo này."}), 403
        claim = (conv or {}).get("claim")
        claim_owner = str((claim or {}).get("staff") or "").strip() if isinstance(claim, dict) else ""
        claim_active = isinstance(claim, dict) and float(claim.get("until") or 0) > time.time() and claim_owner and claim_owner != staff
        if claim_active and not scope["canTakeover"]:
            return jsonify({"success": False, "error": f"Dang do {claim_owner} xu ly. Xin xac nhan truoc khi gui chen.",
                            "claimedBy": claim_owner, "needConfirm": True}), 409
        if claim_active and scope["canTakeover"]:
            _st.audit("takeover_send", staff, conv_id, f"nhắn thay {claim_owner}: {text[:40]}")
        _st.heartbeat(staff, conv_id=conv_id, conv_name=str((conv or {}).get("name") or ""), action="send")
        up_file = (
            request.files.get("photo")
            or request.files.get("image")
            or request.files.get("file")
            or request.files.get("attachment")
        )
        photo_bytes, photo_name = None, ""
        file_bytes, file_name = None, ""
        if up_file:
            raw_bytes = up_file.read()
            raw_name = os.path.basename(up_file.filename or "file.bin") or "file.bin"
            ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
            is_photo_field = ("photo" in (request.files or {}) or "image" in (request.files or {})) and ext in ("jpg", "jpeg", "png", "webp", "")
            if ext in ("jpg", "jpeg", "png", "webp") or is_photo_field:
                photo_bytes, photo_name = raw_bytes, raw_name
                if len(photo_bytes) > 20 * 1024 * 1024:
                    return jsonify({"success": False, "error": "Ảnh vượt quá 20MB."}), 400
            else:
                file_bytes, file_name = raw_bytes, raw_name
                if len(file_bytes) > 50 * 1024 * 1024:
                    return jsonify({"success": False, "error": "File vượt quá 50MB."}), 400
        account_id = str(conv.get("accountId") or "").strip()
        kind = str(conv.get("kind") or "group").strip()
        key = str(conv.get("key") or "").strip()
        out = gateway_queue.enqueue(
            account_id, kind, key, text,
            photo_bytes, photo_name, timeout=120,
            file_bytes=file_bytes, file_name=file_name,
        )
        if not out.get("ok"):
            code = 429 if ("429" in str(out.get("error") or "") or "ngat mach" in str(out.get("error") or "")) else 502
            return jsonify({"success": False, **out}), code
        meta = {
            "mediaToken": "",
            "zaloMsgId": str(out.get("zaloMsgId") or "").strip(),
            "cliMsgId": str(out.get("cliMsgId") or "").strip(),
        }
        if photo_bytes:
            try:
                from features.inbox import media as _md
                tok = _md.save_media(photo_bytes, photo_name, staff)
                meta.update({
                    "mediaToken": tok,
                    "thumb": f"/media/{tok}" if tok else "",
                    "msgType": "chat.photo",
                    "fileName": photo_name,
                    "fileSize": len(photo_bytes),
                })
            except Exception:
                pass
        elif file_bytes:
            try:
                from features.inbox import media as _md
                tok = _md.save_media(file_bytes, file_name, staff)
                meta.update({
                    "mediaToken": tok,
                    "thumb": "",
                    "href": str(out.get("fileUrl") or "").strip(),
                    "msgType": "share.file",
                    "fileName": file_name,
                    "fileSize": len(file_bytes),
                })
            except Exception:
                pass
        saved = _st.add_outbound(account_id, kind, key, staff, text, meta)
        rd = _st.mark_read(saved["convId"], staff, saved["message"].get("msgId"))
        try:
            import json as _js
            inbox_broadcast(_js.dumps({"convId": saved["convId"], "message": saved["message"]}, ensure_ascii=False), "sent")
            inbox_broadcast(_js.dumps({"convId": saved["convId"], "staff": staff, "msgId": rd.get("msgId", ""), "at": rd.get("at", 0)}, ensure_ascii=False), "read")
        except Exception:
            pass
        return jsonify({"success": True, **out, "inbox": saved})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/recall", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_recall():
    try:
        from features.inbox import store as _st
        from features.messaging.undo_msg import undo_message
        data = request.get_json(silent=True) or request.form or {}
        conv_id = str(data.get("convId") or data.get("conv_id") or "").strip()
        msg_id = str(data.get("msgId") or data.get("msg_id") or "").strip()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or "nv1"
        if not conv_id or not msg_id:
            return jsonify({"success": False, "error": "Thiếu convId hoặc msgId."}), 400
        conv, msg = _st.find_message(conv_id, msg_id)
        if not conv:
            return jsonify({"success": False, "error": "Không tìm thấy hội thoại."}), 404
        if not msg:
            return jsonify({"success": False, "error": "Không tìm thấy tin nhắn."}), 404
        msg_from = str(msg.get("from") or "")
        if msg_from not in ("staff", "me"):
            return jsonify({"success": False, "error": "Chỉ thu hồi được tin nhắn do bên mình gửi."}), 400
        is_host = gateway_auth.is_host_request()
        can_mgr_recall = is_host or _st.staff_role(staff) in ("admin", "manager")
        msg_owner = str(msg.get("staff") or "").strip()
        if msg_from == "staff":
            if msg_owner and msg_owner.lower() != staff.strip().lower() and not can_mgr_recall:
                return jsonify({"success": False, "error": f"Chỉ người gửi tin nhắn này ({msg_owner}) mới được phép thu hồi."}), 403
        elif not can_mgr_recall:
            return jsonify({"success": False, "error": "Chỉ máy chính hoặc Trưởng phòng mới được thu hồi tin nhắn gửi từ tài khoản gốc."}), 403
        if msg.get("recalled") or str(msg.get("msgType") or "") == "chat.undo":
            return jsonify({"success": False, "error": "Tin nhắn này đã được thu hồi trước đó."}), 400

        account_id = str(conv.get("accountId") or "").strip()
        kind = str(conv.get("kind") or "group").strip()
        target_id = str(conv.get("key") or "").strip()
        is_group = (kind != "sms")
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        real_msg_id = "" if msg_id.startswith("staff-") else msg_id
        cli_msg_id = str(msg.get("cliMsgId") or "").strip()

        # Fallback: nếu tin nhắn cũ chưa lưu cliMsgId/real_msg_id, thử đồng bộ nhanh từ Zalo
        if not real_msg_id or not cli_msg_id:
            try:
                from features.messaging.last_messages import fetch_last_messages
                t_key = f"{target_id}_{1 if is_group else 0}"
                res = fetch_last_messages({t_key: "0"}, cookies, zpw_enk, imei, zpw_ver=get_zpw_ver())
                items = (res.get("groupMsgs") if is_group else res.get("userMsgs")) or []
                txt_want = str(msg.get("text") or "").strip()
                for item in reversed(items):
                    if real_msg_id and str(item.get("msgId") or "") == real_msg_id:
                        cli_msg_id = str(item.get("cliMsgId") or "").strip()
                        break
                    if not real_msg_id and txt_want and str(item.get("text") or "").strip() == txt_want and str(item.get("senderUid") or "") in ("", "0"):
                        real_msg_id = str(item.get("msgId") or "").strip()
                        cli_msg_id = str(item.get("cliMsgId") or "").strip()
                        break
            except Exception:
                pass

        if not real_msg_id or not cli_msg_id:
            return jsonify({"success": False, "error": "Không tìm thấy mã tin nhắn Zalo (chỉ thu hồi được tin vừa gửi hoặc tin đã đồng bộ đầy đủ)."}), 400

        out = undo_message(
            cookies, zpw_enk, target_id, imei,
            real_msg_id, cli_msg_id, is_group=is_group, zpw_ver=get_zpw_ver(),
        )
        if not out.get("ok"):
            return jsonify({"success": False, "error": out.get("error") or "Zalo từ chối thu hồi tin nhắn."}), 502

        recalled = _st.recall_message(conv_id, msg_id, staff)
        try:
            import json as _js
            inbox_broadcast(_js.dumps({"convId": conv_id, "msgId": msg_id, "message": recalled.get("message")}, ensure_ascii=False), "recalled")
        except Exception:
            pass
        return jsonify({"success": True, "recalled": recalled})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/claim", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_claim():
    try:
        from features.inbox import store as _st
        data = request.get_json(silent=True) or request.form or {}
        conv_id = str(data.get("convId") or data.get("conv_id") or "").strip()
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("admin" if is_host else "nv1")
        ttl = int(data.get("ttlSec") or data.get("ttl") or 600)
        action = str(data.get("action") or "claim").strip()
        can_takeover = is_host or _st.staff_role(staff) in ("admin", "manager")
        if action == "release":
            out = _st.release(conv_id, staff)
        else:
            force = bool(data.get("force")) and can_takeover
            out = _st.claim(conv_id, staff, ttl, force=force)
        if out.get("ok"):
            try:
                import json as _js
                inbox_broadcast(_js.dumps({"convId": conv_id}, ensure_ascii=False), "claim")
            except Exception:
                pass
            return jsonify({"success": True, **out})
        return jsonify({"success": False, **out}), 409
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/read", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_read():
    try:
        from features.inbox import store as _st
        data = request.get_json(silent=True) or request.form or {}
        conv_id = str(data.get("convId") or data.get("conv_id") or "").strip()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or "nv1"
        msg_id = str(data.get("msgId") or data.get("msg_id") or "").strip()
        out = _st.mark_read(conv_id, staff, msg_id)
        if out.get("ok"):
            try:
                import json as _js
                inbox_broadcast(_js.dumps({
                    "convId": conv_id,
                    "staff": staff,
                    "msgId": out.get("msgId", ""),
                    "at": out.get("at", 0),
                }, ensure_ascii=False), "read")
            except Exception:
                pass
        return jsonify({"success": bool(out.get("ok")), **out})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/presence", methods=["GET", "POST"])
@gateway_auth.gateway_auth_required
def api_inbox_presence():
    try:
        from features.inbox import store as _st
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form or {}
            is_host = gateway_auth.is_host_request()
            staff, locked, err = _resolve_client_staff()
            if locked and not staff:
                return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
            staff = staff or ("admin" if is_host else "nv1")
            status = data.get("status")
            conv_id = data.get("convId") if "convId" in data else None
            conv_name = data.get("convName") if "convName" in data else None
            action = data.get("action")
            _st.heartbeat(staff, status=status, conv_id=conv_id, conv_name=conv_name, action=action)
        return jsonify({
            "success": True,
            "online": _st.online_staff(),
            "presenceDetails": _st.presence_details(),
            "users": _st.users_list(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/supervisor", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_inbox_supervisor():
    """Bảng giám sát real-time đội ngũ Sale + Admin cho Super Admin và Admin."""
    try:
        from features.inbox import store as _st
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("Super Admin" if is_host else "nv1")
        scope = _inbox_scope_for_staff(staff, is_host)
        if not scope["canSupervise"]:
            return jsonify({"success": False, "error": "Chỉ Super Admin / Admin mới được xem bảng giám sát."}), 403
        overview = _st.supervisor_overview(
            profiles=gateway_config.get_staff_profiles(),
            accounts=scope["allAccounts"],
        )
        return jsonify({"success": True, "viewerRole": scope["role"], "isHost": is_host, **overview})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/supervisor/action", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_supervisor_action():
    """Thao tác quản trị nhân sự: lock, unlock, ban, unban, release_claims, remove."""
    try:
        from features.inbox import store as _st
        is_host = gateway_auth.is_host_request()
        staff, locked, err = _resolve_client_staff()
        if locked and not staff:
            return jsonify({"success": False, "staffLinkRequired": True, "error": err}), 403
        staff = staff or ("Super Admin" if is_host else "nv1")
        scope = _inbox_scope_for_staff(staff, is_host)
        if not scope["canSupervise"]:
            return jsonify({"success": False, "error": "Chỉ Super Admin / Admin mới được thực hiện thao tác này."}), 403

        data = request.get_json(silent=True) or {}
        target = str(data.get("target") or data.get("staff") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        if not target or not action:
            return jsonify({"success": False, "error": "Thiếu tên nhân sự hoặc thao tác."}), 400
        if target.lower() == "super admin":
            return jsonify({"success": False, "error": "Không thể thao tác lên Super Admin."}), 403
        if target.lower() == staff.lower() and action in ("lock", "ban", "remove"):
            return jsonify({"success": False, "error": "Không thể tự khóa/cấm/xóa chính mình."}), 400

        target_role = _st.staff_role(target)
        if not is_host and target_role in ("super_admin", "admin", "manager"):
            return jsonify({"success": False, "error": "Chỉ Super Admin (máy chính) mới có quyền khóa/cấm/xóa Admin."}), 403

        if action == "remove":
            cur_profiles = gateway_config.get_staff_profiles()
            new_profiles = [p for p in cur_profiles if str(p.get("name") or "").strip().lower() != target.lower()]
            gateway_config.save_staff_profiles(new_profiles)
            released = _st.release_all_claims(target, actor=staff)
            _st.audit("remove_staff", staff, "", f"hủy quyền & xóa {target}")
            out = {"ok": True, "target": target, "action": "remove", "releasedClaims": released}
        else:
            out = _st.moderate_staff(target, action, actor=staff)
            if not out.get("ok"):
                return jsonify({"success": False, **out}), 400

        if out.get("releasedClaims"):
            try:
                import json as _js
                inbox_broadcast(_js.dumps({"convId": ""}, ensure_ascii=False), "claim")
            except Exception:
                pass
        return jsonify({"success": True, **out})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/audit", methods=["GET"])
@gateway_auth.gateway_auth_required
def api_inbox_audit():
    try:
        from features.inbox import store as _st
        limit = int(request.args.get("limit") or 100)
        return jsonify({"success": True, "logs": _st.list_audit(limit)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/config", methods=["GET", "POST"])
@gateway_auth.gateway_auth_required
def api_inbox_config():
    try:
        from features.inbox import store as _st
        if request.method == "GET":
            cfg = _st.get_config()
            try:
                from features.accounts.account_manager import load_accounts as _la
                accs = [{"accountId": str(a.get("accountId") or ""),
                         "name": str(a.get("name") or a.get("accountId") or ""),
                         "avatarUrl": str(a.get("avatarUrl") or a.get("avatar") or "")}
                        for a in (_la() or [])]
            except Exception:
                accs = []
            return jsonify({"success": True, "config": cfg, "accounts": accs})
        data = request.get_json(silent=True) or request.form or {}
        patch = {}
        if "accountId" in data or "account_id" in data:
            patch["accountId"] = str(data.get("accountId") or data.get("account_id") or "").strip()
        if "onlyGroupIds" in data or "only_group_ids" in data:
            patch["onlyGroupIds"] = data.get("onlyGroupIds", data.get("only_group_ids"))
        if "onlyFriendIds" in data or "only_friend_ids" in data:
            patch["onlyFriendIds"] = data.get("onlyFriendIds", data.get("only_friend_ids"))
        if "hideEmpty" in data or "hide_empty" in data:
            v = data.get("hideEmpty", data.get("hide_empty"))
            patch["hideEmpty"] = v in (True, 1, "1", "true", "True", "on") if isinstance(v, str) else bool(v)
        cfg = _st.save_config(patch)
        pruned = 0
        try:
            from features.inbox import worker as _iw
            _iw.ensure_started()
            groups, friends = _iw._sync_directory(str(cfg.get("accountId") or ""), force=True)
            pruned = _st.prune_stale_conversations(
                str(cfg.get("accountId") or ""),
                [g.get("groupId") for g in groups],
                [f.get("userId") for f in friends])
        except Exception:
            pass
        return jsonify({"success": True, "config": cfg, "pruned": pruned})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/prune", methods=["POST"])
@gateway_auth.gateway_auth_required
def api_inbox_prune():
    try:
        from features.inbox import store as _st
        from features.inbox import worker as _iw
        data = request.get_json(silent=True) or request.form or {}
        account_id = str(data.get("accountId") or data.get("account_id") or "").strip() \
            or str(_st.get_config().get("accountId") or "").strip() or _iw._pick_account()
        groups, friends = _iw._sync_directory(account_id, force=True)
        removed = _st.prune_stale_conversations(
            account_id, [g.get("groupId") for g in groups], [f.get("userId") for f in friends])
        return jsonify({"success": True, "accountId": account_id, "removed": removed,
                        "totalGroups": len(groups), "totalFriends": len(friends)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/inbox/stream")
@gateway_auth.gateway_auth_required
def api_inbox_stream():
    client_q = queue.Queue()
    _inbox_sse_clients.append(client_q)

    def event_stream():
        try:
            yield f"data: {json.dumps({'kind': 'hello'}, ensure_ascii=False)}\n\n"
        except Exception:
            pass
        try:
            while True:
                try:
                    msg = client_q.get(timeout=45)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'kind': 'heartbeat'}, ensure_ascii=False)}\n\n"
        finally:
            # Client disconnect -> phai GO khoi danh sach, neu khong se
            # RO RON memory va moi lan broadcast deu lai loop qua client
            # da dong (ton CPU + queue).
            try:
                _inbox_sse_clients.remove(client_q)
            except Exception:
                pass

    resp = Response(event_stream(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route("/media/<token>", methods=["GET"])
def inbox_media_file(token):
    try:
        from flask import send_file as _send
        from features.inbox import media as _md
        rec = _md.resolve(token)
        if not rec:
            return jsonify({"success": False, "error": "Link het han hoac khong ton tai."}), 404
        return _send(rec["path"], as_attachment=False, download_name=rec.get("name") or "file")
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/log-stream")
def log_stream():
    """SSE endpoint: stream log messages to UI in real-time."""
    client_q = queue.Queue()
    _sse_clients.append(client_q)

    def event_stream():
        try:
            while True:
                try:
                    msg = client_q.get(timeout=60)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'msg':'','type':'heartbeat'}, ensure_ascii=False)}\n\n"
        finally:
            # Client disconnect -> go khoi danh sach (tranh ro memory + loop
            # broadcast qua client da dong).
            try:
                _sse_clients.remove(client_q)
            except Exception:
                pass

    resp = Response(event_stream(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp




def _cookie_has_name(cookie_str: str, cookie_name: str) -> bool:
    target = str(cookie_name or "").strip().lower()
    if not target:
        return False
    for item in str(cookie_str or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        if name.strip().lower() == target and value.strip():
            return True
    return False


def _refresh_account_cookies_if_possible(account_id: str, account: dict, timeout: float = 7.0) -> bool:
    port = (account or {}).get("remoteDebugPort")
    if not port:
        return False
    try:
        from features.accounts.account_network_monitor import refresh_account_cookies
        return bool(refresh_account_cookies(account_id, int(port), timeout=timeout))
    except Exception as error:
        print(f"[session_refresh] Không đọc lại được cookies account={account_id}: {error}")
        return False


def _is_zalo_session_error(error) -> bool:
    message = str(error or "").lower()
    return any(marker in message for marker in (
        "zpw_sek",
        "zpw enk",
        "zpwenk",
        "cookie", 
        "phiên đăng nhập",
        "session",
    ))


def _refresh_account_session_blocking(account_id: str, timeout: float = 18.0) -> bool:
    """Tự bắt lại zpwEnk + zpw_sek từ Chrome đang mở và chờ tối đa timeout.

    Chỉ dùng sau khi API Zalo xác nhận session hiện tại không hợp lệ.
    """
    account = get_account(account_id)
    if not account:
        return False
    port = account.get("remoteDebugPort")
    if not port:
        return False

    try:
        if not _chrome_debug_port_alive(port):
            return False
    except Exception:
        return False

    refresh_started_at = int(time.time() * 1000)
    # Không xóa cookies/zpwEnk đang lưu. Monitor sẽ thay thế từng giá trị sau
    # khi bắt được bộ phiên mới hợp lệ; nếu quá trình bắt lại thất bại, dữ liệu
    # cũ vẫn còn để người dùng không bị mất phiên vì một lần bấm Làm mới.
    update_account(
        account_id,
        loginCaptured=False,
        sessionRefreshStartedAt=refresh_started_at,
    )

    try:
        from features.accounts.account_network_monitor import start_account_network_monitor
        start_account_network_monitor(account_id, int(port))
    except Exception as error:
        print(f"[session_refresh] Không khởi động được monitor account={account_id}: {error}")
        return False

    deadline = time.time() + max(5.0, float(timeout or 0))
    while time.time() < deadline:
        current = get_account(account_id) or {}
        cookies = current.get("cookies") or ""
        captured_at = int(current.get("sessionCapturedAt") or 0)
        if (
            current.get("loginCaptured")
            and str(current.get("zpwEnk") or "").strip()
            and _cookie_has_name(cookies, "zpw_sek")
            and captured_at >= refresh_started_at
        ):
            print(f"[session_refresh] Đã khôi phục session account={account_id}")
            return True
        time.sleep(0.35)

    print(f"[session_refresh] Hết thời gian chờ session account={account_id}")
    return False


def _get_account_credentials(account_id: str, require_imei: bool = False):
    """Lấy bộ session đồng nhất của tài khoản đang chọn.

    getmg cần cả zpwEnk và cookie zpw_sek cùng một phiên. Cookie dài hoặc có
    zpsid/__zi nhưng thiếu zpw_sek vẫn không dùng được.
    """
    account_id = (account_id or "").strip()
    if not account_id:
        raise ValueError("Chưa chọn tài khoản.")

    account = get_account(account_id)
    if not account:
        raise ValueError("Không tìm thấy tài khoản.")

    cookies = (account.get("cookies") or "").strip()
    if not _cookie_has_name(cookies, "zpw_sek"):
        # Tự đọc lại cookie HttpOnly từ Chrome đang mở trước khi báo lỗi cho UI.
        _refresh_account_cookies_if_possible(account_id, account)
        account = get_account(account_id) or account
        cookies = (account.get("cookies") or "").strip()

    zpw_enk = (account.get("zpwEnk") or "").strip()
    imei = (account.get("imei") or "").strip()

    missing = []
    if not account.get("loginCaptured"):
        missing.append("phiên đăng nhập chưa hoàn tất")
    if not zpw_enk:
        missing.append("zpwEnk")
    if not _cookie_has_name(cookies, "zpw_sek"):
        missing.append("cookie zpw_sek")
    if require_imei and not imei:
        missing.append("imei")

    if missing:
        raise ValueError(
            "Tài khoản chưa đủ phiên xác thực: " + ", ".join(missing) +
            ". Hãy mở lại tài khoản Zalo, chờ trạng thái Sẵn sàng rồi thao tác lại."
        )

    return account, cookies, zpw_enk, imei


def _emit_session_status(callback, message: str, kind: str = "loading"):
    if not callback:
        return
    try:
        callback(message, kind)
    except TypeError:
        callback(message)
    except Exception:
        pass


def _get_account_friend_uids(account_id: str, account: dict = None, cookies: str = "", zpw_enk: str = "", imei: str = "") -> set:
    """Trả về tập hợp UID bạn bè đã biết của tài khoản (từ cache danh bạ inbox hoặc tải an toàn)."""
    friend_uids = set()
    account_id = str(account_id or "").strip()
    if not account_id:
        return friend_uids
    try:
        from features.inbox.store import get_directory
        d = get_directory(account_id) or {}
        for f in (d.get("friends") or []):
            fuid = str(f.get("userId") or f.get("uid") or "").strip()
            if fuid:
                friend_uids.add(fuid)
    except Exception:
        pass

    if not friend_uids:
        try:
            from features.inbox.worker import _friend_cache_by_acc
            cached = _friend_cache_by_acc.get(account_id) or {}
            for f in (cached.get("friends") or []):
                fuid = str(f.get("userId") or f.get("uid") or "").strip()
                if fuid:
                    friend_uids.add(fuid)
        except Exception:
            pass

    if not friend_uids:
        try:
            from features.inbox.worker import _load_friends_safe
            acc = account or get_account(account_id) or {}
            flist = _load_friends_safe(acc, force=False) or []
            for f in flist:
                fuid = str(f.get("userId") or f.get("uid") or "").strip()
                if fuid:
                    friend_uids.add(fuid)
        except Exception:
            pass
    return friend_uids


def _fetch_group_members_session_safe(
    account_id: str,
    group_input: str,
    callback=None,
    require_imei: bool = False,
    auto_join_when_not_member: bool = True,
    leave_after_auto_join: bool = True,
):
    """Gọi getmg với một lần tự khôi phục session khi zpw_sek/zpwEnk lệch phiên."""
    account, cookies, zpw_enk, imei = _get_account_credentials(
        account_id, require_imei=require_imei
    )
    zpw_ver = get_zpw_ver()

    for attempt in range(2):
        try:
            payload = fetch_group_members_by_input(
                group_input,
                zpw_enk,
                cookies,
                callback=callback,
                zpw_ver=zpw_ver,
                imei=imei,
                auto_join_when_not_member=auto_join_when_not_member,
                leave_after_auto_join=leave_after_auto_join,
            )
            return payload, account, cookies, zpw_enk, imei, zpw_ver
        except ValueError as error:
            if attempt > 0 or not _is_zalo_session_error(error):
                raise
            _emit_session_status(
                callback,
                "Phiên Zalo chưa đồng nhất. Đang bắt lại zpwEnk và zpw_sek...",
                "loading",
            )
            if not _refresh_account_session_blocking(account_id):
                raise ValueError(
                    "Phiên Zalo đã hết hạn hoặc thiếu zpw_sek. Hãy mở tài khoản Zalo, "
                    "đợi trang tải xong rồi thao tác lại."
                ) from error
            account, cookies, zpw_enk, imei = _get_account_credentials(
                account_id, require_imei=require_imei
            )
            zpw_ver = get_zpw_ver()

    raise ValueError("Không thể khôi phục phiên Zalo.")


@app.route("/run", methods=["POST"])
def run():
    account_id = request.form.get("account_id", "").strip()

    raw = request.form.get("group_link", "").strip()
    if not raw:
        raw = request.form.get("group_id", "").strip()

    # Tự động fallback: nếu account chưa là thành viên và input là link nhóm,
    # hệ thống sẽ tự join ngầm, lấy thành viên, rồi rời nhóm. Không cần checkbox UI.
    auto_join_when_not_member = True

    if not raw:
        return jsonify({"error": "Thiếu Group Link hoặc Group ID!"}), 400

    try:
        member_payload, account, cookies, zpw_enk, imei, zpw_ver = _fetch_group_members_session_safe(
            account_id,
            raw,
            callback=sse_broadcast,
            require_imei=False,
            auto_join_when_not_member=auto_join_when_not_member,
            leave_after_auto_join=False,
        )

        uid_list = member_payload["uidList"]
        group_info = member_payload["groupInfo"]
        group_id = member_payload["groupId"]
        member_map = member_payload.get("memberMap") or {}

        sse_broadcast(f"Lấy được {len(uid_list)} UID. Đang lấy thông tin profile...", "loading")
        auto_joined = bool(member_payload.get("autoJoined"))
        auto_left = False
        leave_result = None

        try:
            profiles = fetch_profiles_with_single_fallback(
                uid_list,
                zpw_enk,
                cookies,
                imei=imei,
                log_func=sse_broadcast,
                zpw_ver=zpw_ver,
            )
            own_uid = str((account or {}).get("userId") or (account or {}).get("uid") or "").strip()
            friend_uids = _get_account_friend_uids(account_id, account=account, cookies=cookies, zpw_enk=zpw_enk, imei=imei)
            result = build_member_rows_from_uids(
                uid_list,
                profiles,
                member_map=member_map,
                group_info=group_info,
                own_uid=own_uid,
                friend_uids=friend_uids,
            )
        finally:
            # Nếu hệ thống tự join ngầm thì LUÔN rời nhóm sau khi đã có UID,
            # kể cả khi lấy profile hoặc build bảng lỗi, để account không kẹt
            # lại trong nhóm. Rời sau bước profile để mini profile lấy đủ dữ liệu.
            if auto_joined and uid_list:
                try:
                    leave_result = _leave_group([group_id], imei=imei, zpw_enk=zpw_enk, cookies=cookies, zpw_ver=zpw_ver)
                    auto_left = bool(isinstance(leave_result, dict) and leave_result.get("ok"))
                except Exception as leave_error:
                    sse_broadcast(f"Không rời được nhóm đã tự tham gia: {leave_error}", "warn")

        sse_broadcast(f"Hoàn thành! Tổng cộng {len(result)} thành viên.", "success")
        return jsonify({
            "success": True,
            "total": len(result),
            "data": result,
            "groupInfo": format_group_info(group_info, group_id=group_id, fallback_total=len(result)),
            "autoJoined": auto_joined,
            "autoLeft": auto_left,
            "leaveResult": leave_result,
        })

    except ValueError as e:
        sse_broadcast(str(e), "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        sse_broadcast(f"Lỗi: {str(e)}", "error")
        return jsonify({"error": str(e)}), 500



@app.route("/api/get-avatar", methods=["POST"])
def api_get_avatar():
    """
    API: Lấy avatar full size từ Zalo.
    Gọi features/profiles/get_avata.py -> get_avatar()
    Trả về bk_full_avatar để frontend hiển thị khi click avatar.
    """
    data = request.get_json(silent=True) or {}

    account_id = str(
        data.get("accountId")
        or data.get("account_id")
        or ""
    ).strip()

    fid = str(
        data.get("fid")
        or data.get("uid")
        or data.get("userId")
        or data.get("toid")
        or ""
    ).strip()

    # Fix nếu UI đang hiển thị dạng "ID: 439..."
    if fid.lower().startswith("id:"):
        fid = fid.split(":", 1)[1].strip()

    import re
    m = re.search(r"\d{8,}", fid)
    if m:
        fid = m.group(0)

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản."}), 400

    if not fid:
        return jsonify({"success": False, "error": "Thiếu fid/userId."}), 400

    try:
        from features.profiles.get_avata import get_avatar

        account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        print("[api_get_avatar] call features.profiles.get_avata.get_avatar")
        print("[api_get_avatar] account_id=", account_id)
        print("[api_get_avatar] fid=", fid)
        print("[api_get_avatar] has_cookies=", bool(cookies))
        print("[api_get_avatar] has_zpw_enk=", bool(zpw_enk))
        print("[api_get_avatar] imei=", imei)

        response_json, decoded_data, avatar_data = get_avatar(
            fid=fid,
            imei=imei,
            zpw_enk=zpw_enk,
            cookies=cookies,
            proxy=None,
            zpw_ver=get_zpw_ver()
        )

        bk_full_avatar = ""
        full_avatar = ""

        if isinstance(avatar_data, dict):
            bk_full_avatar = avatar_data.get("bk_full_avatar") or ""
            full_avatar = avatar_data.get("full_avatar") or ""

        avatar_url = bk_full_avatar or full_avatar

        print("[api_get_avatar] bk_full_avatar=", bk_full_avatar)
        print("[api_get_avatar] full_avatar=", full_avatar)

        if not avatar_url:
            return jsonify({
                "success": False,
                "error": "Không lấy được bk_full_avatar.",
                "response": response_json,
                "decoded": decoded_data,
                "avatar_data": avatar_data
            }), 400

        return jsonify({
            "success": True,
            "fid": fid,
            "bk_full_avatar": bk_full_avatar,
            "full_avatar": full_avatar,
            "avatar_url": avatar_url,
            "data": avatar_data,
            "decoded": decoded_data
        })

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/single-profile", methods=["POST"])
def api_single_profile():
    """API: Lấy profile đầy đủ của 1 UID hoặc nhiều UID qua getprofiles/v2."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400

    account_id = str(data.get("accountId", data.get("account_id", "")) or "").strip()
    uid = str(data.get("uid", "") or "").strip()
    uids = data.get("uids") or data.get("uid_list") or []

    # Cho phép frontend gửi {uid: "..."} hoặc {uids: ["...", "..."]}
    if uid:
        uid_input = uid
        is_batch = False
    else:
        if isinstance(uids, str):
            uids = [x.strip() for x in uids.split(",") if x.strip()]
        elif isinstance(uids, (list, tuple)):
            uids = [str(x).strip() for x in uids if str(x).strip()]
        else:
            uids = []

        if not uids:
            return jsonify({"error": "Thiếu UID hoặc danh sách UIDs!"}), 400

        uid_input = uids
        is_batch = True

    try:
        account = get_account(account_id) or {}
        own_uid = str(account.get("userId") or account.get("uid") or "").strip()
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
        result = get_single_profile(uid_input, zpw_enk, cookies, imei=imei, zpw_ver=get_zpw_ver(), raise_on_rate_limit=True)
        friend_uids = _get_account_friend_uids(account_id, account=account, cookies=cookies, zpw_enk=zpw_enk, imei=imei)

        if is_batch:
            # result dạng {uid: profile hoặc None}
            profiles = result if isinstance(result, dict) else {}
            profiles = {str(k): v for k, v in profiles.items() if v}
            for r_uid, r_prof in profiles.items():
                if not isinstance(r_prof, dict):
                    continue
                r_uid_str = str(r_uid).strip()
                if own_uid and r_uid_str == own_uid:
                    r_prof["isSelf"] = True
                    r_prof["isFriend"] = False
                    r_prof["isFr"] = 0
                elif r_uid_str in friend_uids or r_prof.get("isFr") == 1 or str(r_prof.get("isFr")) == "1":
                    r_prof["isSelf"] = False
                    r_prof["isFriend"] = True
                    r_prof["isFr"] = 1
                else:
                    r_prof["isSelf"] = False
                    r_prof["isFriend"] = False
                    r_prof["isFr"] = 0
            return jsonify({
                "success": True,
                "profiles": profiles,
                "total": len(profiles)
            })

        if not result:
            return jsonify({"error": "Không lấy được profile của UID này."}), 400
        if isinstance(result, dict):
            s_uid_str = str(result.get("userId") or uid_input).strip()
            if own_uid and s_uid_str == own_uid:
                result["isSelf"] = True
                result["isFriend"] = False
                result["isFr"] = 0
            elif s_uid_str in friend_uids or result.get("isFr") == 1 or str(result.get("isFr")) == "1":
                result["isSelf"] = False
                result["isFriend"] = True
                result["isFr"] = 1
            else:
                result["isSelf"] = False
                result["isFriend"] = False
                result["isFr"] = 0
        return jsonify({"success": True, "profile": result})
    except ProfileRateLimitError as e:
        return jsonify({"error": str(e) or "Zalo đang giới hạn request profile. Vui lòng chờ rồi thử lại.", "error_code": getattr(e, "code", 221)}), 429
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/friend-request-plans", methods=["GET"])
def api_list_friend_request_plans():
    """API: danh sách kế hoạch kết bạn/mời nhóm đã lưu."""
    try:
        account_id = str(request.args.get("accountId") or request.args.get("account_id") or "").strip()
        status = str(request.args.get("status") or "").strip()
        plans = _load_friend_plans()
        if account_id:
            plans = [p for p in plans if str(p.get("accountId") or "") == account_id]
        plans = _attach_action_plans_progress(plans)
        if status:
            plans = [p for p in plans if str(p.get("status") or "") == status or str((p.get("progress") or {}).get("displayStatus") or "") == status]
        plans = sorted(plans, key=lambda x: int(x.get("createdAt") or 0), reverse=True)
        return jsonify({"success": True, "plans": plans, "total": len(plans)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "plans": []}), 500


@app.route("/api/friend-request-plans", methods=["POST"])
def api_create_friend_request_plan():
    """
    API: tạo kế hoạch chia batch theo ngày cho danh sách người được chọn.
    Lưu ý: route này chỉ lưu kế hoạch pending_manual, không tự động gửi lời mời/kéo người vào nhóm.
    """
    data = request.get_json(silent=True) or {}

    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    message = str(data.get("message") or data.get("msg") or "").strip()
    plan_name = str(data.get("name") or data.get("planName") or "Kế hoạch kết bạn").strip()
    start_date = str(data.get("startDate") or "").strip() or datetime.now().date().isoformat()
    after_action = str(data.get("afterAction") or "none").strip()
    target_group_ids = data.get("targetGroupIds") or data.get("groupIds") or []
    target_groups = data.get("targetGroups") or []
    new_group_name = str(data.get("newGroupName") or "").strip()
    confirm_consent = bool(data.get("confirmConsent") or data.get("consentConfirmed"))

    try:
        daily_limit = int(data.get("dailyLimit") or data.get("perDay") or 1)
    except Exception:
        daily_limit = 1
    daily_limit = max(1, min(daily_limit, 500))

    if isinstance(target_group_ids, str):
        target_group_ids = [target_group_ids]
    target_group_ids = [str(x).strip() for x in target_group_ids if str(x).strip()]

    members_raw = data.get("members") or data.get("selectedMembers") or data.get("userIds") or data.get("uids") or []
    if isinstance(members_raw, str):
        members_raw = [x.strip() for x in members_raw.split(",") if x.strip()]

    seen = set()
    members = []
    for item in members_raw if isinstance(members_raw, list) else []:
        m = _normalize_plan_member(item)
        if not m:
            continue
        uid = m["userId"]
        if uid in seen:
            continue
        seen.add(uid)
        members.append(m)

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản lập lịch."}), 400
    if not get_account(account_id):
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    if not members:
        return jsonify({"success": False, "error": "Danh sách người được chọn đang trống."}), 400
    if not message:
        return jsonify({"success": False, "error": "Vui lòng nhập nội dung lời mời kết bạn."}), 400
    if not confirm_consent:
        return jsonify({
            "success": False,
            "error": "Cần xác nhận danh sách người nhận hợp lệ/được phép liên hệ trước khi lưu kế hoạch."
        }), 400

    if after_action not in ("none", "invite_existing_group", "create_new_group"):
        after_action = "none"
    if after_action == "invite_existing_group" and not target_group_ids:
        return jsonify({"success": False, "error": "Vui lòng chọn nhóm đích."}), 400
    if after_action == "create_new_group" and not new_group_name:
        return jsonify({"success": False, "error": "Vui lòng nhập tên nhóm mới."}), 400

    batches = _build_friend_plan_batches(members, daily_limit, start_date)
    now = _friend_now_ms()
    plan = {
        "id": uuid.uuid4().hex,
        "name": plan_name,
        "status": "planned",
        "mode": "manual_review",
        "accountId": account_id,
        "accountName": _find_account_name(account_id),
        "message": message,
        "dailyLimit": daily_limit,
        "startDate": start_date,
        "createdAt": now,
        "updatedAt": now,
        "totalMembers": len(members),
        "afterAction": after_action,
        "targetGroupIds": target_group_ids,
        "targetGroups": target_groups if isinstance(target_groups, list) else [],
        "newGroupName": new_group_name,
        "consentConfirmed": True,
        "members": members,
        "batches": batches,
        "note": "Kế hoạch chỉ chia lịch và chờ xác nhận thủ công từng batch; hệ thống không tự động gửi hàng loạt.",
    }

    _attach_action_plan_progress(plan)
    plans = _load_friend_plans()
    plans.append(plan)
    _save_friend_plans(plans)

    return jsonify({
        "success": True,
        "message": f"Đã lưu kế hoạch {len(members)} người, {daily_limit} người/ngày.",
        "plan": plan,
    })


@app.route("/api/group-invite-plans", methods=["GET"])
def api_list_group_invite_plans():
    """API: danh sách kế hoạch mời vào nhóm đã lưu."""
    try:
        account_id = str(request.args.get("accountId") or request.args.get("account_id") or "").strip()
        status = str(request.args.get("status") or "").strip()
        plans = _load_group_invite_plans()
        if account_id:
            plans = [p for p in plans if str(p.get("accountId") or "") == account_id]
        plans = _attach_action_plans_progress(plans)
        if status:
            plans = [p for p in plans if str(p.get("status") or "") == status or str((p.get("progress") or {}).get("displayStatus") or "") == status]
        plans = sorted(plans, key=lambda x: int(x.get("createdAt") or 0), reverse=True)
        return jsonify({"success": True, "plans": plans, "total": len(plans)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "plans": []}), 500


@app.route("/api/group-invite-plans", methods=["POST"])
def api_create_group_invite_plan():
    """
    API: tạo kế hoạch chia batch theo ngày để mời thành viên vào nhóm đã chọn.
    Lưu ý: route này chỉ lưu kế hoạch pending_manual, không tự động mời hàng loạt.
    """
    data = request.get_json(silent=True) or {}

    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    plan_name = str(data.get("name") or data.get("planName") or "Kế hoạch mời vào nhóm").strip()
    start_date = str(data.get("startDate") or "").strip() or datetime.now().date().isoformat()
    confirm_consent = bool(data.get("confirmConsent") or data.get("consentConfirmed"))

    try:
        daily_limit = int(data.get("dailyLimit") or data.get("perDay") or 1)
    except Exception:
        daily_limit = 1
    daily_limit = max(1, min(daily_limit, 500))

    members_raw = data.get("members") or data.get("selectedMembers") or data.get("userIds") or data.get("uids") or []
    if isinstance(members_raw, str):
        members_raw = [x.strip() for x in members_raw.split(",") if x.strip()]

    seen_members = set()
    members = []
    for item in members_raw if isinstance(members_raw, list) else []:
        m = _normalize_plan_member(item)
        if not m:
            continue
        uid = m["userId"]
        if uid in seen_members:
            continue
        seen_members.add(uid)
        members.append(m)

    target_group_ids = data.get("targetGroupIds") or data.get("groupIds") or []
    if isinstance(target_group_ids, str):
        target_group_ids = [target_group_ids]
    target_group_ids = [str(x).strip() for x in target_group_ids if str(x).strip()]

    groups_raw = data.get("targetGroups") or data.get("groups") or []
    groups = []
    seen_groups = set()
    for item in groups_raw if isinstance(groups_raw, list) else []:
        g = _normalize_plan_group(item)
        if not g:
            continue
        gid = g["groupId"]
        if gid in seen_groups:
            continue
        seen_groups.add(gid)
        groups.append(g)

    # Nếu frontend chỉ gửi ID thì vẫn lưu được metadata tối thiểu. Đúng là máy móc cũng cần được nhắc cách thở.
    for gid in target_group_ids:
        if gid not in seen_groups:
            groups.append({"groupId": gid, "name": gid, "avatar": "", "memberCount": 0})
            seen_groups.add(gid)

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản lập lịch."}), 400
    if not get_account(account_id):
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    if not members:
        return jsonify({"success": False, "error": "Danh sách người cần mời đang trống."}), 400
    if not target_group_ids:
        return jsonify({"success": False, "error": "Vui lòng chọn ít nhất 1 nhóm đích."}), 400
    if not confirm_consent:
        return jsonify({
            "success": False,
            "error": "Cần xác nhận danh sách người nhận hợp lệ/được phép mời trước khi lưu kế hoạch."
        }), 400

    batches = _build_friend_plan_batches(members, daily_limit, start_date)
    now = _friend_now_ms()
    plan = {
        "id": uuid.uuid4().hex,
        "name": plan_name,
        "status": "planned",
        "mode": "manual_review",
        "accountId": account_id,
        "accountName": _find_account_name(account_id),
        "dailyLimit": daily_limit,
        "startDate": start_date,
        "createdAt": now,
        "updatedAt": now,
        "totalMembers": len(members),
        "targetGroupIds": target_group_ids,
        "targetGroups": groups,
        "consentConfirmed": True,
        "members": members,
        "batches": batches,
        "note": "Kế hoạch chỉ chia lịch và chờ xác nhận thủ công từng batch; hệ thống không tự động mời hàng loạt.",
    }

    _attach_action_plan_progress(plan)
    plans = _load_group_invite_plans()
    plans.append(plan)
    _save_group_invite_plans(plans)

    return jsonify({
        "success": True,
        "message": f"Đã lưu kế hoạch mời {len(members)} người vào {len(target_group_ids)} nhóm, {daily_limit} người/ngày.",
        "plan": plan,
    })


@app.route("/api/friend-request-plans/<plan_id>/batches/<int:batch_day>", methods=["PATCH", "POST"])
def api_update_friend_request_plan_batch(plan_id, batch_day):
    """Cập nhật tiến độ một batch trong kế hoạch gửi kết bạn."""
    data = request.get_json(silent=True) or {}
    try:
        plan, batch = _update_action_plan_batch_status("friend", plan_id, batch_day, data)
        return jsonify({
            "success": True,
            "message": "Đã cập nhật tiến độ batch kết bạn.",
            "plan": plan,
            "batch": batch,
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/group-invite-plans/<plan_id>/batches/<int:batch_day>", methods=["PATCH", "POST"])
def api_update_group_invite_plan_batch(plan_id, batch_day):
    """Cập nhật tiến độ một batch trong kế hoạch mời vào nhóm."""
    data = request.get_json(silent=True) or {}
    try:
        plan, batch = _update_action_plan_batch_status("group_invite", plan_id, batch_day, data)
        return jsonify({
            "success": True,
            "message": "Đã cập nhật tiến độ batch mời vào nhóm.",
            "plan": plan,
            "batch": batch,
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/action-plans", methods=["GET"])
def api_list_action_plans():
    """API tổng hợp để UI xem lịch kết bạn và lịch mời vào nhóm trong một màn."""
    try:
        account_id = str(request.args.get("accountId") or request.args.get("account_id") or "").strip()
        plan_type = str(request.args.get("type") or "all").strip()

        items = []
        if plan_type in ("all", "friend", "friend_request"):
            friend_plans = _attach_action_plans_progress(_load_friend_plans())
            for p in friend_plans:
                p = dict(p)
                p["planType"] = "friend"
                p["planTypeLabel"] = "Gửi kết bạn"
                items.append(p)

        if plan_type in ("all", "group", "group_invite"):
            group_plans = _attach_action_plans_progress(_load_group_invite_plans())
            for p in group_plans:
                p = dict(p)
                p["planType"] = "group_invite"
                p["planTypeLabel"] = "Mời vào nhóm"
                items.append(p)

        if account_id:
            items = [p for p in items if str(p.get("accountId") or "") == account_id]

        items = sorted(items, key=lambda x: int(x.get("createdAt") or 0), reverse=True)
        return jsonify({"success": True, "plans": items, "total": len(items)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "plans": []}), 500

@app.route("/api/send-friend-request", methods=["POST"])
def api_send_friend_request():
    """
    API: Gửi lời mời kết bạn.
    Route này lấy account credentials rồi gọi trực tiếp features/messaging/add_friend.py -> send_friend_request().
    """
    data = request.get_json(silent=True) or {}

    account_id = str(data.get("accountId", data.get("account_id", "")) or "").strip()
    toid = str(
        data.get("toid",
        data.get("toId",
        data.get("uid",
        data.get("userId",
        data.get("to_uid", "")))))
        or ""
    ).strip()
    message = str(data.get("message", data.get("msg", "")) or "").strip()

    if not account_id:
        return jsonify({"error": "Chưa chọn tài khoản gửi kết bạn."}), 400

    if not toid:
        return jsonify({"error": "Thiếu ID người nhận kết bạn."}), 400

    if not message:
        message = "Xin chào, mình muốn kết bạn với bạn."

    try:
        account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        proxy = None  # Không dùng proxy khi gửi kết bạn

        print("[api_send_friend_request] call features.messaging.add_friend.send_friend_request")
        print("[api_send_friend_request] account_id=", account_id)
        print("[api_send_friend_request] toid=", toid)
        print("[api_send_friend_request] has_cookies=", bool(cookies))
        print("[api_send_friend_request] has_zpw_enk=", bool(zpw_enk))
        print("[api_send_friend_request] imei=", imei)
        print("[api_send_friend_request] proxy= DISABLED")

        response_json, decoded_data = send_friend_request(
            toid=toid,
            msg=message,
            imei=imei,
            zpw_enk=zpw_enk,
            cookies=cookies,
            proxy=None,
            zpw_ver=get_zpw_ver()
        )

        print("[api_send_friend_request] response_json=", response_json)
        print("[api_send_friend_request] decoded_data=", decoded_data)

        error_code = None
        error_message = ""

        if isinstance(decoded_data, dict):
            error_code = decoded_data.get("error_code", decoded_data.get("errorCode"))
            error_message = str(decoded_data.get("error_message", decoded_data.get("errorMessage", decoded_data.get("message", ""))) or "")
        elif isinstance(response_json, dict):
            error_code = response_json.get("error_code", response_json.get("errorCode"))
            error_message = str(response_json.get("error_message", response_json.get("errorMessage", response_json.get("message", ""))) or "")

        # Chỉ coi là thành công khi Zalo trả error_code = 0 rõ ràng.
        if str(error_code) == "0":
            return jsonify({
                "success": True,
                "message": "Zalo đã nhận request gửi kết bạn.",
                "data": decoded_data,
                "raw": response_json
            })

        return jsonify({
            "success": False,
            "error": "Zalo không xác nhận gửi kết bạn thành công.",
            "code": error_code,
            "message": error_message,
            "detail": decoded_data,
            "raw": response_json
        }), 400

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/send-sms", methods=["POST"])
def api_send_sms():
    """API: Gui tin nhan Zalo toi user ID theo account duoc chon."""
    data = request.get_json(silent=True) or {}

    account_id = str(data.get("accountId", data.get("account_id", "")) or "").strip()

    to_uid = str(
        data.get("to_uid",
        data.get("toid",
        data.get("toId",
        data.get("uid",
        data.get("userId", "")))))
        or ""
    ).strip()

    # Fix: frontend có thể gửi dạng "ID: 336411663026086078"
    # Zalo API chỉ nhận số UID, không nhận kèm chữ "ID:"
    if to_uid.lower().startswith("id:"):
        to_uid = to_uid.split(":", 1)[1].strip()

    # Lọc tiếp phòng trường hợp có text thừa
    import re
    m = re.search(r"\d{8,}", to_uid)
    if m:
        to_uid = m.group(0)

    message = str(data.get("message", data.get("msg", "")) or "").strip()

    print("[api_send_sms] data=", data)
    print("[api_send_sms] account_id=", account_id)
    print("[api_send_sms] to_uid=", to_uid)
    print("[api_send_sms] message=", message)

    if not account_id:
        return jsonify({"success": False, "error": "Chua chon tai khoan gui tin nhan."}), 400

    if not to_uid:
        return jsonify({"success": False, "error": "Thieu nguoi nhan."}), 400

    if not message:
        return jsonify({"success": False, "error": "Thieu noi dung tin nhan."}), 400

    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        print("[api_send_sms] has_cookies=", bool(cookies))
        print("[api_send_sms] has_zpw_enk=", bool(zpw_enk))
        print("[api_send_sms] imei=", imei)

        # Nội dung chứa link nhóm Zalo -> tự lấy thông tin nhóm rồi gửi link card
        # (message/link); không có link hoặc lỗi -> gửi SMS text như cũ.
        from features.messaging.send_link import send_message_smart
        result = send_message_smart(
            to_uid, message, zpw_enk, cookies, imei,
            is_group=False, zpw_ver=get_zpw_ver(),
        )

        print("[api_send_sms] result=", result)

        if result.get("ok"):
            return jsonify({
                "success": True,
                "message": "Da gui tin nhan." + (" (link card)" if result.get("sentAsLink") else ""),
                "sentAsLink": bool(result.get("sentAsLink")),
                "data": result.get("decoded"),
            })

        return jsonify({
            "success": False,
            "error": "Loi gui SMS tu Zalo.",
            "message": result.get("error", ""),
            "detail": result.get("decoded"),
        }), 400

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/send-photo", methods=["POST"])
def api_send_photo():
    """API: Gửi ảnh (kèm chú thích) tới user ID theo account được chọn.

    Luồng 2 bước theo Zalo Web: upload chunk qua photo_original/upload để lấy
    photoId + URL, sau đó POST photo_original/send để tạo tin nhắn ảnh.
    Nhận multipart/form-data: accountId, to_uid, message (tùy chọn), photo (file).
    """
    account_id = str(request.form.get("accountId", request.form.get("account_id", "")) or "").strip()
    to_uid = str(
        request.form.get("to_uid")
        or request.form.get("toid")
        or request.form.get("toId")
        or request.form.get("uid")
        or request.form.get("userId")
        or ""
    ).strip()
    message = str(request.form.get("message", request.form.get("msg", "")) or "").strip()

    if to_uid.lower().startswith("id:"):
        to_uid = to_uid.split(":", 1)[1].strip()
    import re
    m = re.search(r"\d{8,}", to_uid)
    if m:
        to_uid = m.group(0)

    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản gửi tin nhắn."}), 400
    if not to_uid:
        return jsonify({"success": False, "error": "Thiếu người nhận."}), 400
    if not photo_file:
        return jsonify({"success": False, "error": "Thiếu file ảnh cần gửi."}), 400

    image_bytes = photo_file.read()
    if not image_bytes:
        return jsonify({"success": False, "error": "File ảnh rỗng."}), 400
    if len(image_bytes) > 20 * 1024 * 1024:
        return jsonify({"success": False, "error": "Ảnh vượt quá 20MB."}), 400

    file_name = photo_file.filename or "image.jpg"

    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        from features.messaging.send_photo import send_photo as _send_photo
        # Gửi ảnh TRƯỚC (không caption), sau đó gửi text thành tin nhắn riêng.
        result = _send_photo(
            image_bytes,
            file_name,
            to_uid,
            zpw_enk,
            cookies,
            imei,
            desc="",
            is_group=False,
            zpw_ver=get_zpw_ver(),
        )

        if result.get("ok"):
            text_sent = False
            text_error = ""
            if message:
                try:
                    from features.messaging.send_link import send_message_smart
                    text_result = send_message_smart(
                        to_uid, message, zpw_enk, cookies, imei,
                        is_group=False, zpw_ver=get_zpw_ver(),
                    )
                    text_sent = bool(text_result.get("ok"))
                    if not text_sent:
                        text_error = f"Gửi text sau ảnh lỗi: {text_result.get('error')}"
                except Exception as text_exc:
                    text_error = f"Gửi text sau ảnh lỗi: {text_exc}"
            return jsonify({
                "success": True,
                "message": "Đã gửi ảnh." + (" Đã gửi tin nhắn." if text_sent else ""),
                "photoId": result.get("photoId"),
                "urls": result.get("urls"),
                "textSent": text_sent,
                "textError": text_error,
                "data": result.get("decoded"),
            })

        return jsonify({
            "success": False,
            "error": result.get("message", "Gửi ảnh thất bại."),
            "step": result.get("step"),
            "detail": result.get("decoded"),
            "raw": result.get("response"),
        }), 400

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/send-group-message", methods=["POST"])
def api_send_group_message():
    """API: Gửi tin nhắn vào nhóm Zalo, có thể kèm ảnh.

    Nhận multipart/form-data: accountId, group_id, message (tùy chọn nếu có ảnh),
    photo (file, tùy chọn). Có ảnh -> photo_original/upload + send (nhóm),
    không ảnh -> /api/group/sendmsg như cũ.
    """
    account_id = str(request.form.get("accountId", request.form.get("account_id", "")) or "").strip()
    group_id = str(
        request.form.get("group_id")
        or request.form.get("groupId")
        or request.form.get("grid")
        or ""
    ).strip()
    message = str(request.form.get("message", request.form.get("msg", "")) or "").strip()
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")

    import re
    m = re.search(r"\d{8,}", group_id)
    if m:
        group_id = m.group(0)

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản gửi tin nhắn."}), 400
    if not group_id:
        return jsonify({"success": False, "error": "Thiếu ID nhóm nhận."}), 400
    if not message and not photo_file:
        return jsonify({"success": False, "error": "Thiếu nội dung tin nhắn hoặc ảnh."}), 400

    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)

        if photo_file:
            image_bytes = photo_file.read()
            if not image_bytes:
                return jsonify({"success": False, "error": "File ảnh rỗng."}), 400
            if len(image_bytes) > 20 * 1024 * 1024:
                return jsonify({"success": False, "error": "Ảnh vượt quá 20MB."}), 400

            from features.messaging.send_photo import send_photo as _send_photo
            # Gửi ảnh TRƯỚC (không caption), sau đó gửi text thành tin nhắn riêng.
            result = _send_photo(
                image_bytes,
                photo_file.filename or "image.jpg",
                group_id,
                zpw_enk,
                cookies,
                imei,
                desc="",
                is_group=True,
                zpw_ver=get_zpw_ver(),
            )
            if result.get("ok"):
                text_sent = False
                text_error = ""
                if message:
                    try:
                        from features.messaging.send_link import send_message_smart
                        text_result = send_message_smart(
                            group_id, message, zpw_enk, cookies, imei,
                            is_group=True, zpw_ver=get_zpw_ver(),
                        )
                        text_sent = bool(text_result.get("ok"))
                        if not text_sent:
                            text_error = f"Gửi text sau ảnh lỗi: {text_result.get('error')}"
                    except Exception as text_exc:
                        text_error = f"Gửi text sau ảnh lỗi: {text_exc}"
                return jsonify({
                    "success": True,
                    "message": "Đã gửi ảnh vào nhóm." + (" Đã gửi tin nhắn." if text_sent else ""),
                    "photoId": result.get("photoId"),
                    "urls": result.get("urls"),
                    "textSent": text_sent,
                    "textError": text_error,
                    "data": result.get("decoded"),
                })
            return jsonify({
                "success": False,
                "error": result.get("message", "Gửi ảnh vào nhóm thất bại."),
                "step": result.get("step"),
                "detail": result.get("decoded"),
            }), 400

        # Nội dung chứa link nhóm Zalo -> tự lấy thông tin nhóm rồi gửi link card
        # (group/sendlink); không có link hoặc lỗi -> gửi text nhóm như cũ.
        from features.messaging.send_link import send_message_smart
        result = send_message_smart(
            group_id, message, zpw_enk, cookies, imei,
            is_group=True, zpw_ver=get_zpw_ver(),
        )

        if result.get("ok"):
            return jsonify({
                "success": True,
                "message": "Đã gửi tin nhắn vào nhóm." + (" (link card)" if result.get("sentAsLink") else ""),
                "sentAsLink": bool(result.get("sentAsLink")),
                "data": result.get("decoded"),
            })

        return jsonify({
            "success": False,
            "error": result.get("error") or "Gửi tin nhắn nhóm thất bại.",
            "detail": result.get("decoded"),
        }), 400

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/create-group", methods=["POST"])
def api_create_group():
    """API: Tạo nhóm mới từ danh sách thành viên theo account được chọn."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    account_id = data.get("accountId", data.get("account_id", "")).strip()
    name = data.get("name", "").strip()
    desc = data.get("desc", "").strip()
    user_ids = data.get("userIds", [])

    if not name:
        return jsonify({"error": "Tên nhóm không được để trống!"}), 400
    if not user_ids:
        return jsonify({"error": "Danh sách thành viên trống!"}), 400

    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
        _, decoded = _create_group(name, user_ids, zpw_enk, cookies, imei, zpw_ver=get_zpw_ver())

        error_code = decoded.get("error_code", -1) if isinstance(decoded, dict) else -1
        if error_code == 0:
            group_id = decoded.get("data", {}).get("groupId", "")
            group_url = f"https://zalo.me/g/{group_id}" if group_id else ""
            return jsonify({
                "success": True,
                "groupId": group_id,
                "groupUrl": group_url,
                "message": f"Tạo nhóm thành công! Group ID: {group_id}"
            })

        return jsonify({
            "error": f"Lỗi tạo nhóm (code: {error_code})",
            "detail": decoded
        }), 400

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route("/api/userinfo", methods=["POST"])
def api_get_userinfo():
    """
    API: Lấy user info từ jr.chat.zalo.me theo account được chọn.

    Body:
    {
        "accountId": "...",
        "uid_list": []
    }
    """
    data = request.get_json() or {}

    account_id = data.get("accountId", data.get("account_id", "")).strip()
    uid_list = data.get("uid_list", [])

    try:
        account, cookies, _, _ = _get_account_credentials(account_id, require_imei=False)

        sse_broadcast("📡 Đang lấy user info...", "loading")

        if uid_list:
            result = get_userinfo_batch(cookies, uid_list)
            sse_broadcast(f"✅ Hoàn thành! Lấy được {len(result)} profiles.", "success")
            return jsonify({
                "success": True,
                "total": len(result),
                "data": result
            })

        userinfo = get_userinfo_quick(cookies)
        if userinfo:
            return jsonify({
                "success": True,
                "userinfo": userinfo
            })

        return jsonify({"error": "Không lấy được user info."}), 400

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        sse_broadcast(f"❌ Lỗi: {str(e)}", "error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/userinfo/batch", methods=["POST"])
def api_get_userinfo_batch():
    """
    API: Batch lấy thông tin profile của nhiều UID.
    
    Body:
    {
        "cookies": "...",
        "uid_list": ["uid1", "uid2", ...],
        "batch_size": 20  // optional
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    cookies = data.get("cookies", "").strip()
    uid_list = data.get("uid_list", [])
    batch_size = data.get("batch_size", 20)
    
    if not cookies or not uid_list:
        return jsonify({"error": "Thiếu Cookies hoặc UID list!"}), 400
    
    try:
        sse_broadcast(f"📡 Đang batch fetch {len(uid_list)} profiles...", "loading")
        
        profiles = get_userinfo_batch(cookies, uid_list, batch_size)
        
        sse_broadcast(f"✅ Hoàn thành! Lấy được {len(profiles)} profiles.", "success")
        return jsonify({
            "success": True,
            "total": len(profiles),
            "profiles": profiles
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        sse_broadcast(f"❌ Lỗi: {str(e)}", "error")
        return jsonify({"error": str(e)}), 500


# ─── SCHEDULE APIs ─────────────────────────────────────────────────────────


def _fetch_group_members_worker(task, account_id: str, group_input: str, auto_join_when_not_member: bool = True):
    """Worker lấy thành viên nhóm dùng chung service với /run để tránh trùng logic."""
    try:
        def task_log(msg, typ="info"):
            task.log(msg)

        task.log(f"Đang xử lý: {group_input}")
        task.set_progress(10)

        member_payload, account, cookies, zpw_enk, imei, zpw_ver = _fetch_group_members_session_safe(
            account_id,
            group_input,
            callback=task_log,
            require_imei=False,
            auto_join_when_not_member=auto_join_when_not_member,
            leave_after_auto_join=False,
        )
        uid_list = member_payload["uidList"]
        group_info = member_payload["groupInfo"]
        group_id = member_payload["groupId"]
        member_map = member_payload.get("memberMap") or {}

        task.set_progress(30)
        task.log(f"Lấy được {len(uid_list)} UID. Đang lấy profile...")
        auto_joined = bool(member_payload.get("autoJoined"))
        auto_left = False
        leave_result = None

        try:
            profiles = fetch_profiles_with_single_fallback(
                uid_list,
                zpw_enk,
                cookies,
                imei=imei,
                log_func=task_log,
                zpw_ver=zpw_ver,
            )
            task.set_progress(70)
            result = build_member_rows_from_uids(uid_list, profiles, member_map=member_map, group_info=group_info)
        finally:
            # Nếu tự join ngầm thì LUÔN rời nhóm sau khi đã có UID, kể cả khi
            # lấy profile hoặc build bảng lỗi, để account không kẹt lại trong nhóm.
            if auto_joined and uid_list:
                try:
                    leave_result = _leave_group([group_id], imei=imei, zpw_enk=zpw_enk, cookies=cookies, zpw_ver=zpw_ver)
                    auto_left = bool(isinstance(leave_result, dict) and leave_result.get("ok"))
                except Exception as leave_error:
                    task.log(f"Không rời được nhóm đã tự tham gia: {leave_error}")

        task.set_progress(90)
        task.log(f"Hoàn thành! Tổng {len(result)} thành viên")

        result_data = {
            "total": len(result),
            "data": result,
            "groupInfo": format_group_info(group_info, group_id=group_id, fallback_total=len(result)),
            "autoJoined": auto_joined,
            "autoLeft": auto_left,
            "leaveResult": leave_result,
        }

        task.set_completed(result_data)
        return result_data

    except Exception as e:
        import traceback
        error_msg = str(e)
        task.set_failed(error_msg)
        print(f"[_fetch_group_members_worker] Error: {error_msg}")
        print(traceback.format_exc())
        raise


def _prepare_group_copy_job_worker(task, payload: dict):
    """Lấy thành viên nhóm nguồn và lưu tác vụ sao chép theo hạn mức mỗi ngày."""
    account_id = str(payload.get("accountId") or "").strip()
    source_input = str(payload.get("sourceInput") or "").strip()
    source_input_type, normalized_source_input = normalize_group_input(source_input)
    source_group_id_hint = str(payload.get("sourceGroupId") or "").strip()
    if source_input_type == "group_id" and normalized_source_input:
        # Luôn truyền Group ID sạch vào getmg; không để nhãn ``ID:``, tiền tố g
        # hoặc khoảng trắng làm backend hiểu nhầm thành mã link mời.
        source_input = normalized_source_input
    elif not source_input and source_group_id_hint:
        hint_type, normalized_hint = normalize_group_input(source_group_id_hint)
        if hint_type == "group_id":
            source_input = normalized_hint
    task.set_progress(10)
    task.log("Đang đọc thông tin và danh sách thành viên nhóm nguồn...")
    member_payload, account, cookies, zpw_enk, imei, zpw_ver = _fetch_group_members_session_safe(
        account_id,
        source_input,
        callback=lambda msg, typ="info": task.log(msg, typ),
        require_imei=False,
        auto_join_when_not_member=True,
        leave_after_auto_join=True,
    )
    uid_list = member_payload.get("uidList") or []
    member_map = member_payload.get("memberMap") or {}
    source_group_id = str(member_payload.get("groupId") or "").strip()
    source_group = format_group_info(
        member_payload.get("groupInfo") or {},
        group_id=source_group_id,
        fallback_total=len(uid_list),
    )

    target_group_id = str(payload.get("targetGroupId") or "").strip()
    if payload.get("targetMode") == "existing" and target_group_id == source_group_id:
        raise ValueError("Nhóm nguồn và nhóm đích không được trùng nhau.")

    task.set_progress(40)
    task.log(f"Đã lấy {len(uid_list)} UID. Đang bổ sung tên và ảnh đại diện...")
    profiles = fetch_profiles_with_single_fallback(
        uid_list,
        zpw_enk,
        cookies,
        imei=imei,
        log_func=lambda msg, typ="info": task.log(msg, typ),
        zpw_ver=zpw_ver,
    )
    task.set_progress(58)
    task.log("Đang xác định thành viên đã là bạn bè để ưu tiên thêm vào nhóm trước...")
    friend_relations = fetch_friend_relations(
        uid_list,
        zpw_enk,
        cookies,
        imei=imei,
        log_func=lambda msg, typ="info": task.log(msg, typ),
        zpw_ver=zpw_ver,
    )
    # Danh sách bạn bè thật là nguồn chuẩn. Cờ isFr từ getmg có thể gắn "bạn bè"
    # cho cả thành viên chung của nhóm khác, nếu dùng nó sẽ tưởng người lạ là
    # bạn bè và gọi API thêm trực tiếp -> Zalo trả lỗi 314.
    friend_uids = [uid for uid, is_friend in (friend_relations or {}).items() if is_friend]
    members = build_member_rows_from_uids(
        uid_list,
        profiles,
        member_map=member_map,
        group_info=member_payload.get("groupInfo") or {},
        friend_uids=friend_uids if friend_relations else None,
    )

    # Giới tính để xưng hô đúng (Anh/Chị) trong tin nhắn mời người lạ. Nguồn
    # đáng tin nhất là danh sách bạn bè thật (đã có sẵn gender).
    try:
        friend_genders = fetch_friend_genders(
            uid_list,
            zpw_enk,
            cookies,
            imei=imei,
            log_func=lambda msg, typ="info": task.log(msg, typ),
            zpw_ver=zpw_ver,
        )
    except Exception as exc:
        print(f"[group-copy] Không lấy được giới tính bạn bè: {exc}", flush=True)
        friend_genders = {}
    for member in members:
        uid = str(member.get("userId") or "").strip()
        if not uid:
            continue
        gender = member.get("gender")
        if gender is None or gender == "":
            gender = friend_genders.get(uid)
        member["gender"] = gender

    account_uid = str(
        account.get("uid")
        or account.get("userId")
        or account.get("zaloId")
        or account.get("profileId")
        or ""
    ).strip()
    skip_leaders = bool(payload.get("skipLeaders"))
    skipped_leader_count = 0
    clean_members = []
    seen = set()
    for member in members:
        uid = str(member.get("userId") or member.get("id") or "").strip()
        if not uid or uid == account_uid or uid in seen:
            continue
        seen.add(uid)
        # Bật "Bỏ qua trưởng/phó nhóm": không đưa owner/admin của nhóm nguồn vào tác vụ.
        if skip_leaders and str(member.get("groupRole") or "") in ("owner", "admin"):
            skipped_leader_count += 1
            continue
        relation_value = friend_relations.get(uid)
        raw_friend = relation_value if relation_value is not None else member.get("isFr", member.get("isFriend"))
        friend_text = str(raw_friend).strip().lower()
        is_friend = True if raw_friend is True or friend_text in {"1", "true", "yes"} else (
            False if raw_friend is False or friend_text in {"0", "false", "no"} else None
        )
        clean_members.append({
            "userId": uid,
            "zaloName": member.get("zaloName") or member.get("displayName") or uid,
            "displayName": member.get("displayName") or member.get("zaloName") or uid,
            # Giới tính để xưng hô đúng (Anh/Chị) trong tin nhắn mời người lạ.
            "gender": member.get("gender"),
            "avatar": member.get("avatar") or "",
            # Quan hệ được dùng để ưu tiên thêm toàn bộ bạn bè trước.
            "isFriend": is_friend,
            "isFr": 1 if is_friend is True else (0 if is_friend is False else None),
        })

    if skipped_leader_count:
        task.log(f"Đã bỏ qua {skipped_leader_count} trưởng/phó nhóm của nhóm nguồn theo thiết lập.")
    if not clean_members:
        raise ValueError("Không tìm thấy thành viên hợp lệ trong nhóm nguồn.")

    task.set_progress(85)

    # Danh sách tài khoản thực hiện (đã lọc trùng ở route). Tài khoản đầu là CHỦ.
    account_ids = payload.get("accountIds") or [account_id]
    account_ids = [str(a).strip() for a in account_ids if str(a or "").strip()]
    if not account_ids:
        account_ids = [account_id]

    # Chia khối tuần tự: TK đầu nhận phần đầu danh sách, TK sau nhận phần tiếp theo.
    n = len(account_ids)
    k, m = divmod(len(clean_members), n)
    blocks = [
        clean_members[i * k + min(i, m):(i + 1) * k + min(i + 1, m)]
        for i in range(n)
    ]

    is_multi = n > 1
    target_mode = str(payload.get("targetMode") or "existing").strip()

    # Nhiều tài khoản + tạo nhóm mới: TÀI KHOẢN CHÍNH tạo nhóm NGAY tại đây rồi
    # lấy link nhóm, để mọi tài khoản (chủ + phụ) cùng dùng chung một nhóm và
    # mời vào bằng link đó — thay vì mỗi tài khoản tự tạo một nhóm riêng.
    shared_target_id = str(payload.get("targetGroupId") or "").strip()
    shared_target_link = str(payload.get("targetGroupLink") or "").strip()
    shared_target_name = str(payload.get("newGroupName") or payload.get("targetGroupName") or "").strip()
    if is_multi and target_mode == "new":
        from features.groups.add_group import create_group as _create_group
        from features.groups.group_link import create_group_link as _create_group_link
        from features.groups.group_copy_worker import _extract_group_id, _decoded_dict, _error_message

        master_acc = get_account(account_ids[0]) or {}
        m_cookies = str(master_acc.get("cookies") or "").strip()
        m_enk = str(master_acc.get("zpwEnk") or "").strip()
        m_imei = str(master_acc.get("imei") or "").strip()
        group_name = shared_target_name or "Nhóm mới"
        # Ưu tiên bạn bè của khối tài khoản chính làm thành viên khởi tạo nhóm.
        seed_block = blocks[0] or clean_members
        seed_ids = [
            str(mem.get("userId") or "").strip()
            for mem in sorted(seed_block, key=lambda x: 0 if x.get("isFriend") is True else 1)
            if str(mem.get("userId") or "").strip()
        ][:50]
        task.log(f"Tài khoản chính đang tạo nhóm đích chung '{group_name}'...")
        resp_json, decoded_raw = _create_group(group_name, seed_ids, m_enk, m_cookies, m_imei, zpw_ver=zpw_ver)
        resp_json = resp_json if isinstance(resp_json, dict) else {}
        decoded = _decoded_dict(decoded_raw)
        shared_target_id = _extract_group_id(resp_json, decoded)
        if not shared_target_id:
            raise RuntimeError(_error_message(resp_json, decoded) or "Không tạo được nhóm đích chung bằng tài khoản chính.")
        # Lấy link nhóm để các tài khoản khác mời thành viên vào.
        try:
            link_result = _create_group_link(shared_target_id, m_imei, m_enk, m_cookies, zpw_ver=zpw_ver)
            shared_target_link = str((link_result or {}).get("link") or "").strip()
        except Exception as exc:
            shared_target_link = ""
            task.log(f"Đã tạo nhóm nhưng chưa lấy được link ngay: {exc}", "warn")
        task.log(f"Đã tạo nhóm đích chung (ID {shared_target_id})." + (f" Link: {shared_target_link}" if shared_target_link else ""))
        # Từ đây mọi job dùng nhóm CÓ SẴN này, không tạo nhóm mới nữa.
        target_mode = "existing"

    jobs_created = []
    for idx, aid in enumerate(account_ids):
        block = blocks[idx]
        if not block:
            continue  # tài khoản không được chia thành viên nào (danh sách ngắn hơn số TK)
        acc = get_account(aid) or {}
        acc_name = acc.get("name") or acc.get("displayName") or aid
        acc_avatar = acc.get("avatarUrl") or acc.get("avatar") or ""

        sub = dict(payload)
        sub["accountId"] = aid
        sub.pop("accountIds", None)
        if is_multi:
            sub["title"] = f"{payload.get('title') or 'Sao chép thành viên nhóm'} (TK {idx + 1}/{n} - {acc_name})"
            # Mọi tài khoản dùng chung nhóm đích đã tạo/đã chọn.
            sub["targetMode"] = target_mode
            if shared_target_id:
                sub["targetGroupId"] = shared_target_id
                sub["targetGroupLink"] = shared_target_link
                sub["targetGroupName"] = shared_target_name or sub.get("targetGroupName") or ""
            else:
                sub["targetGroupId"] = str(payload.get("targetGroupId") or "")
                sub["targetGroupLink"] = str(payload.get("targetGroupLink") or "")

        job = create_group_copy_job(sub, block, source_group, acc_name, account_avatar=acc_avatar)
        # Ghi cờ nhiều tài khoản để hiển thị/quản lý.
        if is_multi:
            def _tag(j, midx=idx):
                j["multiAccount"] = True
                j["multiAccountIndex"] = midx
                j["multiAccountTotal"] = n
            save_group_copy_job_mutation(job.get("jobId"), _tag)
        jobs_created.append(job)

    task.log(
        f"Đã lập lịch {len(clean_members)} thành viên"
        + (f" chia cho {n} tài khoản (mỗi tài khoản một khối liên tiếp, không trùng)." if is_multi else ".")
        + f" Mỗi tài khoản có tối đa {payload.get('friendRequestDailyLimit', payload.get('dailyLimit'))} lượt tiếp cận/ngày"
        + " (lời mời kết bạn + tin nhắn gửi link nhóm cho người chưa kết bạn)."
    )
    first_job = jobs_created[0] if jobs_created else {}
    return {
        "jobs": [{key: value for key, value in j.items() if key != "members"} for j in jobs_created],
        "jobCount": len(jobs_created),
        "accountCount": n,
        "sourceGroup": source_group,
        "totalMembers": len(clean_members),
        "verifyIntervalMinutes": first_job.get("verifyIntervalMinutes", 30),
        "campaignDurationDays": first_job.get("campaignDurationDays", 30),
    }


@app.route("/api/group-copy/jobs", methods=["GET"])
def api_group_copy_jobs():
    try:
        return jsonify({"success": True, "jobs": list_group_copy_jobs(include_members=False)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "jobs": []}), 500


@app.route("/api/group-pending-members", methods=["GET", "POST"])
def api_group_pending_members():
    """Xem và duyệt thành viên đang chờ vào nhóm.

    GET  ?accountId=..&groupId=..            -> danh sách UID đang chờ duyệt
    POST {accountId, groupId, memberIds[], approve} -> duyệt (approve=true) hoặc từ chối

    Đây là bước đóng nốt vòng "người lạ vào nhóm": khi nhóm bật chế độ phê duyệt
    thành viên, người lạ bấm link sẽ nằm ở hàng đợi cho tới khi có người duyệt.
    """
    data = request.get_json(silent=True) or {}
    account_id = str(
        data.get("accountId")
        or request.args.get("accountId")
        or ""
    ).strip()
    group_id = str(
        data.get("groupId")
        or request.args.get("groupId")
        or ""
    ).strip()

    if not account_id or not group_id:
        return jsonify({"success": False, "error": "Thiếu accountId hoặc groupId."}), 400

    try:
        from features.groups.pending_members import (
            list_pending_members,
            review_pending_members,
        )

        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
        zpw_ver = get_zpw_ver()

        if request.method == "GET":
            result = list_pending_members(
                group_id, imei, zpw_enk, cookies, zpw_ver=zpw_ver
            )
            return jsonify({
                "success": bool(result.get("ok")),
                "pending": result.get("pending") or [],
                "count": len(result.get("pending") or []),
                "errorCode": result.get("errorCode", -1),
                "error": result.get("errorMessage") or "",
            })

        member_ids = data.get("memberIds") or data.get("members") or []
        if isinstance(member_ids, str):
            member_ids = [x.strip() for x in member_ids.split(",") if x.strip()]
        clean_ids = [str(x or "").strip() for x in member_ids if str(x or "").strip()]
        if not clean_ids:
            return jsonify({"success": False, "error": "Thiếu danh sách memberIds cần duyệt."}), 400

        approve = bool(data.get("approve", True))
        result = review_pending_members(
            group_id,
            clean_ids,
            imei,
            zpw_enk,
            cookies,
            approve=approve,
            zpw_ver=zpw_ver,
        )
        return jsonify({
            "success": bool(result.get("ok")),
            "approved": result.get("approved") or [],
            "failed": result.get("failed") or [],
            "results": result.get("results") or {},
            "errorCode": result.get("errorCode", -1),
            "error": result.get("errorMessage") or "",
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/group-copy/jobs/<job_id>", methods=["GET", "DELETE"])
def api_group_copy_job_detail(job_id):
    try:
        if request.method == "DELETE":
            delete_group_copy_job(job_id)
            return jsonify({"success": True, "message": "Đã xóa tác vụ sao chép nhóm."})
        job = get_group_copy_job(job_id)
        if not job:
            return jsonify({"success": False, "error": "Không tìm thấy tác vụ."}), 404
        return jsonify({"success": True, "job": job})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/group-copy/jobs/<job_id>/cancel", methods=["POST"])
def api_group_copy_job_cancel(job_id):
    try:
        job = cancel_group_copy_job(job_id)
        return jsonify({"success": True, "message": "Đã hủy tác vụ.", "job": job})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/group-copy/jobs/<job_id>/resume", methods=["POST"])
def api_group_copy_job_resume(job_id):
    data = request.get_json(silent=True) or {}
    try:
        job = resume_group_copy_job(job_id, str(data.get("nextRunAt") or "").strip())
        return jsonify({"success": True, "message": "Đã tiếp tục tác vụ.", "job": job})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/group-copy/jobs/<job_id>/verify", methods=["POST"])
def api_group_copy_job_verify(job_id):
    try:
        job = request_group_copy_verification(job_id)
        return jsonify({
            "success": True,
            "message": "Đã đưa yêu cầu kiểm tra nhóm đích vào hàng đợi.",
            "job": job,
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/group-copy/start", methods=["POST"])
def api_group_copy_start():
    data = request.get_json(silent=True) or {}
    account_id = str(data.get("accountId") or "").strip()
    # Nhiều tài khoản cùng thực hiện, chia khối thành viên không trùng.
    raw_account_ids = data.get("accountIds") or []
    if isinstance(raw_account_ids, str):
        raw_account_ids = [x.strip() for x in raw_account_ids.split(",") if x.strip()]
    account_ids = []
    for aid in ([account_id] + list(raw_account_ids)):
        aid = str(aid or "").strip()
        if aid and aid not in account_ids:
            account_ids.append(aid)
    if not account_id and account_ids:
        account_id = account_ids[0]
    # Gói không cho chọn nhiều tài khoản thực hiện: chỉ giữ 1 tài khoản.
    if not _current_plan().get("multiAccountExec") and len(account_ids) > 1:
        account_ids = [account_id or account_ids[0]]
    source_input = str(data.get("sourceInput") or data.get("sourceGroup") or "").strip()
    source_group_id = str(data.get("sourceGroupId") or data.get("resolvedSourceGroupId") or "").strip()
    source_input_type, normalized_source_input = normalize_group_input(source_input)
    if source_input_type == "group_id" and normalized_source_input:
        source_input = normalized_source_input
    if source_group_id:
        hint_type, normalized_hint = normalize_group_input(source_group_id)
        source_group_id = normalized_hint if hint_type == "group_id" else ""
    target_mode = str(data.get("targetMode") or "existing").strip().lower()
    target_group_id = str(data.get("targetGroupId") or "").strip()
    target_group_name = str(data.get("targetGroupName") or "").strip()
    new_group_name = str(data.get("newGroupName") or "").strip()
    start_at = str(data.get("startAt") or "").strip()
    consent = bool(data.get("consentConfirmed") or data.get("confirmConsent"))

    try:
        daily_limit = max(1, min(int(data.get("friendRequestDailyLimit") or data.get("dailyLimit") or data.get("batchSize") or 10), 100))
    except Exception:
        return jsonify({"success": False, "error": "Số lời mời kết bạn mỗi ngày không hợp lệ."}), 400
    try:
        verify_minutes = max(1, min(int(data.get("verifyIntervalMinutes") or 30), 1440))
    except Exception:
        return jsonify({"success": False, "error": "Chu kỳ kiểm tra không hợp lệ."}), 400
    try:
        campaign_days = max(1, min(int(data.get("campaignDurationDays") or 30), 365))
    except Exception:
        return jsonify({"success": False, "error": "Số ngày chạy chiến dịch không hợp lệ."}), 400

    if not account_id:
        return jsonify({"success": False, "error": "Vui lòng chọn tài khoản thực hiện."}), 400
    if not source_input:
        return jsonify({"success": False, "error": "Vui lòng dán link hoặc ID nhóm nguồn."}), 400
    if target_mode not in {"existing", "new"}:
        return jsonify({"success": False, "error": "Kiểu nhóm đích không hợp lệ."}), 400
    if target_mode == "existing" and not target_group_id:
        return jsonify({"success": False, "error": "Vui lòng chọn nhóm đích có sẵn."}), 400
    if target_mode == "new" and not new_group_name:
        return jsonify({"success": False, "error": "Vui lòng nhập tên nhóm mới."}), 400
    if not start_at:
        return jsonify({"success": False, "error": "Vui lòng chọn thời gian bắt đầu."}), 400
    try:
        parsed_start = datetime.fromisoformat(start_at)
        if parsed_start < datetime.now() - timedelta(minutes=1):
            start_at = datetime.now().isoformat(timespec="minutes")
    except ValueError:
        return jsonify({"success": False, "error": "Thời gian bắt đầu không hợp lệ."}), 400
    if not consent:
        return jsonify({
            "success": False,
            "error": "Cần xác nhận bạn có quyền mời các thành viên này và tuân thủ chính sách nền tảng.",
        }), 400

    account = get_account(account_id)
    if not account:
        return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404
    # Kiểm tra mọi tài khoản phụ đủ phiên.
    for aid in account_ids:
        acc = get_account(aid)
        if not acc:
            return jsonify({"success": False, "error": f"Không tìm thấy tài khoản {aid}."}), 404
        if not all([acc.get("cookies"), acc.get("zpwEnk"), acc.get("imei")]):
            return jsonify({"success": False, "error": f"Tài khoản {acc.get('name') or aid} chưa đủ phiên (cookies/zpwEnk/IMEI)."}), 400

    payload = {
        "title": str(data.get("title") or "Sao chép thành viên nhóm").strip(),
        "accountId": account_id,
        "accountIds": account_ids,
        "sourceInput": source_input,
        "sourceInputType": source_input_type,
        "sourceGroupId": source_group_id or (source_input if source_input_type == "group_id" else ""),
        "targetMode": target_mode,
        "targetGroupId": target_group_id,
        "targetGroupName": target_group_name,
        "newGroupName": new_group_name,
        "dailyLimit": daily_limit,
        "batchSize": daily_limit,
        "friendRequestDailyLimit": daily_limit,
        "verifyIntervalMinutes": verify_minutes,
        "campaignDurationDays": campaign_days,
        "removeFriendAfterJoin": bool(data.get("removeFriendAfterJoin")),
        # Gửi lời mời kết bạn kèm link ngay cùng đợt gửi thẻ link nhóm.
        "friendRequestWithLink": data.get("friendRequestWithLink") is not False,
        "statusPollMinutes": int(data.get("statusPollMinutes") or 2),
        "skipLeaders": bool(data.get("skipLeaders")),
        # Nội dung tin nhắn mời người lạ + tối đa 2 link phụ (chỉ gửi 1 thẻ
        # link card, luôn là link nhóm đích).
        "inviteMessageTemplate": str(data.get("inviteMessageTemplate") or "").strip(),
        "inviteExtraLinks": str(data.get("inviteExtraLinks") or "").strip(),
        "startAt": start_at,
        "consentConfirmed": True,
    }

    task = run_task_in_background(
        _prepare_group_copy_job_worker,
        "Lập lịch sao chép nhóm",
        f"Đọc thành viên từ {source_input}, thêm bạn bè trực tiếp, gửi kết bạn và kiểm tra add lại trong {campaign_days} ngày",
        payload,
    )
    return jsonify({
        "success": True,
        "taskId": task.task_id,
        "status": task.status,
        "message": f"Đang đọc nhóm nguồn và lập lịch thêm bạn bè trực tiếp, gửi kết bạn và kiểm tra add lại mỗi {verify_minutes} phút.",
    }), 202


@app.route("/api/schedules/group-members", methods=["POST"])
def api_schedules_group_members():
    """API: Lấy thành viên nhóm cho lập lịch gửi tin (chạy async, không block)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    account_id = data.get("accountId", data.get("account_id", "")).strip()
    group_input = data.get("groupInput", data.get("group_id", data.get("group_link", ""))).strip()
    # Lập lịch cũng tự fallback ngầm giống /run.
    auto_join_when_not_member = True

    if not account_id or not group_input:
        return jsonify({"error": "Thiếu thông tin!"}), 400

    try:
        # Validate account exists and has credentials
        account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=False)
        
        # Start background task
        task = run_task_in_background(
            _fetch_group_members_worker,
            f"Fetch members: {group_input[:30]}",
            f"Lấy danh sách thành viên từ: {group_input}",
            account_id,
            group_input,
            auto_join_when_not_member
        )
        
        # Return task info immediately
        return jsonify({
            "success": True,
            "taskId": task.task_id,
            "status": task.status,
            "message": f"Đang xử lý... Dùng /api/tasks/{task.task_id} để check progress"
        }), 202  # 202 Accepted
        
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules", methods=["GET"])
def api_get_schedules():
    """API: Danh sách lịch gửi."""
    try:
        schedules = get_all_schedules()
        return jsonify({
            "success": True,
            "schedules": schedules
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules/upload-photo", methods=["POST"])
def api_schedules_upload_photo():
    """API: Lưu ảnh đính kèm cho lịch gửi tin (chiến dịch).

    Nhận multipart `photo`, lưu vào data/schedule_photos/, trả về photoPath
    (tương đối với thư mục data) để gắn vào schedule; worker sẽ đọc file này
    khi chạy lịch và gửi qua photo_original/upload + send.
    """
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    if not photo_file:
        return jsonify({"success": False, "error": "Thiếu file ảnh."}), 400

    image_bytes = photo_file.read()
    if not image_bytes:
        return jsonify({"success": False, "error": "File ảnh rỗng."}), 400
    if len(image_bytes) > 20 * 1024 * 1024:
        return jsonify({"success": False, "error": "Ảnh vượt quá 20MB."}), 400

    try:
        from features.messaging.send_photo import get_image_info
        get_image_info(image_bytes)  # raise ValueError nếu không phải ảnh
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    try:
        import uuid as _uuid
        from features.schedules.schedule_manager import DATA_DIR as _SCHED_DATA_DIR

        original_name = photo_file.filename or "image.jpg"
        ext = os.path.splitext(original_name)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            ext = ".jpg"
        photos_dir = os.path.join(_SCHED_DATA_DIR, "schedule_photos")
        os.makedirs(photos_dir, exist_ok=True)
        file_id = _uuid.uuid4().hex[:12]
        saved_name = f"{file_id}{ext}"
        with open(os.path.join(photos_dir, saved_name), "wb") as f:
            f.write(image_bytes)

        return jsonify({
            "success": True,
            "photoPath": f"schedule_photos/{saved_name}",
            "photoName": original_name,
            "size": len(image_bytes),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/schedules", methods=["POST"])
def api_create_schedule_api():
    """API: Tạo lịch gửi mới."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400

    try:
        account_id = str(data.get("accountId") or data.get("account_id") or "").strip()

        # Danh sách tài khoản gửi (nhiều tài khoản chia người nhận không trùng).
        raw_ids = data.get("accountIds") or []
        if isinstance(raw_ids, str):
            raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
        account_ids = []
        for aid in ([account_id] + list(raw_ids)):
            aid = str(aid or "").strip()
            if aid and aid not in account_ids:
                account_ids.append(aid)
        if not account_id and account_ids:
            account_id = account_ids[0]
        # Gói không cho chọn nhiều tài khoản thực hiện: chỉ giữ 1 tài khoản.
        if not _current_plan().get("multiAccountExec") and len(account_ids) > 1:
            account_ids = [account_id or account_ids[0]]

        # Map accountId -> tên hiển thị (để lịch không hiện N/A).
        name_by_id = {}
        try:
            accounts = load_accounts()
            iterable = accounts.values() if isinstance(accounts, dict) else accounts
            for acc in iterable:
                if not isinstance(acc, dict):
                    continue
                aid = str(acc.get("accountId") or acc.get("id") or acc.get("account_id") or "").strip()
                if aid:
                    name_by_id[aid] = (acc.get("name") or acc.get("displayName") or acc.get("zaloName") or acc.get("phone") or aid[:8])
        except Exception as e:
            print("[api_create_schedule_api] load account names failed:", e)

        recipients = data.get("recipients") or []
        n = len(account_ids)
        base_title = str(data.get("title") or "Chiến dịch").strip()

        raw_times = data.get("runTimes") or []
        if isinstance(raw_times, str):
            raw_times = [x.strip() for x in raw_times.split(",") if x.strip()]
        run_times = []
        for rt in raw_times:
            rt_s = str(rt or "").strip()
            if rt_s and rt_s not in run_times:
                run_times.append(rt_s)
        if not run_times:
            single_rt = str(data.get("runAt") or "").strip()
            if single_rt:
                run_times = [single_rt]
        if not run_times:
            return jsonify({"error": "Thời gian gửi không được để trống"}), 400

        raw_msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        msg_items = []
        for item in raw_msgs:
            if isinstance(item, str) and item.strip():
                msg_items.append({"text": item.strip(), "photoPath": "", "photoName": ""})
            elif isinstance(item, dict):
                txt = str(item.get("text") if "text" in item else (item.get("message") or "")).strip()
                pp = str(item.get("photoPath") or "").strip()
                pn = str(item.get("photoName") or "").strip()
                if txt or pp:
                    msg_items.append({"text": txt, "photoPath": pp, "photoName": pn})
        req_mode = str(data.get("messageMode") or "sequence").strip()

        created = []
        total_waves = len(run_times)
        for t_idx, rt in enumerate(run_times):
            wave = dict(data)
            wave.pop("runTimes", None)
            wave["runAt"] = rt
            if msg_items:
                if req_mode == "per_time":
                    chosen = msg_items[t_idx % len(msg_items)]
                    wave["messages"] = [chosen]
                    wave["message"] = chosen["text"]
                    wave["photoPath"] = chosen["photoPath"]
                    wave["photoName"] = chosen["photoName"]
                    wave["messageMode"] = "sequence"
                else:
                    wave["messages"] = msg_items
                    wave["message"] = msg_items[0]["text"]
                    wave["photoPath"] = msg_items[0]["photoPath"]
                    wave["photoName"] = msg_items[0]["photoName"]
                    wave["messageMode"] = req_mode if req_mode in ("sequence", "rotate_recipient") else "sequence"
            wave_title = f"{base_title} (Đợt {t_idx + 1}/{total_waves})" if total_waves > 1 else base_title

            if n <= 1:
                wave["title"] = wave_title
                if account_id:
                    nm = name_by_id.get(account_id)
                    if nm:
                        wave["accountName"] = nm
                        wave["senderName"] = nm
                created.append(create_schedule(wave))
                continue

            # Nhiều tài khoản: chia người nhận thành khối tuần tự, mỗi tài khoản 1 lịch.
            k, m = divmod(len(recipients), n)
            blocks = [recipients[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]
            for idx, aid in enumerate(account_ids):
                block = blocks[idx]
                if not block:
                    continue
                sub = dict(wave)
                sub.pop("accountIds", None)
                sub["accountId"] = aid
                nm = name_by_id.get(aid, aid[:8])
                sub["accountName"] = nm
                sub["senderName"] = nm
                sub["title"] = f"{wave_title} (TK {idx + 1}/{n} - {nm})"
                sub["recipients"] = block
                created.append(create_schedule(sub))

        if not created:
            return jsonify({"error": "Không có người nhận để chia cho các tài khoản."}), 400
        return jsonify({
            "success": True,
            "schedules": created,
            "scheduleCount": len(created),
            "timeCount": total_waves,
            "accountCount": n,
            "schedule": created[0],
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules/<schedule_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
def api_schedule_handler(schedule_id):
    """API: Xử lý lịch gửi (GET/PUT/PATCH/DELETE)."""
    try:
        if request.method == "GET":
            # Chi tiết lịch gửi
            schedule = get_schedule(schedule_id)
            if not schedule:
                return jsonify({"error": "Không tìm thấy lịch"}), 404
            return jsonify({
                "success": True,
                "schedule": schedule
            })
        
        elif request.method in ["PUT", "PATCH"]:
            # Cập nhật lịch gửi (PUT = full update, PATCH = partial update, both do same thing here)
            data = request.get_json()
            if not data:
                return jsonify({"error": "Dữ liệu không hợp lệ"}), 400
            
            schedule = get_schedule(schedule_id)
            if not schedule:
                return jsonify({"error": "Không tìm thấy lịch"}), 404
            
            # Update fields
            if "title" in data:
                schedule["title"] = data["title"]
            if "message" in data:
                schedule["message"] = data["message"]
            if "runAt" in data:
                schedule["runAt"] = data["runAt"]
                # Reset status to pending when runAt is updated
                schedule["status"] = "pending"
            
            schedule["updatedAt"] = int(time.time() * 1000)
            
            # Save
            update_schedule(schedule_id, schedule)
            
            return jsonify({
                "success": True,
                "schedule": schedule,
                "message": "Đã cập nhật lịch"
            })
        
        elif request.method == "DELETE":
            # Xóa lịch gửi
            delete_schedule(schedule_id)
            return jsonify({"success": True, "message": "Đã xóa lịch"})
        
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




@app.route("/api/schedules/<schedule_id>/cancel", methods=["POST"])
def api_cancel_schedule_api(schedule_id):
    """API: Hủy lịch gửi."""
    try:
        schedule = cancel_schedule(schedule_id)
        return jsonify({
            "success": True,
            "schedule": schedule,
            "message": "Đã hủy lịch"
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules/<schedule_id>/run-now", methods=["POST"])
def api_run_schedule_now(schedule_id):
    """API: Chạy lịch ngay bây giờ."""
    try:
        schedule = get_schedule(schedule_id)
        if not schedule:
            return jsonify({"error": "Không tìm thấy lịch"}), 404
        
        if schedule.get("status") != "pending":
            return jsonify({"error": f"Lịch đang {schedule.get('status')}, không thể chạy"}), 400
        
        # Update runAt to now
        import datetime
        now = datetime.datetime.now().isoformat()
        schedule = update_schedule(schedule_id, {"runAt": now})
        
        return jsonify({
            "success": True,
            "schedule": schedule,
            "message": "Lịch sẽ chạy trong vòng 5 giây"
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules/<schedule_id>/rerun", methods=["POST"])
def api_rerun_schedule(schedule_id):
    """Chạy lại lịch: toàn bộ hoặc chỉ người chưa gửi/lỗi."""
    try:
        schedule = get_schedule(schedule_id)
        if not schedule:
            return jsonify({"error": "Không tìm thấy lịch"}), 404
        if schedule.get("status") == "running":
            return jsonify({"error": "Lịch đang chạy, không thể chạy lại lúc này"}), 400
        mode = str((request.get_json(silent=True) or {}).get("mode") or "retry").strip().lower()
        if mode not in {"all", "retry"}:
            return jsonify({"error": "Chế độ chạy lại không hợp lệ"}), 400
        if mode == "all":
            results = []
        else:
            # Giữ người đã gửi thành công; xóa kết quả lỗi để worker gửi lại.
            results = [r for r in (schedule.get("results") or []) if r.get("status") == "success"]
        schedule = update_schedule(schedule_id, {
            "results": results,
            "status": "running",
            "runAt": datetime.now().isoformat(timespec="seconds"),
            "updatedAt": int(time.time() * 1000),
        })
        return jsonify({"success": True, "schedule": schedule, "mode": mode,
                        "message": "Đã đưa lịch vào hàng đợi chạy lại."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _lookup_phones_worker(task, account_id: str, phones: list):
    """Worker lookup số điện thoại: chạy nền + chạy song song từng số."""
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from features.profiles.search_info_from_phone import (
            get_profile_by_phone,
            normalize_vn_phone
        )

        account = get_account(account_id)
        if not account:
            raise ValueError("Không tìm thấy account")

        cookies = account.get("cookies", "")
        zpw_enk = account.get("zpwEnk", "")
        imei = account.get("imei", "")
        zpw_ver = get_zpw_ver()

        missing = []
        if not cookies:
            missing.append("cookies")
        if not zpw_enk:
            missing.append("zpwEnk")
        if not imei:
            missing.append("imei")

        if missing:
            raise ValueError("Account thiếu: " + ", ".join(missing))

        max_lookup = 500
        max_workers = 8
        normalized_map = {}

        for raw_phone in phones:
            normalized = normalize_vn_phone(raw_phone)
            if normalized and normalized not in normalized_map:
                normalized_map[normalized] = raw_phone

        normalized_items = list(normalized_map.items())

        if len(normalized_items) > max_lookup:
            raise ValueError(f"Chỉ được tra tối đa {max_lookup} số mỗi lần")

        total = len(normalized_items)
        if total == 0:
            raise ValueError("Không có số điện thoại hợp lệ")

        workers = max(1, min(max_workers, total))
        task.log(f"Bắt đầu tra {total} số điện thoại song song với {workers} workers...")
        results = [None] * total

        def _lookup_one(idx, normalized_phone, raw_phone):
            try:
                profile = get_profile_by_phone(
                    phone=normalized_phone,
                    zpw_enk=zpw_enk,
                    cookies=cookies,
                    imei=imei,
                    avatar_size=240,
                    language="vi",
                    req_src=85,
                    zpw_ver=zpw_ver
                )

                if profile and profile.get("userId"):
                    return idx, {
                        "phone": raw_phone,
                        "normalizedPhone": normalized_phone,
                        "success": True,
                        "profile": profile
                    }, None

                return idx, {
                    "phone": raw_phone,
                    "normalizedPhone": normalized_phone,
                    "success": False,
                    "error": "Không lấy được profile hoặc thiếu userId"
                }, None

            except Exception as e:
                return idx, {
                    "phone": raw_phone,
                    "normalizedPhone": normalized_phone,
                    "success": False,
                    "error": str(e)
                }, str(e)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(_lookup_one, idx, normalized_phone, raw_phone): (idx, normalized_phone, raw_phone)
                for idx, (normalized_phone, raw_phone) in enumerate(normalized_items)
            }

            done = 0
            for future in as_completed(future_map):
                idx, normalized_phone, raw_phone = future_map[future]
                _, item, err = future.result()
                results[idx] = item
                done += 1
                task.set_progress(int(done / total * 100))

                if item.get("success"):
                    name = (item.get("profile") or {}).get("displayName") or (item.get("profile") or {}).get("zaloName") or "N/A"
                    task.log(f"✅ ({done}/{total}) {raw_phone}: {name}")
                else:
                    task.log(f"⚠️ ({done}/{total}) {raw_phone}: {item.get('error')}")

        results = [r for r in results if r is not None]
        success_count = len([r for r in results if r.get("success")])
        task.set_progress(100)
        task.log(f"Hoàn thành! {success_count}/{len(results)} số có profile")

        result_data = {
            "results": results,
            "total": len(results),
            "success_count": success_count,
            "parallel": True,
            "max_workers": workers,
            "max_lookup": max_lookup
        }

        task.set_completed(result_data)
        return result_data

    except Exception as e:
        import traceback
        error_msg = str(e)
        task.set_failed(error_msg)
        print(f"[_lookup_phones_worker] Error: {error_msg}")
        traceback.print_exc()
        return {"error": error_msg}


@app.route("/api/schedules/lookup-phones", methods=["POST"])
def api_lookup_phones_for_schedule():
    """API: Tra thông tin số điện thoại để lập lịch gửi (chạy async, không block)."""
    try:
        body = request.get_json(force=True) or {}

        account_id = body.get("account_id") or body.get("accountId") or ""
        phones = body.get("phones") or []
        phones_text = body.get("phonesText") or ""

        if phones_text and not phones:
            phones = [
                line.strip()
                for line in phones_text.replace(",", "\n").splitlines()
                if line.strip()
            ]

        if not account_id:
            return jsonify({
                "success": False,
                "error": "Thiếu account_id"
            }), 400

        if not phones:
            return jsonify({
                "success": False,
                "error": "Chưa nhập số điện thoại"
            }), 400

        # Validate account has credentials
        account = get_account(account_id)
        if not account:
            return jsonify({
                "success": False,
                "error": "Không tìm thấy account"
            }), 404
        
        # Start background task
        task = run_task_in_background(
            _lookup_phones_worker,
            f"Lookup {len(phones)} phones",
            f"Tra thông tin {len(phones)} số điện thoại",
            account_id,
            phones
        )
        
        # Return task info immediately
        return jsonify({
            "success": True,
            "taskId": task.task_id,
            "status": task.status,
            "phonesCount": len(phones),
            "message": f"Đang tra {len(phones)} số... Dùng /api/tasks/{task.task_id} để check progress"
        }), 202  # 202 Accepted

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def _lookup_members_for_invite(
    user_ids: list,
    zpw_enk: str,
    cookies: str,
    imei: str = "",
    zpw_ver=None,
) -> dict:
    """Lấy tên Zalo + giới tính của danh sách UID để soạn tin nhắn mời.

    Nguồn:
      - ``/api/social/friend/getfriends`` cho bạn bè (có gender chính xác).
      - ``/api/social/friend/getprofiles/v2`` cho phần còn lại (thường không có
        gender vì người lạ không nằm trong danh bạ) -> dùng xưng hô "Anh/Chị".

    Trả về ``{uid: {zaloName, displayName, gender}}``.
    """
    result: dict = {}
    uids = [str(uid or "").strip() for uid in (user_ids or []) if str(uid or "").strip()]
    if not uids:
        return result

    try:
        from features.friends.friend_service import get_friend_list

        for item in get_friend_list(zpw_enk, cookies, imei=imei, zpw_ver=zpw_ver) or []:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("userId") or "").strip()
            if not uid:
                continue
            result[uid] = {
                "zaloName": str(item.get("zaloName") or item.get("displayName") or "").strip(),
                "displayName": str(item.get("displayName") or item.get("zaloName") or "").strip(),
                "gender": item.get("gender"),
            }
    except Exception as exc:
        print(f"[groups/invite] Không lấy được danh sách bạn bè: {exc}", flush=True)

    missing = [uid for uid in uids if uid not in result]
    if missing:
        try:
            from features.profiles.get_single_profile import get_single_profile

            profiles = get_single_profile(
                missing,
                zpw_enk,
                cookies,
                imei=imei,
                zpw_ver=zpw_ver,
            ) or {}
            for uid, prof in profiles.items():
                if not isinstance(prof, dict):
                    continue
                clean_uid = str(prof.get("userId") or uid or "").strip()
                if not clean_uid:
                    continue
                result[clean_uid] = {
                    "zaloName": str(prof.get("zaloName") or prof.get("displayName") or "").strip(),
                    "displayName": str(prof.get("displayName") or prof.get("zaloName") or "").strip(),
                    "gender": prof.get("gender"),
                }
        except Exception as exc:
            print(f"[groups/invite] Không lấy được profile: {exc}", flush=True)

    return result


def _invite_strangers_via_group_link(
    account_id: str,
    group_ids: list,
    user_ids: list,
    imei: str,
    zpw_enk: str,
    cookies: str,
    zpw_ver,
    members_by_uid: dict = None,
    template: str = "",
    extra_links: str = "",
    group_name: str = "",
) -> list:
    """Gửi thẻ link nhóm cho người lạ qua tin nhắn 1-1.

    Zalo không cho add trực tiếp người chưa kết bạn vào nhóm (mã 314/269), nhưng
    vẫn cho gửi thẻ link nhóm trong tin nhắn 1-1 (``/api/message/link``, code 0).
    Người nhận chỉ cần bấm vào link là vào nhóm, không cần kết bạn trước.

    Chỉ MỘT thẻ link được gửi và thẻ đó luôn là link nhóm đích. Nội dung tin
    nhắn dựng từ ``template`` với tên Zalo và xưng hô theo giới tính thật.

    Trả về danh sách kết quả từng người: {userId, ok, code, message, groupId}.
    """
    results = []
    if not group_ids or not user_ids:
        return results

    members_by_uid = members_by_uid if isinstance(members_by_uid, dict) else {}

    try:
        from features.groups.group_link import get_group_link_detail
        from features.messaging.invite_message import (
            DEFAULT_INVITE_TEMPLATE,
            extract_extra_links,
            render_invite_template,
        )
        from features.messaging.send_link import resolve_group_link_info, send_message_smart
    except Exception as exc:
        print(f"[groups/invite] Không nạp được module gửi link: {exc}", flush=True)
        return [
            {
                "userId": uid,
                "ok": False,
                "code": -1,
                "message": f"Không nạp được module gửi link: {exc}",
            }
            for uid in user_ids
        ]

    for group_id in group_ids:
        group_id = str(group_id or "").strip()
        if not group_id:
            continue

        # Lấy link tham gia của nhóm (có cache sẵn trong tài khoản).
        link = ""
        try:
            detail = get_group_link_detail(
                group_id,
                imei,
                zpw_enk,
                cookies,
                zpw_ver=zpw_ver,
            )
            link = str(detail.get("link") or "").strip()
        except Exception as exc:
            print(f"[groups/invite] Không lấy được link nhóm {group_id}: {exc}", flush=True)

        if not link:
            # Thử link đã lưu trong tài khoản.
            try:
                account = get_account(account_id) or {}
                raw_groups = account.get("personalGroups") or []
                if isinstance(raw_groups, dict):
                    raw_groups = list(raw_groups.values())
                for item in raw_groups:
                    if not isinstance(item, dict):
                        continue
                    gid = str(item.get("groupId") or item.get("gridId") or item.get("id") or "").strip()
                    if gid != group_id:
                        continue
                    link = str(
                        item.get("groupLink")
                        or item.get("group_link")
                        or item.get("inviteLink")
                        or item.get("link")
                        or ""
                    ).strip()
                    if link:
                        break
            except Exception:
                link = ""

        if not link:
            for uid in user_ids:
                results.append(
                    {
                        "userId": uid,
                        "ok": False,
                        "code": -1,
                        "message": "Chưa có link tham gia của nhóm nên không gửi được cho người lạ.",
                        "groupId": group_id,
                    }
                )
            continue

        link_info = None
        try:
            link_info = resolve_group_link_info(
                link,
                zpw_enk,
                cookies,
                zpw_ver=zpw_ver,
                imei=imei,
            )
        except Exception as exc:
            print(f"[groups/invite] Không lấy được metadata link nhóm: {exc}", flush=True)

        message = f"Mời bạn tham gia nhóm. Bấm vào link bên dưới để vào nhóm: {link}"
        for uid in user_ids:
            uid = str(uid or "").strip()
            if not uid:
                continue
            member = members_by_uid.get(uid) or {}
            text = render_invite_template(
                template or DEFAULT_INVITE_TEMPLATE,
                name=str(
                    member.get("zaloName")
                    or member.get("displayName")
                    or member.get("dName")
                    or ""
                ).strip(),
                gender=member.get("gender"),
                link=link,
                group_name=group_name,
                extra_links=extract_extra_links(extra_links, limit=2),
            )
            try:
                outcome = send_message_smart(
                    uid,
                    text,
                    zpw_enk,
                    cookies,
                    imei,
                    is_group=False,
                    zpw_ver=zpw_ver,
                    link_info=link_info,
                    # Ép thẻ card là link nhóm đích, không phải link phụ.
                    link=link,
                )
                ok = bool(outcome.get("ok"))
                decoded = outcome.get("decoded") if isinstance(outcome, dict) else None
                code = -1
                detail = ""
                if isinstance(decoded, dict):
                    try:
                        code = int(decoded.get("error_code", -1))
                    except (TypeError, ValueError):
                        code = -1
                    detail = str(decoded.get("error_message") or decoded.get("message") or "").strip()
                if not ok and not detail:
                    detail = str((outcome or {}).get("error") or "").strip()
                results.append(
                    {
                        "userId": uid,
                        "ok": ok,
                        "code": code,
                        "message": detail,
                        "groupId": group_id,
                        "sentAsLink": bool(outcome.get("sentAsLink")),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "userId": uid,
                        "ok": False,
                        "code": -1,
                        "message": str(exc),
                        "groupId": group_id,
                    }
                )
    return results


@app.route("/api/groups/invite", methods=["POST"])
def api_invite_members_to_groups():
    """API: Mời danh sách thành viên đã chọn vào một hoặc nhiều nhóm cá nhân."""
    data = request.get_json(silent=True) or {}

    account_id = str(data.get("accountId", data.get("account_id", "")) or "").strip()
    group_ids = data.get("groupIds") or data.get("groups") or data.get("gridIds") or []
    user_ids = data.get("userIds") or data.get("members") or data.get("uids") or []

    try:
        batch_size = int(data.get("batchSize") or 50)
    except Exception:
        batch_size = 50
    batch_size = max(1, min(batch_size, 100))

    if isinstance(group_ids, str):
        group_ids = [group_ids]
    if isinstance(user_ids, str):
        user_ids = [user_ids]

    # Chuẩn hóa và bỏ trùng nhưng giữ thứ tự.
    seen_groups = set()
    clean_group_ids = []
    for gid in group_ids:
        gid = str(gid or "").strip()
        if gid and gid not in seen_groups:
            seen_groups.add(gid)
            clean_group_ids.append(gid)

    seen_users = set()
    clean_user_ids = []
    for uid in user_ids:
        uid = str(uid or "").strip()
        if uid and uid not in seen_users:
            seen_users.add(uid)
            clean_user_ids.append(uid)

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản thực hiện."}), 400
    if not clean_group_ids:
        return jsonify({"success": False, "error": "Chưa chọn nhóm để mời."}), 400
    if not clean_user_ids:
        return jsonify({"success": False, "error": "Danh sách thành viên cần mời đang trống."}), 400

    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=True)
        zpw_ver = get_zpw_ver()

        # Zalo chỉ cho thêm trực tiếp vào nhóm những người ĐÃ LÀ BẠN BÈ.
        # Nếu gộp người lạ vào cùng batch, Zalo trả lỗi 314 cho cả batch và
        # không ai được thêm. Vì vậy tách bạn bè (add trực tiếp được) và người
        # lạ (chỉ có thể gửi link/lời mời) thành hai lần gọi riêng.
        friend_uids = set()
        try:
            from features.friends.friend_service import get_friend_uid_set

            friend_uids = get_friend_uid_set(zpw_enk, cookies, imei=imei, zpw_ver=zpw_ver)
        except Exception as exc:
            print(f"[groups/invite] Không lấy được danh sách bạn bè: {exc}", flush=True)

        known_friends = [uid for uid in clean_user_ids if uid in friend_uids]
        known_strangers = [uid for uid in clean_user_ids if uid not in friend_uids]
        if friend_uids and known_friends and known_strangers:
            print(
                f"[groups/invite] Tách {len(known_friends)} bạn bè và "
                f"{len(known_strangers)} người lạ.",
                flush=True,
            )

        # Người lạ: Zalo chặn add trực tiếp (mã 314/269). Hai đường Zalo chấp
        # nhận với người chưa kết bạn:
        #   1. Thẻ link nhóm trong tin nhắn 1-1 (/api/message/link, code 0) —
        #      người nhận bấm là vào nhóm ngay, KHÔNG cần kết bạn trước.
        #   2. Lời mời kết bạn kèm link nhóm — sau khi họ chấp nhận thì add
        #      trực tiếp được.
        # Nếu không tách riêng, Zalo trả 314 cho CẢ batch và không ai vào nhóm.
        stranger_link_results = []
        if known_strangers:
            # Lấy tên Zalo + giới tính để xưng hô đúng trong tin nhắn mời.
            members_by_uid = _lookup_members_for_invite(
                known_strangers,
                zpw_enk,
                cookies,
                imei=imei,
                zpw_ver=zpw_ver,
            )
            stranger_link_results = _invite_strangers_via_group_link(
                account_id,
                clean_group_ids,
                known_strangers,
                imei,
                zpw_enk,
                cookies,
                zpw_ver,
                members_by_uid=members_by_uid,
                template=str(data.get("inviteMessageTemplate") or "").strip(),
                extra_links=str(data.get("inviteExtraLinks") or "").strip(),
                group_name=str(data.get("groupName") or "").strip(),
            )

        results = []
        total_accepted = 0
        total_invited = 0
        total_joined = 0
        total_inbox = 0
        total_failed = 0

        for group_id in clean_group_ids:
            invite_result = _invite_members_to_group(
                group_id,
                known_friends or clean_user_ids,
                imei,
                zpw_enk,
                cookies,
                zpw_ver=zpw_ver,
                batch_size=batch_size,
            )
            group_result = {
                "groupId": group_id,
                "success": bool(invite_result.get("success")),
                "partial": bool(invite_result.get("partial")),
                "accepted": int(invite_result.get("acceptedCount") or 0),
                "invited": int(invite_result.get("invitedCount") or 0),
                "joined": int(invite_result.get("joinedCount") or 0),
                "inbox": int(invite_result.get("inboxCount") or 0),
                "failed": int(invite_result.get("failedCount") or 0),
                "errorMembers": [
                    item.get("userId")
                    for item in (invite_result.get("members") or [])
                    if item.get("status") == "failed"
                ],
                "members": invite_result.get("members") or [],
                "batches": invite_result.get("batches") or [],
            }
            total_accepted += group_result["accepted"]
            total_invited += group_result["invited"]
            total_joined += group_result["joined"]
            total_inbox += group_result["inbox"]
            total_failed += group_result["failed"]
            results.append(group_result)

        stranger_link_sent = sum(1 for item in stranger_link_results if item.get("ok"))
        stranger_link_failed = len(stranger_link_results) - stranger_link_sent
        all_success = total_failed == 0
        message = (
            f"Zalo đã nhận {total_accepted} lượt mời"
            + (f", trong đó {total_inbox} lượt vào tin nhắn chờ" if total_inbox else "")
            + (f"; lỗi {total_failed} lượt." if total_failed else ".")
        )
        if known_strangers:
            if stranger_link_sent:
                message += (
                    f" {stranger_link_sent}/{len(known_strangers)} người chưa kết bạn "
                    "đã nhận thẻ link nhóm — họ bấm là vào nhóm được ngay, không cần kết bạn trước."
                )
            else:
                message += (
                    f" {len(known_strangers)} người chưa kết bạn không thể thêm trực tiếp "
                    "(Zalo chỉ cho thêm bạn bè) và chưa gửi được link nhóm cho họ."
                )
        return jsonify({
            "success": all_success and stranger_link_failed == 0,
            "partial": not all_success and total_accepted > 0,
            "message": message,
            "totalGroups": len(clean_group_ids),
            "totalMembers": len(clean_user_ids),
            "totalAccepted": total_accepted,
            "totalInvited": total_invited,
            "totalJoined": total_joined,
            "totalInbox": total_inbox,
            "totalFailed": total_failed,
            "friendCount": len(known_friends),
            "strangerCount": len(known_strangers),
            "strangerUserIds": known_strangers,
            "strangerLinkSentCount": stranger_link_sent,
            "strangerLinkFailedCount": stranger_link_failed,
            "strangerLinkResults": stranger_link_results,
            "results": results,
        }), 200 if total_accepted > 0 or all_success or stranger_link_sent > 0 else 400

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ─── GROUP APIs ─────────────────────────────────────────────────────────


def _chrome_debug_port_alive(port) -> bool:
    """Kiểm tra Chrome remote-debugging port còn truy cập được không."""
    try:
        port = int(port)
    except Exception:
        return False
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.2)
        return resp.status_code == 200
    except Exception:
        return False


_GROUPS_COUNT_REFRESH_LOCK = threading.Lock()
_GROUPS_COUNT_REFRESH_AT: dict = {}


def _schedule_member_count_refresh(account_id: str, account: dict) -> None:
    """Hẹn giờ làm mới số thành viên của các nhóm (nền, không chặn request).

    Chỉ chạy lại sau mỗi 2 phút và mỗi tài khoản chỉ một luồng tại một thời
    điểm -> tránh spam API Zalo khi frontend poll liên tục.
    """
    try:
        if not str(account.get("zpwEnk") or "").strip():
            return
        if not str(account.get("cookies") or "").strip():
            return
        if not (account.get("personalGroups") or []):
            return

        now = time.time()
        with _GROUPS_COUNT_REFRESH_LOCK:
            last = float(_GROUPS_COUNT_REFRESH_AT.get(account_id) or 0)
            if now - last < 120:
                return
            _GROUPS_COUNT_REFRESH_AT[account_id] = now

        def _worker() -> None:
            try:
                from features.groups.group_manager import refresh_member_counts
                refresh_member_counts(account_id)
            except Exception as exc:
                print(f"[groups-count-refresh] Lỗi: {exc}", flush=True)

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"groups-count-{str(account_id)[:8]}",
        ).start()
    except Exception as exc:
        print(f"[groups-count-refresh] Không lên lịch được: {exc}", flush=True)


@app.route("/api/groups/personal", methods=["GET"])
def api_get_account_groups():
    """API: Lay danh sach personalGroups da luu trong data/accounts.json theo account dang chon.

    Nếu số thành viên đã cũ quá 2 phút thì làm mới nền (không chặn request)
    để con số "N thành viên" trên UI luôn sát thực tế.
    """
    account_id = (
        request.args.get("accountId")
        or request.args.get("account_id")
        or request.args.get("id")
        or ""
    ).strip()

    if not account_id:
        return jsonify({"success": False, "error": "Thieu accountId", "groups": []}), 400

    try:
        accounts = load_accounts()

        account = None
        for acc in accounts:
            aid = str(acc.get("accountId") or acc.get("id") or acc.get("account_id") or "").strip()
            if aid == account_id:
                account = acc
                break

        if not account:
            return jsonify({
                "success": False,
                "error": "Khong tim thay tai khoan",
                "accountId": account_id,
                "groups": []
            }), 404

        raw_groups = account.get("personalGroups") or []

        # personalGroups co the la list hoac dict
        if isinstance(raw_groups, dict):
            raw_groups = list(raw_groups.values())

        groups = []
        for g in raw_groups:
            if not isinstance(g, dict):
                continue

            gid = str(
                g.get("groupId")
                or g.get("gridId")
                or g.get("id")
                or g.get("gid")
                or ""
            ).strip()

            if not gid:
                continue

            name = (
                g.get("name")
                or g.get("grid_name")
                or g.get("groupName")
                or g.get("title")
                or ("Group " + gid[:8])
            )

            group_link = str(
                g.get("groupLink")
                or g.get("group_link")
                or g.get("inviteLink")
                or g.get("link")
                or ""
            ).strip()
            if group_link and not group_link.lower().startswith(("http://", "https://")):
                token = group_link.strip().strip("/")
                group_link = ("https://" + token) if token.lower().startswith("zalo.me/g/") else ("https://zalo.me/g/" + token)

            groups.append({
                "groupId": gid,
                "name": name,
                "avatar": g.get("avatar") or g.get("grid_avatar") or g.get("avt") or "",
                "fullAvt": g.get("fullAvt") or g.get("grid_fullAvt") or g.get("fullAvatar") or "",
                "memberCount": g.get("memberCount") or g.get("grid_totalMember") or g.get("totalMember") or g.get("total") or 0,
                "groupLink": group_link,
                "group_link": group_link,
                "inviteLink": group_link,
                "linkStatus": g.get("linkStatus") or ("ready" if group_link else "pending"),
                "linkExpirationDate": int(g.get("linkExpirationDate") or g.get("expirationDate") or 0),
                "linkCreatedAutomatically": bool(g.get("linkCreatedAutomatically", False)),
                "linkError": g.get("linkError") or "",
                "fetchStatus": g.get("fetchStatus") or "",
                "error": g.get("error") or "",
            })

        # Dữ liệu cũ chỉ có Group ID sẽ được bổ sung link tự động ở nền.
        # Không khóa request UI; lần tải/poll kế tiếp sẽ nhận groupLink đã lưu.
        missing_link_ids = [
            item["groupId"] for item in groups
            if not item.get("groupLink") and str(item.get("linkStatus") or "").lower() not in {"error", "ready"}
        ]
        sync_status = str(account.get("groupsSyncStatus") or "").lower()
        if (
            missing_link_ids
            and sync_status not in {"refreshing", "ids_captured", "links_updating"}
            and account.get("zpwEnk")
            and account.get("cookies")
            and account.get("imei")
        ):
            update_account(account_id, groupsSyncStatus="links_updating")
            start_fetch_group_details_parallel(
                account_id,
                [item["groupId"] for item in groups],
                str(account.get("zpwEnk") or ""),
                str(account.get("cookies") or ""),
                max_workers=4,
            )

        print("[api_get_account_groups] account_id=", account_id, "groups=", len(groups))

        # Làm mới số thành viên ở nền nếu đã cũ quá 2 phút. Không chặn
        # request UI -> lần poll sau sẽ có số liệu mới.
        _schedule_member_count_refresh(account_id, account)

        return jsonify({
            "success": True,
            "accountId": account_id,
            "groups": groups,
            "total": len(groups),
            "groupsSyncedAt": int(account.get("groupsSyncedAt") or 0),
            "groupsSyncStatus": account.get("groupsSyncStatus") or "",
            "accountUpdatedAt": int(account.get("updatedAt") or 0),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "groups": []}), 500


@app.route("/api/groups/link", methods=["POST"])
def api_get_or_create_group_link():
    """Lấy hoặc tự kích hoạt link cho nhóm đang được chọn trên giao diện."""
    data = request.get_json(silent=True) or request.form or {}
    account_id = str(
        data.get("accountId")
        or data.get("account_id")
        or ""
    ).strip()
    group_id = str(
        data.get("groupId")
        or data.get("group_id")
        or data.get("gridId")
        or data.get("grid")
        or ""
    ).strip()
    force_value = data.get("force", False)
    force = str(force_value).strip().lower() in {"1", "true", "yes", "on"}

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản."}), 400
    if not group_id:
        return jsonify({"success": False, "error": "Thiếu Group ID."}), 400

    try:
        result = ensure_group_link_for_account(account_id, group_id, force=force)
        status = 200 if result.get("success") else 400
        return jsonify(result), status
    except ValueError as error:
        return jsonify({
            "success": False,
            "groupId": group_id,
            "groupLink": "",
            "linkStatus": "error",
            "error": str(error),
        }), 400
    except Exception as error:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "groupId": group_id,
            "groupLink": "",
            "linkStatus": "error",
            "error": str(error),
        }), 500


def _auto_close_headless_after_groups_sync(account_id: str, timeout_sec: float = 45.0):
    """Đợi đồng bộ nhóm (getlg/v4) xong trên Chrome headless rồi tự đóng để giải phóng RAM/profile."""
    try:
        from features.accounts.account_manager import close_account, is_account_chrome_headless
        deadline = time.time() + float(timeout_sec or 45.0)
        while time.time() < deadline:
            if not is_account_chrome_headless(account_id):
                return
            acc = get_account(account_id) or {}
            if str(acc.get("groupsSyncStatus") or "") == "done":
                # Cho monitor ~2s hoàn tất bắt cookie/userinfo bổ sung nếu đang tới sau getlg/v4
                grace_end = min(deadline, time.time() + 2.5)
                while time.time() < grace_end:
                    acc2 = get_account(account_id) or {}
                    if acc2.get("loginCaptured") and acc2.get("cookies"):
                        break
                    time.sleep(0.4)
                break
            time.sleep(0.8)
        if is_account_chrome_headless(account_id):
            close_account(account_id)
            print(f"[groups-sync] Đã tự đóng Chrome headless cho account={account_id}", flush=True)
    except Exception as e:
        print(f"[groups-sync] Lỗi auto-close headless account={account_id}: {e}", flush=True)


@app.route("/api/groups/refresh-account", methods=["POST"])
def api_refresh_account_groups():
    """
    Đồng bộ lại danh sách nhóm cho tài khoản Zalo đang chọn.

    Ưu tiên gọi API trực tiếp (``getlg/v4`` + ``getmg``) với zpwEnk/cookies đã
    lưu: nhanh (~0.6s) và số thành viên luôn mới. Chỉ khi thiếu credential
    mới mở Chrome headless để bắt session mới như luồng cũ.
    """
    data = request.get_json(silent=True) or request.form or {}
    account_id = str(
        data.get("accountId")
        or data.get("account_id")
        or data.get("id")
        or ""
    ).strip()

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản."}), 400

    try:
        account = get_account(account_id)
        if not account:
            return jsonify({"success": False, "error": "Không tìm thấy tài khoản."}), 404

        started_at = int(time.time() * 1000)

        # ── Nhanh: gọi thẳng API Zalo, không cần Chrome ──────────────────
        zpw_enk = str(account.get("zpwEnk") or "").strip()
        cookies = str(account.get("cookies") or "").strip()
        imei = str(account.get("imei") or "").strip()
        if zpw_enk and cookies:
            try:
                from features.groups.group_sync_api import (
                    fetch_group_ids,
                    fetch_group_summaries,
                )
                from features.groups.group_manager import merge_group_summaries

                group_ids = fetch_group_ids(zpw_enk, cookies)
                if group_ids:
                    summaries = fetch_group_summaries(
                        group_ids, zpw_enk, cookies, imei
                    )
                    merged = merge_group_summaries(
                        group_ids,
                        summaries,
                        account.get("personalGroups") or [],
                    )

                    update_account(
                        account_id,
                        personalGroups=merged,
                        groupsSyncedAt=int(time.time() * 1000),
                        groupsSyncStatus="details_done",
                    )
                    print(
                        f"[groups-sync] API trực tiếp: {len(merged)} nhóm "
                        f"trong {int(time.time() * 1000) - started_at}ms",
                        flush=True,
                    )
                    return jsonify({
                        "success": True,
                        "accountId": account_id,
                        "startedAt": started_at,
                        "reusedRunningBrowser": False,
                        "headless": False,
                        "directApi": True,
                        "account": get_account(account_id) or account,
                        "message": f"Đã đồng bộ {len(merged)} nhóm trực tiếp qua API.",
                    })
            except Exception as exc:
                print(f"[groups-sync] API trực tiếp lỗi, chuyển sang Chrome: {exc}", flush=True)

        # ── Dự phòng: mở Chrome để bắt session mới ────────────────────────
        port = account.get("remoteDebugPort")
        reused_running_browser = False

        # Nếu Chrome của account đang mở, không mở thêm profile trùng nữa.
        # Chỉ reset trạng thái capture và start monitor để điều hướng/reload Zalo Web.
        if port and _chrome_debug_port_alive(port):
            update_account(
                account_id,
                loginCaptured=False,
                userinfoCaptured=False,
                zpwEnk="",
                cookies="",
                personalGroups=[],
                groupsSyncedAt=0,
                groupsSyncStatus="refreshing",
            )
            from features.accounts.account_network_monitor import start_account_network_monitor
            start_account_network_monitor(account_id, int(port))
            account = get_account(account_id) or account
            reused_running_browser = True
        else:
            account = open_account(account_id, clear_groups=True, headless=True)
            threading.Thread(
                target=_auto_close_headless_after_groups_sync,
                args=(account_id, 45.0),
                daemon=True,
                name=f"headless-groups-{account_id[:8]}",
            ).start()

        return jsonify({
            "success": True,
            "accountId": account_id,
            "startedAt": started_at,
            "reusedRunningBrowser": reused_running_browser,
            "headless": not reused_running_browser,
            "directApi": False,
            "account": account,
            "message": (
                "Đang đồng bộ lại danh sách nhóm từ trình duyệt đang mở."
                if reused_running_browser
                else "Đang chạy ngầm đồng bộ danh sách nhóm (headless)..."
            ),
        })

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/groups/join-link", methods=["POST"])
def api_join_group_by_link():
    """API riêng: tham gia nhóm bằng link mời."""
    data = request.get_json(silent=True) or request.form or {}
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    link = str(data.get("link") or data.get("groupLink") or data.get("group_link") or "").strip()

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản."}), 400
    if not link:
        return jsonify({"success": False, "error": "Thiếu link nhóm."}), 400

    try:
        _account, cookies, zpw_enk, _imei = _get_account_credentials(account_id, require_imei=False)
        result = _join_group_by_link(link, zpw_enk, cookies, zpw_ver=get_zpw_ver())
        status = 200 if result.get("ok") else 400
        return jsonify({
            "success": bool(result.get("ok")),
            "message": result.get("message", ""),
            "groupId": result.get("groupId", ""),
            "error_code": result.get("error_code"),
            "decoded": result.get("decoded"),
        }), status
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/groups/leave", methods=["POST"])
def api_leave_group():
    """API riêng: rời một hoặc nhiều nhóm theo groupId/grid."""
    data = request.get_json(silent=True) or request.form or {}
    account_id = str(data.get("accountId") or data.get("account_id") or "").strip()
    grids = data.get("grids") or data.get("groupIds") or data.get("group_ids") or data.get("grid") or data.get("groupId") or ""

    if isinstance(grids, str):
        grids = [x.strip() for x in re.split(r"[,\n\s]+", grids) if x.strip()]
    elif not isinstance(grids, list):
        grids = [grids]

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản."}), 400
    if not grids:
        return jsonify({"success": False, "error": "Thiếu groupId cần rời."}), 400

    try:
        _account, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=False)
        result = _leave_group(grids, imei=imei, zpw_enk=zpw_enk, cookies=cookies, zpw_ver=get_zpw_ver())
        status = 200 if result.get("ok") else 400
        return jsonify({
            "success": bool(result.get("ok")),
            "message": result.get("message", ""),
            "grids": result.get("grids", []),
            "error_code": result.get("error_code"),
            "decoded": result.get("decoded"),
        }), status
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/groups/members", methods=["GET"])
def api_get_group_members_for_manager():
    """API: Lấy thành viên của một nhóm trong tab Quản lý nhóm."""
    account_id = (
        request.args.get("accountId")
        or request.args.get("account_id")
        or ""
    ).strip()
    group_id = (
        request.args.get("groupId")
        or request.args.get("grid")
        or request.args.get("id")
        or ""
    ).strip()

    if not account_id:
        return jsonify({"success": False, "error": "Chưa chọn tài khoản.", "members": []}), 400
    if not group_id:
        return jsonify({"success": False, "error": "Chưa chọn nhóm.", "members": []}), 400

    try:
        member_payload, account, cookies, zpw_enk, imei, zpw_ver = _fetch_group_members_session_safe(
            account_id,
            group_id,
            callback=None,
            require_imei=False,
        )

        uid_list = member_payload.get("uidList") or []
        group_info = member_payload.get("groupInfo") or {}
        resolved_group_id = member_payload.get("groupId") or group_id
        member_map = member_payload.get("memberMap") or {}

        profiles = fetch_profiles_with_single_fallback(
            uid_list,
            zpw_enk,
            cookies,
            imei=imei,
            log_func=None,
            zpw_ver=zpw_ver,
        )
        own_uid = str((account or {}).get("userId") or (account or {}).get("uid") or "").strip()
        friend_uids = _get_account_friend_uids(account_id, account=account, cookies=cookies, zpw_enk=zpw_enk, imei=imei)
        members = build_member_rows_from_uids(
            uid_list,
            profiles,
            member_map=member_map,
            group_info=group_info,
            own_uid=own_uid,
            friend_uids=friend_uids,
        )

        return jsonify({
            "success": True,
            "accountId": account_id,
            "groupId": str(resolved_group_id),
            "total": len(members),
            "members": members,
            "groupInfo": format_group_info(group_info, group_id=resolved_group_id, fallback_total=len(members)),
        })

    except ValueError as e:
        return jsonify({"success": False, "error": str(e), "members": []}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "members": []}), 500


# ─── TASK MANAGEMENT APIs ──────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    """API: Danh sách tất cả tasks (background jobs)."""
    try:
        status_filter = request.args.get("status")  # "running", "completed", "failed", etc.
        limit = int(request.args.get("limit", 50))
        
        tasks = list_tasks(status=status_filter, limit=limit)
        task_dicts = [t.to_dict() for t in tasks]
        
        return jsonify({
            "success": True,
            "tasks": task_dicts,
            "total": len(task_dicts)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<task_id>", methods=["GET"])
def api_get_task_status(task_id):
    """API: Lấy status + progress + result của 1 task."""
    try:
        task = get_task(task_id)
        if not task:
            return jsonify({"error": f"Task {task_id} not found"}), 404
        
        return jsonify({
            "success": True,
            "task": task.to_dict()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/groups/info", methods=["POST"])
def api_get_group_info():
    """API: Lấy thông tin nhóm chi tiết từ Zalo API (không lấy danh sách thành viên).

    Nhận groupId thô hoặc groupInput (link/ID) — dùng chung logic chuẩn hoá
    link->groupId với luồng lấy thành viên (resolve_group_input_to_group_id)
    để trang Sao chép nhóm có thể xem trước avatar/tên/số thành viên nhóm
    nguồn ngay khi dán link, không cần tạo hẳn tác vụ mới biết.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    account_id = data.get("accountId") or data.get("account_id")
    group_input = str(
        data.get("groupInput") or data.get("groupId") or data.get("group_id") or ""
    ).strip()

    if not account_id or not group_input:
        return jsonify({"error": "Thiếu accountId hoặc groupId"}), 400

    try:
        account = get_account(account_id)
        if not account:
            return jsonify({"error": "Không tìm thấy tài khoản"}), 404

        zpw_enk = account.get("zpwEnk", "")
        cookies = account.get("cookies", "")
        imei = account.get("imei", "")

        if not zpw_enk or not cookies:
            return jsonify({"error": "Tài khoản chưa đầy đủ thông tin (zpwEnk, cookies)"}), 400

        try:
            _, group_id, resolved_info, _ = resolve_group_input_to_group_id(
                group_input, zpw_enk, cookies, zpw_ver=get_zpw_ver()
            )
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        resolved_info = resolved_info or {}

        if not group_id:
            return jsonify({"success": False, "error": "Không xác định được Group ID từ link/ID đã nhập."}), 400

        sse_broadcast(f"Đang lấy thông tin nhóm {group_id[:8]}...", "loading")

        from features.members.get_members import get_members as get_members_api

        try:
            print(f"[api_groups_info] Calling get_members for group {group_id[:8]} zpw_enk={zpw_enk[:8]}... cookies len={len(cookies)}")
            resp, decoded, ginfo, mem_list = get_members_api(group_id, zpw_enk, cookies, imei=imei, zpw_ver=get_zpw_ver())

            print(f"[api_groups_info] Response: decoded={type(decoded)} ginfo keys={list(ginfo.keys()) if ginfo else 'None'}")

            if not ginfo:
                print(f"[api_groups_info] ginfo is empty!")
                ginfo = {}

            merged_name = ginfo.get("grid_name") or resolved_info.get("name") or ""
            print(f"[api_groups_info] Returning groupInfo with name={merged_name or 'N/A'}")
            sse_broadcast(f"Hoàn thành! {merged_name or 'Nhóm'}", "success")

            return jsonify({
                "success": True,
                "groupInfo": {
                    "groupId": ginfo.get("gridId", group_id),
                    "name": merged_name,
                    "desc": ginfo.get("grid_desc", "") or resolved_info.get("desc", ""),
                    "type": ginfo.get("grid_type", 0),
                    "creatorId": ginfo.get("grid_creatorId", ""),
                    "adminIds": ginfo.get("grid_adminIds", []),
                    "avt": ginfo.get("grid_avatar", "") or resolved_info.get("avt", ""),
                    "fullAvt": ginfo.get("grid_fullAvt", "") or resolved_info.get("fullAvt", ""),
                    "totalMember": ginfo.get("grid_totalMember", 0) or resolved_info.get("totalMember", 0),
                }
            })
        
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"[api_groups_info] ERROR: {e}")
            print(f"[api_groups_info] Traceback:\n{error_trace}")
            error_msg = f"Lỗi khi lấy thông tin nhóm: {str(e)}"
            sse_broadcast(error_msg, "error")
            return jsonify({
                "success": False,
                "error": error_msg
            }), 400
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        sse_broadcast(f"Lỗi: {str(e)}", "error")
        return jsonify({"error": str(e)}), 500


# ─── Device Management Endpoints ──────────────────────────────────────────────
DEVICE_REGISTRY_FILE = os.path.join(app_root, "data", "devices.json")

def load_devices_registry():
    """Tải danh sách thiết bị"""
    if os.path.exists(DEVICE_REGISTRY_FILE):
        try:
            with open(DEVICE_REGISTRY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_devices_registry(devices):
    """Lưu danh sách thiết bị"""
    os.makedirs(os.path.dirname(DEVICE_REGISTRY_FILE), exist_ok=True)
    with open(DEVICE_REGISTRY_FILE, 'w', encoding='utf-8') as f:
        json.dump(devices, f, indent=2, ensure_ascii=False)

@app.route('/api/device/register', methods=['POST'])
def api_device_register():
    """
    Đăng ký thiết bị mới
    """
    try:
        data = request.json
        device_id = data.get("device_id")
        
        if not device_id:
            return jsonify({"error": "device_id required"}), 400
        
        devices = load_devices_registry()
        
        # Cập nhật hoặc thêm mới thiết bị
        devices[device_id] = {
            "device_name": data.get("device_name", "Unknown"),
            "username": data.get("username", "Unknown"),
            "device_id": device_id,
            "mac_address": data.get("mac_address", ""),
            "imei": data.get("imei", "N/A"),
            "location": data.get("location", "Unknown"),
            "os": data.get("os", ""),
            "ip_address": data.get("ip_address", ""),
            "first_seen": devices.get(device_id, {}).get("first_seen", datetime.now().isoformat()),
            "last_seen": datetime.now().isoformat(),
            "active": True
        }
        
        save_devices_registry(devices)
        
        print(f"✅ Đã đăng ký thiết bị: {data.get('device_name')} ({device_id})")
        
        return jsonify({
            "success": True,
            "message": f"Đã đăng ký thiết bị: {data.get('device_name')}",
            "device": devices[device_id]
        }), 201
        
    except Exception as e:
        print(f"❌ Lỗi đăng ký thiết bị: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/device/log', methods=['POST'])
def api_device_log():
    """
    Ghi log hoạt động thiết bị
    """
    try:
        data = request.json
        device_name = data.get("device_name", "Unknown")
        action = data.get("action", "unknown_action")
        
        log_file = os.path.join(app_root, "data", "device_logs.json")
        
        # Tải logs hiện có
        logs = []
        if os.path.exists(log_file):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    logs = json.load(f)
            except:
                logs = []
        
        # Thêm log mới
        log_entry = {
            "device_name": device_name,
            "username": data.get("username", "Unknown"),
            "action": action,
            "details": data.get("details", ""),
            "timestamp": datetime.now().isoformat(),
            "ip_address": data.get("ip_address", "")
        }
        
        logs.append(log_entry)
        
        # Giữ chỉ 1000 log gần nhất
        if len(logs) > 1000:
            logs = logs[-1000:]
        
        # Lưu logs
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
        
        print(f"📝 Log: {device_name} - {action}")
        
        return jsonify({
            "success": True,
            "message": "Log đã ghi"
        }), 201
        
    except Exception as e:
        print(f"❌ Lỗi ghi log: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/devices/list', methods=['GET'])
def api_devices_list():
    """
    Lấy danh sách tất cả thiết bị đã đăng ký
    """
    try:
        devices = load_devices_registry()
        
        return jsonify({
            "success": True,
            "total": len(devices),
            "devices": list(devices.values())
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/devices', methods=['GET'])
def devices_dashboard():
    """
    Hiển thị dashboard quản lý thiết bị
    """
    try:
        return render_template('devices.html')
    except Exception as e:
        return f"Lỗi: {str(e)}", 500


# ─── Ensure UTF-8 encoding for all responses ──────────────────────────────────
@app.route("/api/updater/version", methods=["GET"])
def api_updater_version():
    return jsonify({
        "success": True,
        "message": "OK",
    })


@app.route("/api/updater/check", methods=["GET", "POST"])
def api_updater_check():
    try:
        current = _read_app_version()
        release = _fetch_latest_release()
        has_update = _is_newer_version(release["version"], current)
        return jsonify({
            "success": True,
            "state": "ready" if has_update else "none",
            "message": "Có bản cập nhật mới." if has_update else "Bạn đang dùng phiên bản mới nhất.",
            "currentVersion": current,
            "latestVersion": release["version"],
        })
    except Exception as e:
        print("[updater] check error=", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/updater/apply", methods=["POST"])
def api_updater_apply():
    try:
        current = _read_app_version()
        release = _fetch_latest_release()
        if not _is_newer_version(release["version"], current):
            return jsonify({
                "success": True,
                "state": "none",
                "message": "Bạn đang dùng phiên bản mới nhất.",
            })

        update_dir = _get_update_dir()
        if os.path.isdir(update_dir):
            shutil.rmtree(update_dir, ignore_errors=True)
        os.makedirs(update_dir, exist_ok=True)
        zip_path = os.path.join(update_dir, UPDATE_ASSET_NAME)
        _download_update_zip(release["downloadUrl"], zip_path)
        _validate_update_zip(zip_path, release.get("size") or 0)
        _verify_update_hash(zip_path, release)
        install_result = _install_zip_update(zip_path, release["version"])

        return jsonify({
            "success": True,
            "state": install_result.get("state") or "done",
            "message": install_result.get("message") or "Đã cập nhật.",
        })
    except Exception as e:
        print("[updater] apply error=", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.after_request
def after_request(response):
    """Ensure all responses use UTF-8 encoding."""
    content_type = response.headers.get('Content-Type', '')
    
    # If no content type, assume text/html
    if not content_type:
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
    # If content type exists but no charset, add it
    elif 'charset' not in content_type:
        if 'application/json' in content_type:
            response.headers['Content-Type'] = 'application/json; charset=utf-8'
        elif 'text/html' in content_type:
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
        elif 'text/javascript' in content_type or 'application/javascript' in content_type:
            response.headers['Content-Type'] = f'{content_type}; charset=utf-8'
        elif 'text/' in content_type:
            response.headers['Content-Type'] = f'{content_type}; charset=utf-8'
    
    # Do not let the embedded browser keep an old HTML/CSS/JS version after a
    # patch has been copied over the application directory.
    request_path = request.path or ""
    if (
        "text/html" in response.headers.get("Content-Type", "")
        or request_path.startswith("/static/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response



# ─── Minimal Tkinter launcher ─────────────────────────────────────────────────
def _get_app_icon_path():
    """Tìm icon .ico của ứng dụng cho Tkinter và PyInstaller.

    Ưu tiên static/ico/app.ico trong thư mục resource đã extract của PyInstaller,
    sau đó fallback về thư mục chạy EXE/source.
    """
    candidates = []
    try:
        candidates.append(os.path.join(static_path, "ico", "app.ico"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(base_path, "static", "ico", "app.ico"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(app_root, "static", "ico", "app.ico"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ico", "app.ico"))
    except Exception:
        pass

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _get_app_logo_path():
    """Return the bundled PNG logo used by the minimal launcher."""
    candidates = [
        os.path.join(static_path, "ico", "app_256.png"),
        os.path.join(base_path, "static", "ico", "app_256.png"),
        os.path.join(app_root, "static", "ico", "app_256.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ico", "app_256.png"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _enable_windows_terminal_copy_mode():
    """Cho phép click/kéo chọn text trong CMD/PowerShell nếu app vẫn chạy console."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10

        ENABLE_MOUSE_INPUT = 0x0010
        ENABLE_INSERT_MODE = 0x0020
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

        stdin_handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()

        if stdin_handle and stdin_handle != ctypes.c_void_p(-1).value:
            if kernel32.GetConsoleMode(stdin_handle, ctypes.byref(mode)):
                old_mode = mode.value
                new_mode = old_mode | ENABLE_EXTENDED_FLAGS | ENABLE_QUICK_EDIT_MODE | ENABLE_INSERT_MODE
                new_mode &= ~ENABLE_MOUSE_INPUT
                new_mode &= ~ENABLE_VIRTUAL_TERMINAL_INPUT

                if kernel32.SetConsoleMode(stdin_handle, new_mode):
                    print(
                        f"✅ Terminal edit/copy mode enabled: old=0x{old_mode:04x}, new=0x{new_mode:04x}",
                        flush=True,
                    )
                    print("✅ Có thể click/kéo chọn text trong terminal để copy log", flush=True)
                else:
                    err = kernel32.GetLastError()
                    print(f"⚠️ Không bật được terminal edit/copy mode. GetLastError={err}", flush=True)
    except Exception as e:
        print(f"⚠️ Không bật được terminal edit/copy mode: {e}", flush=True)


def _start_backend_server():
    """Start các worker phụ và Flask server. Hàm này chạy trong thread nền."""
    try:
        _enable_windows_terminal_copy_mode()

        # DEV MODE: bo check kich hoat — chay thang worker.
        ensure_schedule_worker_started()
        print("✅ Dev mode: bo check kich hoat, worker da chay", flush=True)

        print("✅ Backend đã sẵn sàng", flush=True)
        print("🌐 Giao diện: http://127.0.0.1:5000/policy", flush=True)
        print("👉 Bấm nút 'Mở giao diện' để mở trình duyệt.", flush=True)
        # Share LAN: mac dinh mo 0.0.0.0 de Client LAN goi duoc ngay (1-2 click).
        # An toan nho X-Internal-Token + whitelist LAN (xem features/gateway/auth.py).
        _host = (os.environ.get("NEXUS_HOST") or "0.0.0.0").strip() or "0.0.0.0"
        if _host == "0.0.0.0":
            print("🌐 Share LAN: mo port 5000 cho LAN tin cay (can X-Internal-Token).", flush=True)
        app.run(debug=False, port=5000, host=_host, use_reloader=False, threaded=True)
    except Exception as e:
        print(f"❌ Lỗi khởi động backend: {e}", flush=True)
        import traceback
        traceback.print_exc()


def _cleanup_update_script():
    try:
        candidates = []
        candidates.append(os.path.join(tempfile.gettempdir(), "_nexus_apply_update.bat"))
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), "_nexus_apply_update.bat"))
        else:
            candidates.append(os.path.join(app_root, "_nexus_apply_update.bat"))
        for path in candidates:
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        pass


NEXUS_UI_URL = "http://127.0.0.1:5000/guide"
NEXUS_UI_ORIGIN = "http://127.0.0.1:5000"
NEXUS_UI_EDGE_DEBUG_PORT = 9322


def _find_edge_executable():
    """Tìm Microsoft Edge trên Windows; giao diện Nexus không dùng Chrome/default browser."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    if sys.platform.startswith("win"):
        try:
            import winreg
            registry_locations = (
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            )
            for hive, key_path in registry_locations:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                    if value and os.path.isfile(value):
                        return value
                except OSError:
                    continue
        except Exception:
            pass

    return ""


def _edge_ui_debug_ready():
    try:
        response = requests.get(
            f"http://127.0.0.1:{NEXUS_UI_EDGE_DEBUG_PORT}/json/version",
            timeout=0.6,
        )
        return response.status_code == 200
    except Exception:
        return False


def _edge_ui_targets():
    try:
        response = requests.get(
            f"http://127.0.0.1:{NEXUS_UI_EDGE_DEBUG_PORT}/json/list",
            timeout=0.8,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _find_existing_nexus_ui_target():
    """Tìm bất kỳ tab Nexus đang mở, kể cả khi người dùng đã chuyển khỏi /policy."""
    matches = []
    for target in _edge_ui_targets():
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        url = str(target.get("url") or "")
        if url == NEXUS_UI_ORIGIN or url.startswith(NEXUS_UI_ORIGIN + "/"):
            matches.append(target)

    # /json/list thường trả target đang hoạt động gần đầu danh sách. Không điều hướng
    # lại URL để giữ nguyên đúng trang người dùng đang xem trước đó.
    return matches[0] if matches else None


def _activate_edge_target(target):
    target_id = str((target or {}).get("id") or "").strip()
    if not target_id:
        return False
    try:
        response = requests.get(
            f"http://127.0.0.1:{NEXUS_UI_EDGE_DEBUG_PORT}/json/activate/{target_id}",
            timeout=1.0,
        )
        return response.status_code == 200
    except Exception:
        return False


def _maximize_edge_target_window(target):
    """Phóng to (maximize) cửa sổ Edge chứa target qua CDP để app luôn mở full màn hình."""
    target_id = str((target or {}).get("id") or "").strip()
    if not target_id:
        return False
    ws = None
    try:
        import websocket

        ver_res = requests.get(
            f"http://127.0.0.1:{NEXUS_UI_EDGE_DEBUG_PORT}/json/version",
            timeout=1.0,
        )
        if ver_res.status_code != 200:
            return False
        ws_url = str((ver_res.json() or {}).get("webSocketDebuggerUrl") or "").strip()
        if not ws_url:
            return False

        ws = websocket.create_connection(ws_url, timeout=2.0)

        def _cdp_call(req_id, method, params):
            ws.send(json.dumps({"id": req_id, "method": method, "params": params}))
            for _ in range(10):
                raw = ws.recv()
                msg = json.loads(raw) if raw else {}
                if msg.get("id") == req_id:
                    return msg
            return {}

        win_msg = _cdp_call(1, "Browser.getWindowForTarget", {"targetId": target_id})
        result = win_msg.get("result") or {}
        window_id = result.get("windowId")
        if window_id is None:
            return False
        bounds = result.get("bounds") or {}
        if bounds.get("windowState") == "maximized":
            return True
        set_msg = _cdp_call(
            2,
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "maximized"}},
        )
        return "error" not in set_msg
    except Exception:
        return False
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _create_edge_ui_target():
    """Tạo tab UI chỉ khi Edge đã chạy nhưng chưa hề có tab Nexus."""
    try:
        from urllib.parse import quote
        response = requests.put(
            f"http://127.0.0.1:{NEXUS_UI_EDGE_DEBUG_PORT}/json/new?{quote(NEXUS_UI_URL, safe='')}",
            timeout=1.5,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _open_nexus_ui_in_edge():
    """Mở Nexus bằng Edge App mode: không tab/địa chỉ/thanh điều hướng và không mở trùng cửa sổ."""
    existing = _find_existing_nexus_ui_target()
    if existing and _activate_edge_target(existing):
        _maximize_edge_target_window(existing)
        return True, str(existing.get("url") or NEXUS_UI_URL), True

    edge_exe = _find_edge_executable()
    if not edge_exe:
        return False, "Không tìm thấy Microsoft Edge trên máy.", False

    # Dùng profile Edge riêng cho cửa sổ Nexus để CDP có thể nhận diện cửa sổ
    # đang tồn tại ở lần bấm sau. --app=URL giúp giao diện hiển thị như ứng dụng:
    # chỉ còn title bar của cửa sổ, không có tab, address bar, Back/Refresh hay menu trình duyệt.
    profile_dir = os.path.join(app_root, "data", "nexus_edge_ui")
    os.makedirs(profile_dir, exist_ok=True)

    args = [
        edge_exe,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={NEXUS_UI_EDGE_DEBUG_PORT}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--start-maximized",
        f"--app={NEXUS_UI_URL}",
    ]
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    # Kể cả Edge profile vẫn còn tiến trình nền/CDP sau khi đóng cửa sổ UI,
    # luôn gọi lại --app để Edge tạo đúng cửa sổ app-mode; không dùng /json/new
    # vì endpoint đó có thể sinh tab/cửa sổ trình duyệt thông thường có thanh điều hướng.
    subprocess.Popen(args, creationflags=creationflags, close_fds=True)

    for _ in range(40):
        time.sleep(0.1)
        target = _find_existing_nexus_ui_target()
        if target:
            _activate_edge_target(target)
            _maximize_edge_target_window(target)
            return True, str(target.get("url") or NEXUS_UI_URL), False

    # Edge có thể khởi động chậm hơn thời gian chờ nhưng lệnh --app đã được gửi thành công.
    return True, NEXUS_UI_URL, False


def _launch_wait_window():
    """Show only the Nexus logo and the button that opens the web interface."""
    _cleanup_update_script()
    import socket
    import tkinter as tk
    from tkinter import ttk

    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhanMem.Nexus.Launcher")
    except Exception:
        pass

    root = tk.Tk()
    root.title("Nexus")
    window_width = 420
    window_height = 330
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    pos_x = max(0, (screen_width - window_width) // 2)
    pos_y = max(0, (screen_height - window_height) // 2)
    root.geometry(f"{window_width}x{window_height}+{pos_x}+{pos_y}")
    root.resizable(False, False)
    root.configure(bg="#F5F7FB")

    try:
        icon_path = _get_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            root.iconbitmap(default=icon_path)
    except Exception as error:
        print(f"Không đặt được icon cửa sổ: {error}", flush=True)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        "Nexus.TButton",
        background="#111827",
        foreground="#FFFFFF",
        borderwidth=0,
        focusthickness=0,
        focuscolor="#111827",
        font=("Segoe UI", 11, "bold"),
        padding=(30, 14),
        relief="flat",
    )
    style.map(
        "Nexus.TButton",
        background=[("disabled", "#CBD5E1"), ("active", "#1F2937"), ("pressed", "#0F172A")],
        foreground=[("disabled", "#64748B"), ("active", "#FFFFFF")],
    )

    content = tk.Frame(root, bg="#F5F7FB", padx=42, pady=36)
    content.pack(fill="both", expand=True)

    logo_path = _get_app_logo_path()
    if logo_path:
        try:
            logo_image = tk.PhotoImage(file=logo_path)
            max_side = max(logo_image.width(), logo_image.height())
            if max_side > 150:
                factor = max(1, (max_side + 149) // 150)
                logo_image = logo_image.subsample(factor, factor)
            logo = tk.Label(content, image=logo_image, bg="#F5F7FB", borderwidth=0)
            logo.image = logo_image
            root._nexus_logo_image = logo_image
            logo.pack(pady=(4, 42))
        except Exception as error:
            print(f"Không tải được logo launcher: {error}", flush=True)

    open_button = ttk.Button(
        content,
        text="Đang khởi động…",
        state="disabled",
        style="Nexus.TButton",
    )
    open_button.pack(fill="x")

    def backend_is_ready():
        try:
            with socket.create_connection(("127.0.0.1", 5000), timeout=0.08):
                return True
        except OSError:
            return False

    ui_open_lock = threading.Lock()

    def open_ui():
        if not backend_is_ready() or ui_open_lock.locked():
            return

        def worker():
            with ui_open_lock:
                opened_ok = False
                try:
                    root.after(0, lambda: open_button.configure(text="Đang mở Edge…", state="disabled"))
                    success, detail, reused = _open_nexus_ui_in_edge()
                    if success:
                        opened_ok = True
                        action = "Đã chuyển tới giao diện Edge đang mở" if reused else "Đã mở giao diện bằng Edge"
                        print(f"{action}: {detail}", flush=True)
                    else:
                        print(f"Không thể mở giao diện bằng Edge: {detail}", flush=True)
                except Exception as error:
                    print(f"Không thể mở giao diện bằng Edge: {error}", flush=True)
                finally:
                    try:
                        if opened_ok:
                            # Mo Edge xong -> an launcher di, backend van chay ngam.
                            # Can mo lai: bam lai icon Nexus (neu exe) hoac chay lai python app.py.
                            root.after(0, lambda: (open_button.configure(text="Đã mở — ẩn launcher"), root.withdraw()))
                        else:
                            root.after(0, lambda: open_button.configure(text="Mở giao diện", state="normal"))
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    open_button.configure(command=open_ui)

    def poll_backend():
        if backend_is_ready():
            open_button.configure(text="Mở giao diện", state="normal")
            return
        root.after(250, poll_backend)

    def on_close():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        root.destroy()
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", on_close)

    server_thread = threading.Thread(target=_start_backend_server, daemon=True)
    server_thread.start()
    print("Đang khởi động Nexus. Launcher không hiển thị log.", flush=True)
    root.after(200, poll_backend)
    root.mainloop()


if __name__ == "__main__":
    _launch_wait_window()
