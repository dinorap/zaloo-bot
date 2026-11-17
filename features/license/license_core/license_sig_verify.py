"""
Kiểm chữ ký phản hồi của license server (license.fastst.cloud) - phía TOOL.
Python thuần, không cần thư viện ngoài (không cần cryptography/PyNaCl).
"""
import base64
import hashlib
import json

# Khóa công khai của server.
TRUSTED_KEYS = {
    "131b44fc3ed2175d": "FT1sD+OjHCYXmkHJP0frb2WPgVklKT9jPD8h5+ZNuw8=",
    "fbe3a7831d3c2353": "tmlDUThR08iclbAnleI1bc/Qhdx6N5ckrDs5SYBB1JE=",
}
SIGNATURE_VERSION = 1


class SignatureError(Exception):
    pass


# ── Ed25519 verify (RFC 8032, bản tham chiếu) ────────────────────────────────
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64:
        return False
    a = _decompress(public_key)
    r = _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# ── Kiểm phản hồi của server ─────────────────────────────────────────────────
def verify_signed_response(
    resp: dict,
    *,
    endpoint: str,
    license_key: str,
    hwid: str,
    product_id: str,
    nonce: str,
) -> dict:
    """Trả về payload ĐÃ KÝ (dict). Ném SignatureError nếu không tin được."""
    if not isinstance(resp, dict):
        raise SignatureError("phản hồi không phải JSON object")
    sig_payload, sig, kid = resp.get("sig_payload"), resp.get("sig"), resp.get("key_id")
    if not sig_payload or not sig:
        raise SignatureError("phản hồi không có chữ ký")
    pub_b64 = TRUSTED_KEYS.get(kid or "")
    if not pub_b64:
        raise SignatureError(f"khóa ký không tin cậy: {kid}")
    try:
        raw_sig = base64.b64decode(sig, validate=True)
        message = sig_payload.encode("ascii")
    except Exception as err:
        raise SignatureError(f"chữ ký sai định dạng: {err}") from err
    if not ed25519_verify(base64.b64decode(pub_b64), message, raw_sig):
        raise SignatureError("chữ ký không hợp lệ")

    payload = json.loads(sig_payload)
    expected = {
        "v": SIGNATURE_VERSION,
        "kid": kid,
        "ep": endpoint,
        "key": license_key,
        "hwid": hwid,
        "product": product_id,
        "nonce": nonce,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise SignatureError(f"payload sai trường {field}")
    return payload
