"""Đồng bộ danh sách nhóm Zalo TRỰC TIẾP qua API (không cần mở Chrome).

Vì sao cần module này:
- Luồng cũ mở Chrome headless rồi chờ ``account_network_monitor`` bắt được
  request ``getlg/v4``. Việc đó mất 30-90 giây (mở trình duyệt, đăng nhập,
  chờ trang tải, chờ monitor bắt request). Người dùng phải ngồi chờ.
- Nhưng ``getlg/v4`` chỉ cần ``zpwEnk`` + ``cookies`` đã lưu. Gọi thẳng ra
  danh sách groupId trong ~0.3s (đo thực tế), sau đó lấy tên/avatar/số
  thành viên của từng nhóm bằng ``getmg`` với ``mcount=1`` (~0.03s/nhóm).

Tổng thời gian đồng bộ toàn bộ danh sách nhóm giảm từ hàng chục giây xuống
dưới 1 giây, và số thành viên luôn mới thay vì bị đóng băng theo lần sync
cũ.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core.zalo import http as zalo_http
from core.zalo.dec import zalo_decode
from core.zalo.enc import zalo_encode
from core.zalo.zalo_config import get_zpw_ver
from core.zalo.zalo_headers import zalo_mobile_headers

GETLG_URL = "https://tt-group-wpa.chat.zalo.me/api/group/getlg/v4"
GETMG_URL = "https://tt-group-wpa.chat.zalo.me/api/group/getmg"


def _cookie_dict(cookies: str) -> dict:
    result: dict = {}
    for item in str(cookies or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _decode(response_json: dict, zpw_enk: str) -> dict:
    """Giải mã trường ``data`` của response Zalo (có thể là JSON string)."""
    data = response_json.get("data") if isinstance(response_json, dict) else None
    decoded: Any = data
    if isinstance(data, str) and data:
        try:
            decoded = zalo_decode(data, zpw_enk)
        except Exception:
            decoded = {}
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except Exception:
            decoded = {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


def _post_encrypted(url: str, payload: dict, zpw_enk: str, cookies: str,
                    zpw_ver: str | None = None, timeout: int = 20) -> dict:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = zalo_encode(plaintext, zpw_enk, url_encode=True)
    response = zalo_http.post(
        url,
        params={"zpw_ver": get_zpw_ver(zpw_ver), "zpw_type": "30"},
        data=f"params={encoded}",
        headers=zalo_mobile_headers(),
        cookies=_cookie_dict(cookies),
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    response_json = response.json()
    if response_json.get("error_code", 0) not in (0, None):
        raise RuntimeError(
            str(response_json.get("error_message") or f"error_code={response_json.get('error_code')}")
        )
    return _decode(response_json, zpw_enk)


def fetch_group_ids(zpw_enk: str, cookies: str, zpw_ver: str | None = None,
                    timeout: int = 20) -> list[str]:
    """Lấy danh sách groupId từ ``getlg/v4``.

    Raises:
        RuntimeError: khi thiếu credential hoặc Zalo trả lỗi.
    """
    zpw_enk = str(zpw_enk or "").strip()
    cookies = str(cookies or "").strip()
    if not zpw_enk:
        raise RuntimeError("Thiếu zpwEnk")
    if not cookies:
        raise RuntimeError("Thiếu cookies")

    decoded = _post_encrypted(
        GETLG_URL,
        {},
        zpw_enk,
        cookies,
        zpw_ver=zpw_ver,
        timeout=timeout,
    )
    data = decoded.get("data")
    if not isinstance(data, dict):
        return []
    grid_ver_map = data.get("gridVerMap")
    if not isinstance(grid_ver_map, dict):
        return []

    seen = set()
    group_ids = []
    for group_id in grid_ver_map.keys():
        gid = str(group_id or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            group_ids.append(gid)
    return group_ids


def _extract_group_obj(data_obj: Any, group_id: str) -> dict:
    """``getmg`` có thể trả group object ở nhiều vị trí khác nhau."""
    if not isinstance(data_obj, dict):
        return {}
    gid = str(group_id or "").strip()

    for candidate in (
        data_obj.get(gid),
        data_obj.get(int(gid)) if gid.isdigit() else None,
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate

    for map_key in ("gridInfoMap", "grid_info_map", "groups", "groupMap"):
        group_map = data_obj.get(map_key)
        if isinstance(group_map, str):
            try:
                group_map = json.loads(group_map)
            except Exception:
                group_map = None
        if isinstance(group_map, dict):
            candidate = group_map.get(gid)
            if isinstance(candidate, dict) and candidate:
                return candidate
            if len(group_map) == 1:
                only = next(iter(group_map.values()))
                if isinstance(only, dict):
                    return only
    return {}


def fetch_group_summary(group_id: str, zpw_enk: str, cookies: str, imei: str = "",
                        zpw_ver: str | None = None, timeout: int = 20) -> dict:
    """Lấy tên/avatar/số thành viên của MỘT nhóm.

    Dùng ``mcount=1`` nên không kéo danh sách thành viên về -> rất nhanh và
    nhẹ, nhưng ``totalMember`` vẫn chính xác.
    """
    payload = {
        "grids": [str(group_id)],
        "avatar_size": 120,
        "member_avatar_size": 120,
        "mpage": 1,
        "mcount": 1,
        "imei": str(imei or ""),
    }
    decoded = _post_encrypted(
        GETMG_URL,
        payload,
        zpw_enk,
        cookies,
        zpw_ver=zpw_ver,
        timeout=timeout,
    )
    data = decoded.get("data")
    raw = _extract_group_obj(data, group_id)
    if not raw:
        return {"groupId": str(group_id), "name": "", "avatar": "", "fullAvt": "",
                "memberCount": 0, "error": "Không tìm thấy thông tin nhóm"}

    try:
        member_count = int(raw.get("totalMember") or 0)
    except (TypeError, ValueError):
        member_count = 0

    return {
        "groupId": str(group_id),
        "name": str(raw.get("name") or raw.get("gridName") or ""),
        "desc": str(raw.get("desc") or ""),
        "avatar": str(raw.get("avt") or raw.get("avatar") or ""),
        "fullAvt": str(raw.get("fullAvt") or ""),
        "memberCount": member_count,
        "error": "",
    }


def fetch_group_summaries(group_ids: list[str], zpw_enk: str, cookies: str,
                          imei: str = "", workers: int = 6,
                          zpw_ver: str | None = None, timeout: int = 20) -> dict:
    """Lấy tóm tắt nhiều nhóm song song. Trả về ``{groupId: summary}``."""
    clean_ids = []
    seen = set()
    for gid in group_ids or []:
        gid = str(gid or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            clean_ids.append(gid)
    if not clean_ids:
        return {}

    max_workers = max(1, min(int(workers or 6), len(clean_ids)))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fetch_group_summary, gid, zpw_enk, cookies, imei, zpw_ver, timeout
            ): gid
            for gid in clean_ids
        }
        for future in futures:
            gid = futures[future]
            try:
                results[gid] = future.result()
            except Exception as exc:
                results[gid] = {
                    "groupId": gid,
                    "name": "",
                    "avatar": "",
                    "fullAvt": "",
                    "memberCount": 0,
                    "error": str(exc),
                }
    return results
