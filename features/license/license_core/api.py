# features/license/license_core/api.py
import json
import requests
import secrets
import time
import threading
import socket
import os
from .hwid import get_hwid_info, get_machine_guid, legacy_fallback_hwid
from .license_sig_verify import verify_signed_response, SignatureError
from .storage import LicenseStorage

ERROR_MAP = {
    "hwid_mismatch": "❌ Key này đã kích hoạt trên máy khác! Hệ thống chỉ cho phép 1 máy 1 key.",
    "expired": "⚠️ Key đã hết hạn sử dụng.",
    "revoked": "🚫 Key đã bị Admin thu hồi.",
    "license_not_found": "❌ Key không tồn tại trên hệ thống.",
    "license_not_activated": "⚠️ Key chưa được kích hoạt.",
    "server_error": "🔌 Lỗi kết nối Server.",
    "network_error": "📡 Không có mạng Internet.",
    "no_key": "Chưa nhập key kích hoạt.",
    "product_mismatch": "❌ Key này dành cho sản phẩm khác.",
    "invalid_product_id": "❌ Key không dùng được cho sản phẩm này.",
    "license_inactive": "⚠️ Key đang bị tạm ngưng.",
    "max_devices_reached": "❌ Key đã dùng hết số máy cho phép.",
    "server_rejected": "❌ Server từ chối xác thực key.",
    "session_conflict": "❌ Key đang được dùng trên máy khác. Vui lòng liên hệ admin.",
    "session_required": "❌ Key đang được dùng trên máy khác. Vui lòng liên hệ admin.",
    "unknown": "❌ Key không hợp lệ.",
}

CLEAR_KEY_REASONS = ('revoked', 'expired', 'product_mismatch', 'invalid_product_id')
SESSION_REASONS = ('session_conflict', 'session_required')

OFFLINE_GRACE_SEC = 7200
CLOCK_SKEW_TOLERANCE_SEC = 300
OFFLINE_START_COST_SEC = 300
CLOCK_DRIFT_LOG_SEC = 600

_last_offline_mono = None


def _reset_offline_clock():
    global _last_offline_mono
    _last_offline_mono = None


