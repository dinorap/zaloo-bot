"""Gateway config: token noi bo + whitelist LAN. Chi Host om session that."""
import os
import json
import re
import hmac
import hashlib
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "gateway.json")


def _load_file_cfg() -> dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {}


def _save_file_cfg(patch: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    cur = _load_file_cfg()
    cur.update(patch or {})
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return cur


def _bool_cfg(val, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(val)


def get_internal_token() -> str:
    # Ưu tiên token đã lưu trong data/gateway.json để người dùng đổi/xoay trực tiếp từ UI
    # kể cả khi .env đang đặt giá trị mặc định.
    f = _load_file_cfg()
    tok = str(f.get("internalToken") or "").strip()
    if tok:
        return tok
    tok = (os.environ.get("INTERNAL_TOKEN") or "").strip()
    if tok:
        return tok
    # tu sinh 1 lan, luu file (khong commit)
    tok = secrets.token_urlsafe(32)
    try:
        _save_file_cfg({"internalToken": tok})
    except Exception:
        pass
    return tok


def get_include_token_in_link() -> bool:
    """Có gắn sẵn ?token=... vào link share Inbox hay không (mặc định BẬT)."""
    try:
        f = _load_file_cfg()
        if "includeTokenInLink" in f:
            return _bool_cfg(f.get("includeTokenInLink"), True)
    except Exception:
        pass
    return True


def get_preset_staff_enabled() -> bool:
    """Có bật chế độ cấp sẵn tên nhân viên trong link share hay không."""
    try:
        f = _load_file_cfg()
        if "presetStaffEnabled" in f:
            return _bool_cfg(f.get("presetStaffEnabled"), False)
    except Exception:
        pass
    return False


def get_per_staff_password_enabled() -> bool:
    """True = mỗi nhân sự có 1 mật khẩu riêng; False = dùng chung 1 internalToken."""
    try:
        f = _load_file_cfg()
        if "perStaffPassword" in f:
            return _bool_cfg(f.get("perStaffPassword"), False)
    except Exception:
        pass
    return False


def get_preset_staff_name() -> str:
    """Tên nhân viên cấp sẵn (hoặc danh sách cách nhau dấu phẩy) khi bật chế độ khóa tên."""
    try:
        return str(_load_file_cfg().get("presetStaffName") or "").strip()
    except Exception:
        return ""


def parse_staff_names(raw) -> list:
    """Tách chuỗi hoặc danh sách tên nhân viên, giữ nguyên chữ hoa/thường và bỏ trùng."""
    if isinstance(raw, (list, tuple)):
        items = [str(x or "").strip() for x in raw]
    else:
        items = [x.strip() for x in re.split(r"[,;\r\n]+", str(raw or ""))]
    out = []
    seen = set()
    for name in items:
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(name)
    return out


def get_preset_staff_names() -> list:
    """Danh sách tên nhân viên được cấp link cố định."""
    try:
        f = _load_file_cfg()
        if isinstance(f.get("staffProfiles"), list) and f.get("staffProfiles"):
            names = [str((p or {}).get("name") or "").strip()
                     for p in f.get("staffProfiles") if isinstance(p, dict)]
            parsed = parse_staff_names(names)
            if parsed:
                return parsed
        if isinstance(f.get("presetStaffNames"), list):
            parsed = parse_staff_names(f.get("presetStaffNames"))
            if parsed:
                return parsed
        return parse_staff_names(f.get("presetStaffName") or "")
    except Exception:
        return []


def normalize_staff_role(role: str) -> str:
    """Chuẩn hóa vai trò nhân sự về 'manager' (Admin giám sát) hoặc 'sale'."""
    r = str(role or "").strip().lower()
    if r in ("admin", "manager", "director", "supervisor", "lead", "truong_phong", "giam_doc"):
        return "manager"
    return "sale"


def _clean_account_ids(raw) -> list:
    if isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[,;\r\n]+", raw) if x.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(x or "").strip() for x in raw if str(x or "").strip()]
    else:
        items = []
    out = []
    seen = set()
    for aid in items:
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


def normalize_staff_profiles(raw) -> list:
    """Chuẩn hóa danh sách phân quyền nhân sự [{name, role, accountIds, password}]."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            nm = item.strip()
            role = "sale"
            accs = []
            pw = ""
        elif isinstance(item, dict):
            nm = str(item.get("name") or item.get("staff") or "").strip()
            role = normalize_staff_role(item.get("role"))
            accs = _clean_account_ids(
                item.get("accountIds") if "accountIds" in item else item.get("accountId")
            )
            pw = str(item.get("password") or item.get("passcode") or "").strip()
        else:
            continue
        if not nm:
            continue
        low = nm.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append({"name": nm, "role": role, "accountIds": accs, "password": pw})
    return out


def get_staff_profiles() -> list:
    """Trả về danh sách hồ sơ nhân sự kèm role ('manager'|'sale') và accountIds phụ trách."""
    try:
        f = _load_file_cfg()
        profiles = normalize_staff_profiles(f.get("staffProfiles") or [])
        if profiles:
            return profiles
        legacy_names = get_preset_staff_names()
        out = []
        for nm in legacy_names:
            low = nm.strip().lower()
            r = "manager" if (low == "admin" or low.startswith("admin ")) else "sale"
            out.append({"name": nm, "role": r, "accountIds": []})
        return out
    except Exception:
        return []


def get_staff_profile(name: str):
    """Tìm hồ sơ phân quyền theo tên nhân sự (không phân biệt hoa/thường)."""
    nm = str(name or "").strip().lower()
    if not nm:
        return None
    for p in get_staff_profiles():
        if str(p.get("name") or "").strip().lower() == nm:
            return dict(p)
    return None


def get_staff_role(name: str) -> str:
    """Lấy vai trò của nhân sự từ cấu hình Gateway ('manager' hoặc 'sale')."""
    prof = get_staff_profile(name)
    if prof:
        return normalize_staff_role(prof.get("role"))
    return "sale"


def get_staff_account_ids(name: str) -> list:
    """Danh sách accountId Zalo được giao cho Sale (rỗng = theo cấu hình share chung)."""
    prof = get_staff_profile(name)
    if not prof:
        return []
    return list(prof.get("accountIds") or [])


def get_staff_password(name: str) -> str:
    """Mật khẩu riêng của nhân sự (nếu có)."""
    prof = get_staff_profile(name)
    if not prof:
        return ""
    return str(prof.get("password") or "").strip()


def verify_gateway_token(tok: str, staff_name: str = "") -> bool:
    """Kiểm tra mật khẩu truy cập theo chế độ 1 MK chung hoặc mỗi người 1 MK."""
    tok = str(tok or "").strip()
    if not tok:
        return False
    shared_tok = get_internal_token()
    if not get_per_staff_password_enabled():
        return bool(shared_tok and tok == shared_tok)
    nm = str(staff_name or "").strip()
    if nm:
        prof = get_staff_profile(nm)
        if prof is not None:
            pw = str(prof.get("password") or "").strip()
            if pw:
                return tok == pw
            return bool(shared_tok and tok == shared_tok)
        return False
    return bool(shared_tok and tok == shared_tok)


def save_staff_profiles(raw_profiles) -> list:
    """Lưu staffProfiles đồng thời đồng bộ presetStaffNames/presetStaffName cho link cũ."""
    profiles = normalize_staff_profiles(raw_profiles)
    names = [p["name"] for p in profiles]
    _save_file_cfg({
        "staffProfiles": profiles,
        "presetStaffNames": names,
        "presetStaffName": ", ".join(names),
    })
    return profiles


def sign_staff_name(name: str, token: str = "") -> str:
    """Tạo chữ ký HMAC ngắn cho từng tên nhân viên để mỗi người giữ 1 link cố định độc lập."""
    nm = str(name or "").strip()
    if not nm:
        return ""
    secret = str(token or get_internal_token() or "").strip()
    if not secret:
        return ""
    msg = f"staff:{nm}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def verify_staff_name(name: str, sig: str, token: str = "") -> bool:
    """Kiểm tra chữ ký HMAC của tên nhân viên."""
    nm = str(name or "").strip()
    got = str(sig or "").strip()
    if not nm or not got:
        return False
    expected = sign_staff_name(nm, token=token)
    return bool(expected) and hmac.compare_digest(expected, got)


def resolve_allowed_staff_name(name: str) -> str:
    """Trả về đúng tên chuẩn trong danh sách cho phép (nếu có cấu hình danh sách)."""
    nm = str(name or "").strip()
    if not nm:
        return ""
    allowed = get_preset_staff_names()
    if not allowed:
        return nm
    low = nm.lower()
    for entry in allowed:
        if entry.lower() == low:
            return entry
    return ""


def get_preferred_lan_ip() -> str:
    """IP LAN ưu tiên do Host chọn (nếu máy có nhiều card mạng)."""
    try:
        return str(_load_file_cfg().get("preferredLanIp") or "").strip()
    except Exception:
        return ""


def get_allowed_lan() -> list:
    raw = (os.environ.get("GATEWAY_ALLOW_LAN") or "").strip()
    f = _load_file_cfg()
    if not raw:
        raw = str(f.get("allowLan") or "").strip()
    if not raw:
        # mac dinh: loopback + LAN 192.168.x + 10.x
        return ["127.0.0.1", "::1", "192.168.", "10."]
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts or ["127.0.0.1"]


def get_send_gap() -> float:
    try:
        return max(0.5, float(os.environ.get("GATEWAY_SEND_GAP") or _load_file_cfg().get("sendGapSec") or 1.2))
    except Exception:
        return 1.2


def get_public_url() -> str:
    """URL công khai của Host (vd: https://xxx.ngrok-free.app hoặc http://IP:5000).

    Máy khách ở xa (khác tỉnh, vài trăm km) mở đúng URL này + nhập Token là vào.
    Non-code chỉ cần copy 1 link share duy nhất."""
    raw = (os.environ.get("NEXUS_PUBLIC_URL") or "").strip()
    if raw:
        return raw.rstrip("/")
    try:
        return str(_load_file_cfg().get("publicUrl") or "").strip().rstrip("/")
    except Exception:
        return ""


def get_allow_wan() -> bool:
    """Mặc định BẬT: cho phép IP lạ (WAN) miễn đúng Token.

    Giữ whitelist LAN chỉ để chặn request không có token."""
    try:
        f = _load_file_cfg()
        if "allowWan" in f:
            v = f.get("allowWan")
            if isinstance(v, str):
                return v.strip().lower() not in ("0", "false", "no", "off")
            return bool(v)
    except Exception:
        pass
    return True


def get_shared_account_ids() -> list:
    """Danh sách accountId được Host tick share cho máy khách.

    Rỗng = share acc đang chạy trong Inbox (mặc định, dễ dùng nhất cho non-code)."""
    try:
        v = _load_file_cfg().get("sharedAccountIds") or []
        if isinstance(v, str):
            v = [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
        return [str(x).strip() for x in (v or []) if str(x).strip()]
    except Exception:
        return []


def is_loopback(ip: str) -> bool:
    ip = (ip or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost")
