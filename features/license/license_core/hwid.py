import subprocess
import hashlib
import platform
import os
import json

# Lệnh PowerShell timeout
_PS_TIMEOUT_SEC = 10

# In-memory cache để không phải gọi subprocess lặp đi lặp lại
_CACHED_HWID_INFO = None


def _get_hardware_cache_file():
    """Lấy đường dẫn file cache hardware bền vững"""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # data/.hardware_cache.json
        data_dir = os.path.abspath(os.path.join(base_dir, "..", "..", "..", "data"))
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, ".hardware_cache.json")
    except Exception:
        pass
    return None


def _read_persistent_hw_cache():
    """
    Chỉ tin cache khi nó được ghi trên CHÍNH máy này (MachineGuid trùng).
    """
    cache_file = _get_hardware_cache_file()
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not (data.get("uuid") or data.get("disk_stable")):
                return None
            guid = get_machine_guid()
            if not guid or data.get("machine_guid") != guid:
                return None
            return data
        except Exception:
            pass
    return None


def _save_persistent_hw_cache(uuid_val, disk_stable_val, disk_legacy_val):
    cache_file = _get_hardware_cache_file()
    guid = get_machine_guid()
    if cache_file and guid and (uuid_val or disk_stable_val):
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "uuid": uuid_val or "",
                    "disk_stable": disk_stable_val or "",
                    "disk_legacy": disk_legacy_val or "",
                    "machine_guid": guid,
                }, f)
        except Exception:
            pass


def get_machine_guid():
    """MachineGuid của Windows (mỗi lần cài Windows một giá trị), None nếu không đọc được."""
    if platform.system() != "Windows":
        return None
    return _get_windows_machine_guid()


def _get_windows_machine_guid():
    """Lấy Windows MachineGuid từ Registry - không phụ thuộc PowerShell/WMI/Network"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        if guid and isinstance(guid, str) and len(guid.strip()) > 5:
            return guid.strip()
    except Exception:
        pass
    return None


def get_sys_info(command, retries=1):
    """
    Hàm chạy lệnh PowerShell và lấy kết quả sạch.
    """
    system_root = os.environ.get("SystemRoot", "C:\\Windows")
    powershell_exe = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(powershell_exe):
        powershell_exe = "powershell"

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    full_cmd = [powershell_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]

    for _ in range(max(1, retries + 1)):
        try:
            output = subprocess.check_output(
                full_cmd,
                startupinfo=startupinfo,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=_PS_TIMEOUT_SEC,
            ).decode("utf-8", errors="ignore").strip()
            if output:
                return output
        except Exception:
            pass

    return None


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _fallback_hwid():
    """Giá trị dự phòng khi không dò được phần cứng — kết hợp MachineGuid hoặc hostname"""
    guid = _get_windows_machine_guid()
    if guid:
        return _hash(f"GUID-{guid}")
    return _hash(platform.node())


def legacy_fallback_hwid():
    return _hash(platform.node())


_JUNK_SMBIOS_UUIDS = {"03000200-0400-0500-0006-000700080009"}


def _clean_smbios_uuid(uuid):
    if not uuid:
        return None
    u = uuid.strip().upper()
    hex_only = u.replace("-", "")
    if not hex_only or set(hex_only) <= {"F"} or set(hex_only) <= {"0"} or u in _JUNK_SMBIOS_UUIDS:
        return None
    return u


def get_hwid_info(force_refresh=False):
    """
    Trả về đầy đủ thông tin HWID:
        stable      : HWID ổn định (UUID + Sort Index disk serial)
        legacy      : HWID kiểu cũ
        fallback    : True nếu KHÔNG dò được phần cứng
        smbios_uuid : UUID bo mạch (đã lọc rác) cho dấu máy, None nếu không có.
    """
    global _CACHED_HWID_INFO
    if _CACHED_HWID_INFO and not force_refresh:
        return _CACHED_HWID_INFO

    if platform.system() != "Windows":
        h = _hash(platform.node())
        res = {"stable": h, "legacy": h, "fallback": True, "smbios_uuid": None}
        _CACHED_HWID_INFO = res
        return res

    # 1. Thử đọc từ persistent cache trước nếu có
    saved_hw = _read_persistent_hw_cache()

    # 2. Quét phần cứng từ hệ điều hành
    uuid = get_sys_info("(Get-CimInstance -Class Win32_ComputerSystemProduct).UUID", retries=2)
    disk_stable = get_sys_info(
        "(Get-CimInstance -Class Win32_DiskDrive | Sort-Object Index | Select-Object -First 1).SerialNumber", retries=2
    )
    disk_legacy = get_sys_info(
        "(Get-CimInstance -Class Win32_DiskDrive | Select-Object -First 1).SerialNumber", retries=1
    )

    # 3. Nếu quét thất bại do network switch/WMI bận, dùng lại hardware đã lưu bền vững
    if (not uuid or not disk_stable) and saved_hw:
        if not uuid and saved_hw.get("uuid"):
            uuid = saved_hw["uuid"]
        if not disk_stable and saved_hw.get("disk_stable"):
            disk_stable = saved_hw["disk_stable"]
        if not disk_legacy and saved_hw.get("disk_legacy"):
            disk_legacy = saved_hw["disk_legacy"]

    # 4. Lưu vào cache
    if uuid or disk_stable:
        _save_persistent_hw_cache(uuid, disk_stable, disk_legacy)

    # 5. Fallback nếu hoàn toàn trống
    if not uuid and not disk_stable and not disk_legacy:
        h = _fallback_hwid()
        res = {"stable": h, "legacy": h, "fallback": True, "smbios_uuid": None}
        _CACHED_HWID_INFO = res
        return res

    uuid = uuid or ""
    res = {
        "stable": _hash(f"{uuid}-{disk_stable or ''}"),
        "legacy": _hash(f"{uuid}-{disk_legacy or ''}"),
        "fallback": False,
        "smbios_uuid": _clean_smbios_uuid(uuid),
    }
    _CACHED_HWID_INFO = res
    return res


def get_hwid():
    """HWID chính của máy (bản ổn định)."""
    try:
        return get_hwid_info()["stable"]
    except Exception as e:
        import sys
        print(f"[HWID DEBUG] get_hwid failed: {e}", file=sys.stderr, flush=True)
        return _fallback_hwid()
