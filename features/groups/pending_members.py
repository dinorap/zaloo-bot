"""Duyệt tự động thành viên chờ (pending members) của nhóm Zalo.

Bối cảnh:
- Khi nhóm bật chế độ phê duyệt thành viên (``joinAppr: 1``), người lạ bấm
  link tham gia nhóm sẽ KHÔNG vào nhóm ngay mà nằm ở hàng đợi chờ duyệt.
- Nếu không ai duyệt, họ kẹt vĩnh viễn ở trạng thái "chờ" dù đã bấm link.
- Hai API dưới đây cho phép tự động đóng nốt vòng đó:
    GET  /api/group/pending-mems/list    -> danh sách UID đang chờ duyệt
    GET  /api/group/pending-mems/review  -> duyệt (isApprove=1) hoặc từ chối

Đây là cơ chế ĐẦU TIÊN và DUY NHẤT giúp người lạ (chưa kết bạn) vào được
nhóm mà không cần họ chấp nhận lời mời kết bạn trước.

Tham chiếu: zca-js ``getPendingGroupMembers`` / ``reviewPendingMemberRequest``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from core.zalo import http as zalo_http
from core.zalo.dec import zalo_decode
from core.zalo.enc import zalo_encode
from core.zalo.zalo_config import get_zpw_ver
from core.zalo.zalo_headers import zalo_mobile_headers

PENDING_LIST_URL = "https://tt-group-wpa.chat.zalo.me/api/group/pending-mems/list"
PENDING_REVIEW_URL = "https://tt-group-wpa.chat.zalo.me/api/group/pending-mems/review"

# Mã trạng thái trả về từ /pending-mems/review.
REVIEW_STATUS_SUCCESS = 0
REVIEW_STATUS_NOT_IN_PENDING_LIST = 170
REVIEW_STATUS_ALREADY_IN_GROUP = 178
REVIEW_STATUS_INSUFFICIENT_PERMISSION = 166

# Mã lỗi ở cấp request (nằm ở error_code chung, không phải map từng UID).
ERROR_CODE_NOT_LEADER = 166
ERROR_CODE_GROUP_NOT_FOUND = 132


def _cookie_dict(cookies: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in str(cookies or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _decoded_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"raw": value}
        except Exception:
            return {"raw": value}
    return {}


def _call_pending_api(
    url: str,
    payload: dict,
    zpw_enk: str,
    cookies: str,
    *,
    zpw_ver: str | None = None,
    timeout: int = 30,
) -> tuple[dict, dict]:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    params = zalo_encode(plaintext, zpw_enk, url_encode=True)
    request_url = (
        f"{url}?zpw_ver={get_zpw_ver(zpw_ver)}"
        f"&zpw_type=30&params={params}"
    )
    response = zalo_http.get(
        request_url,
        headers=zalo_mobile_headers(),
        cookies=_cookie_dict(cookies),
        timeout=timeout,
    )
    response.raise_for_status()
    response_json = response.json()
    data_field = response_json.get("data", "")
    if isinstance(data_field, str) and data_field:
        decoded = zalo_decode(data_field, zpw_enk)
    else:
        decoded = data_field
    return response_json, _decoded_dict(decoded)


def _extract_pending_users(decoded: dict) -> list[dict]:
    """Lấy danh sách UID đang chờ duyệt từ response của ``pending-mems/list``.

    Response thực tế: ``{"time": <ms>, "users": [{"uid": "...", "dpn": "..."}]}``
    Nhưng Zalo có thể bọc thêm một lớp ``data`` nên thử cả hai.
    """
    candidates: list[Any] = [decoded]
    inner = decoded.get("data")
    if isinstance(inner, dict):
        candidates.insert(0, inner)

    for source in candidates:
        users = source.get("users") if isinstance(source, dict) else None
        if not isinstance(users, list):
            continue
        result: list[dict] = []
        seen: set[str] = set()
        for item in users:
            if isinstance(item, dict):
                uid = str(item.get("uid") or item.get("userId") or item.get("id") or "").strip()
                name = str(item.get("dpn") or item.get("displayName") or item.get("zaloName") or "").strip()
            else:
                uid = str(item or "").strip()
                name = ""
            if not uid or uid in seen:
                continue
            seen.add(uid)
            result.append({"userId": uid, "displayName": name})
        if result:
            return result
    return []


def list_pending_members(
    grid: str,
    imei: str,
    zpw_enk: str,
    cookies: str,
    *,
    zpw_ver: str | None = None,
    timeout: int = 30,
) -> dict:
    """Đọc danh sách thành viên đang chờ duyệt của nhóm.

    Trả về ``{"ok", "pending", "errorCode", "errorMessage", "response", "decoded"}``.
    """
    grid = str(grid or "").strip()
    if not grid:
        return {"ok": False, "pending": [], "errorCode": -1, "errorMessage": "Thiếu groupId."}

    try:
        response_json, decoded = _call_pending_api(
            PENDING_LIST_URL,
            {"grid": grid, "imei": str(imei or "")},
            zpw_enk,
            cookies,
            zpw_ver=zpw_ver,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "ok": False,
            "pending": [],
            "errorCode": -1,
            "errorMessage": str(exc),
            "response": {},
            "decoded": {},
        }

    error_code = -1
    try:
        error_code = int(decoded.get("error_code", response_json.get("error_code", -1)))
    except (TypeError, ValueError):
        error_code = -1
    error_message = str(
        decoded.get("error_message")
        or decoded.get("message")
        or response_json.get("error_message")
        or response_json.get("message")
        or ""
    ).strip()

    if error_code != 0:
        return {
            "ok": False,
            "pending": [],
            "errorCode": error_code,
            "errorMessage": error_message
            or f"Zalo trả về mã lỗi {error_code} khi đọc danh sách chờ duyệt.",
            "response": response_json,
            "decoded": decoded,
        }

    return {
        "ok": True,
        "pending": _extract_pending_users(decoded),
        "errorCode": 0,
        "errorMessage": "",
        "response": response_json,
        "decoded": decoded,
    }


def review_pending_members(
    grid: str,
    members: Iterable[str],
    imei: str,
    zpw_enk: str,
    cookies: str,
    *,
    approve: bool = True,
    zpw_ver: str | None = None,
    timeout: int = 30,
) -> dict:
    """Duyệt (hoặc từ chối) một danh sách UID đang chờ vào nhóm.

    Trả về ``{"ok", "results", "approved", "failed", "errorCode", "errorMessage"}``
    với ``results`` là map ``{uid: status_code}``.
    """
    grid = str(grid or "").strip()
    clean: list[str] = []
    seen: set[str] = set()
    for raw_uid in members or []:
        uid = str(raw_uid or "").strip()
        if uid and uid not in seen:
            seen.add(uid)
            clean.append(uid)

    if not grid or not clean:
        return {
            "ok": False,
            "results": {},
            "approved": [],
            "failed": [],
            "errorCode": -1,
            "errorMessage": "Thiếu groupId hoặc danh sách thành viên cần duyệt.",
        }

    try:
        response_json, decoded = _call_pending_api(
            PENDING_REVIEW_URL,
            {
                "grid": grid,
                "members": clean,
                "isApprove": 1 if approve else 0,
            },
            zpw_enk,
            cookies,
            zpw_ver=zpw_ver,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "ok": False,
            "results": {},
            "approved": [],
            "failed": clean,
            "errorCode": -1,
            "errorMessage": str(exc),
        }

    error_code = -1
    try:
        error_code = int(decoded.get("error_code", response_json.get("error_code", -1)))
    except (TypeError, ValueError):
        error_code = -1
    error_message = str(
        decoded.get("error_message")
        or decoded.get("message")
        or response_json.get("error_message")
        or response_json.get("message")
        or ""
    ).strip()

    # Response dạng map: { "<uid>": <status_code> }
    raw_map = decoded.get("data")
    if not isinstance(raw_map, dict):
        raw_map = decoded if all(str(k).isdigit() for k in decoded.keys()) else {}

    results: dict[str, int] = {}
    for uid in clean:
        value = raw_map.get(uid)
        if value is None:
            # Một số phiên bản bọc map trong "membersMap"/"results".
            for key in ("membersMap", "results", "statusMap"):
                nested = decoded.get(key)
                if isinstance(nested, dict) and uid in nested:
                    value = nested.get(uid)
                    break
        try:
            results[uid] = int(value) if value is not None else -1
        except (TypeError, ValueError):
            results[uid] = -1

    approved = [uid for uid, code in results.items() if code == REVIEW_STATUS_SUCCESS]
    failed = [uid for uid, code in results.items() if code != REVIEW_STATUS_SUCCESS]

    return {
        "ok": error_code == 0 and not failed,
        "results": results,
        "approved": approved,
        "failed": failed,
        "errorCode": error_code,
        "errorMessage": error_message,
        "response": response_json,
        "decoded": decoded,
    }


def approve_pending_members(
    grid: str,
    imei: str,
    zpw_enk: str,
    cookies: str,
    *,
    only_uids: Iterable[str] | None = None,
    zpw_ver: str | None = None,
    timeout: int = 30,
) -> dict:
    """Tiện ích: đọc danh sách chờ duyệt rồi duyệt hết.

    ``only_uids`` giới hạn chỉ duyệt những UID thuộc chiến dịch đang chạy, tránh
    duyệt nhầm yêu cầu của người khác trong cùng nhóm.
    """
    listed = list_pending_members(
        grid,
        imei,
        zpw_enk,
        cookies,
        zpw_ver=zpw_ver,
        timeout=timeout,
    )
    if not listed.get("ok"):
        return {
            "ok": False,
            "pending": [],
            "approved": [],
            "failed": [],
            "errorCode": listed.get("errorCode", -1),
            "errorMessage": listed.get("errorMessage", ""),
            "action": "list",
        }

    pending = listed.get("pending") or []
    if only_uids is not None:
        allowed = {str(u).strip() for u in only_uids if str(u or "").strip()}
        pending = [item for item in pending if item.get("userId") in allowed]

    if not pending:
        return {
            "ok": True,
            "pending": [],
            "approved": [],
            "failed": [],
            "errorCode": 0,
            "errorMessage": "",
            "action": "list",
        }

    reviewed = review_pending_members(
        grid,
        [item.get("userId") for item in pending],
        imei,
        zpw_enk,
        cookies,
        approve=True,
        zpw_ver=zpw_ver,
        timeout=timeout,
    )
    reviewed["pending"] = pending
    reviewed["action"] = "review"
    return reviewed
