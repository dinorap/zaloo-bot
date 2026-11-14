"""Gateway auth: token noi bo + whitelist LAN. Chi Host om session that."""
from flask import request, jsonify
from functools import wraps
from .config import (
    get_internal_token,
    get_allowed_lan,
    is_loopback,
    get_allow_wan,
    get_preset_staff_enabled,
    get_preset_staff_name,
    verify_gateway_token,
)


def client_ip() -> str:
    for hdr in ("Cf-Connecting-Ip", "X-Real-IP", "X-Forwarded-For"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            return val.split(",")[0].strip()
    return (request.remote_addr or "").strip()


def _host_header_is_loopback() -> bool:
    raw = (request.host or "").strip().lower()
    if not raw:
        return True
    if raw.startswith("["):
        host = raw.split("]", 1)[0].lstrip("[")
    else:
        host = raw.split(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1")


def is_host_request() -> bool:
    """True chỉ khi request đến trực tiếp từ chính máy Host (loopback, không qua proxy/tunnel/LAN)."""
    remote = (request.remote_addr or "").strip()
    if not is_loopback(remote):
        return False
    for hdr in ("X-Forwarded-For", "Cf-Connecting-Ip", "X-Real-IP", "X-Forwarded-Host", "Forwarded"):
        if (request.headers.get(hdr) or "").strip():
            return False
    return _host_header_is_loopback()


def ip_allowed(ip: str) -> bool:
    ip = (ip or "").strip()
    if is_loopback(ip):
        return True
    for prefix in get_allowed_lan():
        p = (prefix or "").strip()
        if not p:
            continue
        if "/" in p:
            try:
                base, bits = p.split("/", 1)
                bits = int(bits)
                octets = base.split(".")
                if bits == 24 and ip.startswith(".".join(octets[:3]) + "."):
                    return True
                if bits == 16 and ip.startswith(".".join(octets[:2]) + "."):
                    return True
            except Exception:
                continue
        elif ip == p or ip.startswith(p):
            return True
    return False


def mask_secret(v: str) -> str:
    s = str(v or "")
    if len(s) <= 8:
        return "***"
    return s[:4] + "***" + s[-3:]


def gateway_auth_required(fn):
    """Loopback UI mien token, LAN/WAN xa bat buoc token.

    Cho phép máy khách ở xa (khác tỉnh, IP lạ) chỉ cần nhập đúng
    X-Internal-Token là vào được — không chặn cứng theo whitelist nữa.
    Whitelist chỉ dùng để chặn request KHÔNG có token.
    Tắt bằng allowWan=false trong data/gateway.json nếu chỉ muốn LAN.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = client_ip()
        if is_host_request():
            return fn(*args, **kwargs)
        tok = (request.headers.get("X-Internal-Token") or "").strip()
        staff_name = (request.headers.get("X-Staff-Name") or "").strip()
        if request.is_json:
            try:
                j = request.get_json(silent=True) or {}
                if not tok:
                    tok = str(j.get("token") or "").strip()
                if not staff_name:
                    staff_name = str(j.get("name") or j.get("staff") or "").strip()
            except Exception:
                pass
        if not tok:
            tok = (request.args.get("token") or request.form.get("token") or "").strip()
        if not staff_name:
            staff_name = (
                request.args.get("name")
                or request.args.get("staff")
                or request.form.get("name")
                or request.form.get("staff")
                or ""
            ).strip()
        # Token đúng -> cho qua bất kể IP nào (LAN hay WAN xa vài trăm km),
        # trừ khi Host tắt allowWan thì vẫn chặn IP lạ.
        locked_staff = get_preset_staff_name() if get_preset_staff_enabled() else ""
        if tok and verify_gateway_token(tok, staff_name):
            if get_allow_wan() or ip_allowed(ip):
                return fn(*args, **kwargs)
            return jsonify({"success": False, "error": f"IP {ip} không nằm trong mạng LAN và Host đang tắt truy cập từ xa.", "presetStaffName": locked_staff}), 403
        if not ip_allowed(ip):
            return jsonify({"success": False, "error": "Máy xa cần nhập đúng Mật khẩu để truy cập.", "presetStaffName": locked_staff}), 403
        return jsonify({"success": False, "error": "Sai mật khẩu truy cập.", "presetStaffName": locked_staff}), 401
    return wrapper