def _log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def _json_or_none(response):
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class LicenseGuard:
    def __init__(self, server_url=None, product_id="mkt_zalo", allow_offline=False):
        self.product_id = product_id
        hwid_info = get_hwid_info()
        self.hwid = hwid_info["stable"]
        self.hwid_legacy = hwid_info["legacy"]
        self.hwid_is_fallback = hwid_info["fallback"]
        self.machine_guid = get_machine_guid()
        self.smbios_uuid = hwid_info.get("smbios_uuid")
        
        # Đường dẫn file .lic trong thư mục data/ của tool
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        lic_path = os.path.join(base_dir, "data", f"{product_id}.lic")
        self.storage = LicenseStorage(lic_path)

        DEFAULT_SERVER_URL = "https://license.fastst.cloud/"
        stored_url = self.storage.load_server_url()
        base_url = (server_url or stored_url or DEFAULT_SERVER_URL).strip()
        self.server_url = base_url.rstrip("/") if base_url else DEFAULT_SERVER_URL

        self.last_signed = None
        self._periodic_thread = None
        self._periodic_stop_event = None
        self._on_license_fail = None

    def _get_msg(self, code):
        return ERROR_MAP.get(code, f"Lỗi không xác định ({code})")

    def _machine(self):
        return {"guid": self.machine_guid or "", "smbios_uuid": self.smbios_uuid or ""}

    def _machine_matches(self, data):
        machine = data.get("machine") or {}
        guid = machine.get("guid") or data.get("machine_guid")
        if not (self.machine_guid and guid == self.machine_guid):
            return False
        stored_uuid = machine.get("smbios_uuid")
        if stored_uuid and self.smbios_uuid and stored_uuid != self.smbios_uuid:
            return False
        return True

    def _hwid_candidates(self):
        candidates = []
        try:
            data = self.storage.load_full_data() or {}
        except Exception:
            data = {}
        cached = data.get("hwid")

        if cached and (
            self._machine_matches(data)
            or (not self.hwid_is_fallback and cached in (self.hwid, self.hwid_legacy))
            or cached == legacy_fallback_hwid()
        ):
            candidates.append(cached)

        if not self.hwid_is_fallback:
            candidates.extend([self.hwid, self.hwid_legacy])
        else:
            candidates.append(self.hwid)

        seen = set()
        return [h for h in candidates if h and not (h in seen or seen.add(h))]

    def _stored_token(self, license_key):
        data = self.storage.load_full_data() or {}
        if data.get("license_key") != license_key:
            return ""
        return data.get("session_token") or ""

    def _post_signed(self, path, endpoint, license_key, hwid, session_token, extra=None, timeout=12):
        nonce = secrets.token_hex(16)
        payload = {
            "license_key": license_key,
            "hwid": hwid,
            "product_id": self.product_id,
            "nonce": nonce,
            "session_token": session_token,
        }
        if extra:
            payload.update(extra)
        response = requests.post(f"{self.server_url}{path}", json=payload, timeout=timeout)
        body = _json_or_none(response)
        signed = None
        if body is not None:
            try:
                signed = verify_signed_response(
                    body, endpoint=endpoint, license_key=license_key,
                    hwid=hwid, product_id=self.product_id, nonce=nonce,
                )
            except SignatureError as e:
                _log(f"[License] Phản hồi {endpoint} không có chữ ký hợp lệ: {e}")
        if signed is not None and signed.get("session_token"):
            self.storage.save_session_token(signed["session_token"])
        return response, body, signed

    def _save_signed(self, license_key, hwid, body, signed):
        loc_t = time.time()
        server_time = signed.get("server_time")
        if isinstance(server_time, (int, float)) and abs(loc_t - server_time) > CLOCK_DRIFT_LOG_SEC:
            _log(f"[License] Giờ máy lệch giờ server {int(loc_t - server_time)}s")
        self.storage.save_signed_cache(
            license_key, hwid, self._machine(), body.get("key_id"),
            body.get("sig_payload"), body.get("sig"), signed, loc_t,
        )
        self.last_signed = signed
        _reset_offline_clock()

    def check_license(self):
        """
        Hàm chính: Server luôn là nguồn xác thực chính (check trước).
        Cache cục bộ CHỈ dùng làm ân hạn tạm thời khi không gọi được server.
        """
        saved_key = self.storage.load_key_only()
        if not saved_key:
            return {"ok": False, "reason": "no_key", "message": self._get_msg("no_key")}

        try:
            last_rejection = None
            token = self._stored_token(saved_key)

            for hwid in self._hwid_candidates():
                response, body, signed = self._post_signed(
                    "/license/verify", "verify", saved_key, hwid, token)
                if signed is not None:
                    token = signed.get("session_token") or token
                    if signed.get("ok"):
                        self._save_signed(saved_key, hwid, body, signed)
                        expire_at = signed.get("expire_at") or "Vĩnh viễn"
                        ui_mode = signed.get("ui_mode") or 1
                        return {
                            "ok": True,
                            "message": f"Active (Online) - Hạn: {expire_at}",
                            "expire_at": signed.get("expire_at"),
                            "ui_mode": ui_mode,
                            "signed": signed,
                        }

                    reason = signed.get("reason") or "unknown"
                    last_rejection = (reason, signed.get("message"), True)
                    if reason == 'hwid_mismatch':
                        continue
                    break

                if response.status_code == 200:
                    raise RuntimeError("unsigned_response")

                if body and (body.get('reason') or body.get('detail')):
                    reason = body.get('reason') or 'server_rejected'
                    last_rejection = (reason, body.get('detail'), False)
                    if reason == 'hwid_mismatch':
                        continue
                    break

                raise RuntimeError(f"server_http_{response.status_code}")

            if last_rejection:
                reason, detail, is_signed = last_rejection
                if is_signed and reason in CLEAR_KEY_REASONS:
                    self.storage.clear()
                return {"ok": False, "reason": reason, "message": detail or self._get_msg(reason)}

            raise RuntimeError("server_no_response")

        except Exception as e:
            cached_data = self.storage.load_full_data()
            signed = self._validate_cache(cached_data) if cached_data else None
            if signed is not None and self._charge_offline_use(cached_data):
                expire_info = signed.get("expire_at") or "Vĩnh viễn"
                ui_mode = signed.get("ui_mode") or 1
                self.last_signed = signed
                return {
                    "ok": True,
                    "message": f"Active (Offline Grace) - Hạn: {expire_info}",
                    "expire_at": signed.get("expire_at"),
                    "ui_mode": ui_mode,
                    "signed": signed,
                }
            return {"ok": False, "reason": "network_error", "message": self._get_msg("network_error") + f" ({str(e)})"}

    def activate(self, license_key):
        """
        Kích hoạt key với server:
        1. Verify trước (nếu cùng máy).
        2. Kích hoạt và gán HWID vào key trên server.
        """
        clean_key = (license_key or "").strip()
        if not clean_key:
            return False, "Vui lòng nhập key bản quyền.", None

        try:
            candidates = self._hwid_candidates()
            if self.hwid not in candidates:
                candidates.insert(0, self.hwid)

            # 1. Thử Verify trước
            token = self._stored_token(clean_key)
            verify_failed = False
            for hwid_try in candidates:
                try:
                    _, body, signed = self._post_signed(
                        "/license/verify", "verify", clean_key, hwid_try, token)
                except requests.exceptions.RequestException:
                    verify_failed = True
                    break
                if signed is None:
                    continue
                token = signed.get("session_token") or token
                if signed.get("ok"):
                    self._save_signed(clean_key, hwid_try, body, signed)
                    return True, "Kích hoạt thành công!", signed
                if signed.get("reason") in SESSION_REASONS:
                    return False, self._get_msg(signed["reason"]), None

            # 2. Activate
            last_err = None
            for hwid_try in candidates:
                try:
                    response, body, signed = self._post_signed(
                        "/license/activate", "activate", clean_key, hwid_try, "",
                        extra={"device_name": socket.gethostname()}, timeout=15)
                except Exception as net_err:
                    last_err = f"🔌 Lỗi kết nối mạng: {net_err}"
                    continue

                if signed is not None:
                    if signed.get("ok"):
                        self._save_signed(clean_key, hwid_try, body, signed)
                        return True, "Kích hoạt thành công!", signed
                    reason = signed.get('reason') or 'unknown'
                    msg = signed.get('message') or self._get_msg(reason)
                    if reason == 'hwid_mismatch':
                        last_err = msg
                        continue
                    return False, msg, None

                if response.status_code == 200:
                    last_err = "❌ Phản hồi kích hoạt không hợp lệ (không có chữ ký server)."
                    continue

                detail = (body or {}).get('detail') or f"Lỗi Server ({response.status_code})"
                if "máy khác" in str(detail).lower() or "hwid" in str(detail).lower():
                    last_err = detail
                    continue
                return False, detail, None

            if last_err:
                return False, str(last_err), None
            if verify_failed:
                return False, "🔌 Không thể kết nối Server bản quyền. Vui lòng kiểm tra Internet.", None
            return False, "Không thể kích hoạt. Vui lòng kiểm tra lại kết nối mạng.", None

        except requests.exceptions.Timeout:
            return False, "⏳ Hết thời gian chờ (Timeout). Vui lòng thử lại!", None
        except requests.exceptions.ConnectionError:
            return False, "🔌 Không thể kết nối Server bản quyền. Vui lòng kiểm tra Internet.", None
        except Exception as e:
            return False, f"❌ Lỗi hệ thống: {str(e)}", None

    def _charge_offline_use(self, data):
        global _last_offline_mono
        now_mono = time.monotonic()
        if _last_offline_mono is None:
            spent = OFFLINE_START_COST_SEC
        else:
            spent = max(0.0, now_mono - _last_offline_mono)
        _last_offline_mono = now_mono

        used = float(data.get("offline_used", 0) or 0) + spent
        self.storage.save_offline_use(used, time.time())
        return used < OFFLINE_GRACE_SEC

    def _validate_cache(self, data):
        try:
            sig_payload = data.get("sig_payload")
            if not sig_payload:
                return None
            unverified = json.loads(sig_payload)
            signed = verify_signed_response(
                {"sig_payload": sig_payload, "sig": data.get("sig"), "key_id": data.get("key_id")},
                endpoint=unverified.get("ep"),
                license_key=data.get("license_key"),
                hwid=data.get("hwid"),
                product_id=self.product_id,
                nonce=unverified.get("nonce"),
            )
            if signed.get("ep") not in ("verify", "activate") or not signed.get("ok"):
                return None

            if signed.get("hwid") not in self._hwid_candidates():
                return None

            now = time.time()
            loc_t = float(data.get("loc_t", 0) or 0)
            last_seen = max(float(data.get("last_seen", 0) or 0), loc_t)

            if now < last_seen - CLOCK_SKEW_TOLERANCE_SEC:
                return None

            age = now - loc_t
            if not (0 <= age < OFFLINE_GRACE_SEC):
                return None
            if float(data.get("offline_used", 0) or 0) >= OFFLINE_GRACE_SEC:
                return None

            expire_ts = signed.get("expire_ts")
            server_time = signed.get("server_time")
            if not isinstance(server_time, (int, float)):
                return None
            if expire_ts is not None and server_time + age >= float(expire_ts):
                return None

            return signed
        except Exception:
            return None

    def start_periodic_check(self, interval_hours=2, on_fail=None):
        """
        Bắt đầu check license định kỳ trong background thread (mặc định 2 giờ/lần).
        """
        if self._periodic_thread is not None and self._periodic_thread.is_alive():
            return False
        
        self._on_license_fail = on_fail
        self._periodic_stop_event = threading.Event()
        
        def _periodic_check_loop():
            interval_seconds = max(60, int(interval_hours * 3600))
            _log(f"[License-Periodic] Đã khởi động luồng kiểm tra bản quyền định kỳ mỗi {interval_hours}h.")
            while not self._periodic_stop_event.is_set():
                if self._periodic_stop_event.wait(timeout=interval_seconds):
                    break
                
                _log("[License-Periodic] Đang quét kiểm tra bản quyền định kỳ...")
                result = self.check_license()
                
                if not result.get('ok'):
                    reason = result.get('reason', 'unknown')
                    message = result.get('message', 'License check failed')
                    _log(f"[License-Periodic ALERT] Kiểm tra bản quyền thất bại: {reason} - {message}")
                    if self._on_license_fail:
                        try:
                            self._on_license_fail(reason, message)
                        except Exception as e:
                            _log(f"[License-Periodic] Lỗi callback: {e}")
                else:
                    _log(f"[License-Periodic OK] Key hợp lệ: {result.get('message')}")
        
        self._periodic_thread = threading.Thread(target=_periodic_check_loop, daemon=True, name="LicenseGuard-Periodic")
        self._periodic_thread.start()
        return True

    def stop_periodic_check(self):
        if self._periodic_stop_event:
            self._periodic_stop_event.set()
        if self._periodic_thread and self._periodic_thread.is_alive():
            self._periodic_thread.join(timeout=3)
        self._periodic_thread = None
        self._periodic_stop_event = None
