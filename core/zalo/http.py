"""HTTP client dùng chung cho các API Zalo.

Vì sao cần module này:
- ``requests.get``/``requests.post`` tạo một connection TCP + TLS **mới** cho mỗi
  lần gọi. Chiến dịch sao chép nhóm gọi hàng trăm API liên tiếp (đọc thành
  viên, gửi thẻ link, gửi lời mời kết bạn, kiểm tra nhóm) nên phần lớn thời
  gian bị đốt vào bắt tay TLS. Dùng ``requests.Session`` với connection pool
  giữ lại kết nối giữa các lần gọi -> nhanh hơn rõ rệt.
- Cookie luôn được truyền theo từng request nên jar của session được giữ sạch,
  tránh lẫn cookie giữa hai tài khoản Zalo khác nhau.

Ngoài ra module hỗ trợ:
- ``retries`` + backoff co jitter (cho lỗi tam thoi 429/5xx/timeout).
- ``timeout`` mặc định dạng tuple ``(connect, read)`` -> ket noi tre khong
  lam treo ca request.
"""

from __future__ import annotations

import random
import threading
import time
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter

# Số connection tối đa giữ mở cùng lúc. Worker group-copy chạy song song với
# worker inbox nên con số này nhiều hơn số thread đơn lẻ.
_POOL_SIZE = 16

# Timeout mac dinh: (connect, read)
DEFAULT_TIMEOUT = (8, 25)

# Ma HTTP nem lai duoc: 429 (rate limit) va 5xx (server dap trong).
RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 522, 524})

_SESSION: requests.Session | None = None
_SESSION_LOCK = threading.Lock()


def get_session() -> requests.Session:
    """Trả về ``requests.Session`` dùng chung (có connection pool)."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=_POOL_SIZE,
                pool_maxsize=_POOL_SIZE,
                max_retries=0,      # tu retry thu cong de co backoff kiem soat
                pool_block=False,   # khong chan request neu het slot
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            # Không dùng cookie tự động của requests vì Zalo dùng cookie riêng.
            session.cookies.clear()
            # Không tự kế thừa proxy từ môi trường (tránh rò request).
            # Đặt NEXUS_TRUST_ENV=1 nếu máy này BẮT BUỘC cần proxy hệ thống
            # (ví dụ trong môi trường công ty có HTTP_PROXY).
            session.trust_env = str(
                os.environ.get("NEXUS_TRUST_ENV", "") or ""
            ).strip().lower() in ("1", "true", "yes", "on")
            _SESSION = session
    return _SESSION


def request(
    method: str,
    url: str,
    *,
    retries: int = 0,
    backoff: float = 0.6,
    timeout: Any = None,
    **kwargs: Any,
) -> requests.Response:
    """Gửi request qua session dùng chung, luôn kèm cookie theo lời gọi.

    Tham số đặc biệt:
        retries: số lần thử lại (mặc định 0 = không thử lại).
        backoff: thời gian chờ nền giữa các lần thử (nhân dần + jitter).
        timeout: mặc định ``DEFAULT_TIMEOUT`` dạng (connect, read).

    ``kwargs`` còn lại giống ``requests.request`` (headers, params, data,
    cookies, files...). Cookie truyền vào luôn được áp dụng cho đúng lời gọi đó.
    """
    session = get_session()
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    attempts = max(1, int(retries) + 1)
    last_exc: Exception | None = None

    for attempt in range(attempts):
        # Dọn jar trước khi gọi để không dùng nhầm cookie của tài khoản khác.
        session.cookies.clear()
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == attempts - 1:
                raise
        else:
            if resp.status_code not in RETRY_STATUS or attempt == attempts - 1:
                return resp
            last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            try:
                resp.close()
            except Exception:
                pass

        # Backoff lũy thừa + jitter để không bao giờ dồn request.
        delay = backoff * (2 ** attempt) + random.uniform(0, backoff)
        time.sleep(delay)

    if last_exc:
        raise last_exc
    raise requests.RequestException(f"request failed: {method} {url}")


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)


def close_session() -> None:
    """Đóng và bỏ session dùng chung (dùng khi tắt ứng dụng)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception:
                pass
            _SESSION = None
