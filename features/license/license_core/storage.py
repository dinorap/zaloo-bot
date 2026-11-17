import json
import os
import time
import hmac
import hashlib
import base64

OBFUSCATED_SECRET = "U0VQX0RFUF9UUkFJX1ZPX0RPSV9QUk9fTUFYXzIwMjU="

def _decode_secret(obfuscated: str) -> str:
    try:
        return base64.b64decode(obfuscated.encode('utf-8')).decode('utf-8')
    except Exception:
        return "SEP_DEP_TRAI_VO_DOI_PRO_MAX_2025"

def _is_sha256_hex(value: str) -> bool:
    v = (value or "").strip().lower()
    if len(v) != 64:
        return False
    return all(c in "0123456789abcdef" for c in v)

def _resolve_signature_key() -> str:
    env_hash = (os.getenv("SIGNATURE_SECRET_HASH") or "").strip().lower()
    if _is_sha256_hex(env_hash):
        return env_hash

    env_secret = os.getenv("SIGNATURE_SECRET")
    if env_secret:
        return hashlib.sha256(env_secret.encode("utf-8")).hexdigest()

    fallback_secret = _decode_secret(OBFUSCATED_SECRET)
    return hashlib.sha256(fallback_secret.encode("utf-8")).hexdigest()

SIGNATURE_SECRET = _resolve_signature_key()


class LicenseStorage:
    def __init__(self, file_path="mkt_zalo.lic"):
        self.file_path = file_path

    def _sign_data(self, data_str):
        return hmac.new(
            SIGNATURE_SECRET.encode('utf-8'), 
            data_str.encode('utf-8'), 
            hashlib.sha256
        ).hexdigest()

    def _read_file(self):
        if not os.path.exists(self.file_path):
            return None
        try:
            with open(self.file_path, "r", encoding='utf-8') as f:
                wrapped_data = json.load(f)
            
            payload_str = wrapped_data.get("payload", "")
            signature = wrapped_data.get("signature", "")
            
            expected_sig = self._sign_data(payload_str)
            if not hmac.compare_digest(expected_sig, signature):
                print("⚠️ Cảnh báo: File license đã bị sửa đổi trái phép!")
                return None
                
            return json.loads(payload_str)
        except Exception:
            return None

    def _write_file(self, data):
        try:
            payload_str = json.dumps(data, sort_keys=True)
            signature = self._sign_data(payload_str)
            final_content = {
                "payload": payload_str,
                "signature": signature
            }
            tmp_path = f"{self.file_path}.{os.getpid()}.tmp"
            os.makedirs(os.path.dirname(os.path.abspath(self.file_path)) or ".", exist_ok=True)
            with open(tmp_path, "w", encoding='utf-8') as f:
                json.dump(final_content, f, indent=4)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            print(f"Lỗi ghi file license: {e}")

    def load_full_data(self):
        return self._read_file()

    def load_server_url(self):
        data = self._read_file()
        if data and isinstance(data, dict):
            return data.get("server_url")
        return None

    def save_server_url(self, server_url: str):
        current = self._read_file()
        if not isinstance(current, dict):
            current = {}
        current["server_url"] = server_url
        self._write_file(current)

    def load_key_only(self):
        data = self._read_file()
        if data and isinstance(data, dict):
            return data.get("license_key")
        return None

    def save(self, license_key):
        data = self._read_file()
        if not isinstance(data, dict):
            data = {}
        data.update({
            "license_key": license_key,
            "updated_at": time.time()
        })
        self._write_file(data)

    def save_signed_cache(self, license_key, hwid, machine, key_id, sig_payload, sig, signed, loc_t):
        data = self._read_file()
        if not isinstance(data, dict):
            data = {}
        data.pop("machine_guid", None)
        data.update({
            "license_key": license_key,
            "hwid": hwid,
            "machine": machine,
            "key_id": key_id,
            "sig_payload": sig_payload,
            "sig": sig,
            "session_token": signed.get("session_token") or data.get("session_token") or "",
            "loc_t": loc_t,
            "last_seen": max(float(data.get("last_seen", 0) or 0), loc_t),
            "last_check": loc_t,
            "server_data": signed,
            "offline_used": 0,
        })
        self._write_file(data)

    def save_session_token(self, token: str):
        data = self._read_file()
        if not isinstance(data, dict) or not token:
            return
        data["session_token"] = token
        self._write_file(data)

    def save_offline_use(self, offline_used: float, last_seen: float):
        data = self._read_file()
        if not isinstance(data, dict):
            return
        data["offline_used"] = float(offline_used)
        data["last_seen"] = max(float(data.get("last_seen", 0) or 0), float(last_seen))
        self._write_file(data)
            
    def clear(self):
        data = self._read_file()
        server_url = None
        if data and isinstance(data, dict):
            server_url = data.get("server_url")
        if os.path.exists(self.file_path):
            try:
                os.remove(self.file_path)
            except Exception:
                pass
        if server_url:
            new_data = {"server_url": server_url}
            self._write_file(new_data)
