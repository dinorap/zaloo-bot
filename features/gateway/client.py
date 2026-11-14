"""Client SDK tai su dung cho may Client goi ve Host.

Khong bao gio cam cookies/zpwEnk that. Moi request mang X-Internal-Token.
Giong cach aff_webhook goi nexus_get / nexus_post_form.

Toi uu: dung connection pool (core.zalo.http) thay ``requests`` moi lan ->
TLS handshake chi mot lan cho ca client goi lien tuc Host.
"""
import os

from core.zalo import http as zalo_http

HOST_URL = os.environ.get("NEXUS_HOST_URL", "http://127.0.0.1:5000").rstrip("/")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")


def configure(host_url: str = "", token: str = ""):
    global HOST_URL, INTERNAL_TOKEN
    if host_url:
        HOST_URL = host_url.rstrip("/")
    if token:
        INTERNAL_TOKEN = token


def _headers(extra: dict | None = None) -> dict:
    h = {"X-Internal-Token": INTERNAL_TOKEN}
    if extra:
        h.update(extra)
    return h


class GatewayError(RuntimeError):
    pass


def nexus_get(path: str, params: dict | None = None, timeout: int = 30) -> dict:
    try:
        r = zalo_http.get(HOST_URL + path, params=params or {},
                          headers=_headers(), timeout=timeout)
    except Exception as e:
        raise GatewayError(f"Khong noi duoc Host {HOST_URL}: {e}")
    try:
        data = r.json()
    except Exception:
        raise GatewayError(f"Host tra ve khong phai JSON (HTTP {r.status_code}): {r.text[:200]}")
    if r.status_code in (401, 403):
        raise GatewayError(f"Auth loi ({r.status_code}): {data.get('error') if isinstance(data, dict) else r.text[:200]}")
    return data


def nexus_post(path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    try:
        r = zalo_http.post(HOST_URL + path, json=payload or {},
                           headers=_headers({"Content-Type": "application/json"}),
                           timeout=timeout)
    except Exception as e:
        raise GatewayError(f"Khong noi duoc Host {HOST_URL}: {e}")
    try:
        data = r.json()
    except Exception:
        raise GatewayError(f"Host tra ve khong phai JSON (HTTP {r.status_code})")
    if r.status_code in (401, 403, 429):
        raise GatewayError(f"Gateway {r.status_code}: {(data or {}).get('error') if isinstance(data, dict) else ''}")
    return data


def nexus_post_form(path: str, data: dict | None = None, files: dict | None = None, timeout: int = 120) -> dict:
    try:
        r = zalo_http.post(HOST_URL + path, data=data or {}, files=files,
                           headers=_headers(), timeout=timeout)
    except Exception as e:
        raise GatewayError(f"Khong noi duoc Host {HOST_URL}: {e}")
    try:
        payload = r.json()
    except Exception:
        raise GatewayError(f"Host tra ve khong phai JSON (HTTP {r.status_code})")
    return payload


# --- API thuan tien ---

def session_health(account_id: str = "") -> dict:
    return nexus_get("/api/gateway/session/health", {"accountId": account_id} if account_id else {})
