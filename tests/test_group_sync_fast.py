"""Test cho luồng đồng bộ nhóm nhanh qua API + số thành viên luôn đúng.

Các test này KHÔNG gọi mạng thật (Zalo API) — chúng kiểm tra logic merge,
đường đọc nhanh và việc số liệu không bị "đóng băng".
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK]   {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label} -> {detail}")


def test_sync_api_payload() -> None:
    print("\n== Payload đồng bộ nhanh ==")
    from features.groups.group_sync_api import _extract_group_obj

    check("group object nằm ở data[groupId]",
          _extract_group_obj({"123": {"name": "A"}}, "123").get("name") == "A")
    check("group object nằm ở gridInfoMap",
          _extract_group_obj({"gridInfoMap": {"123": {"name": "B"}}}, "123").get("name") == "B")
    check("không có group object -> rỗng",
          _extract_group_obj({"other": 1}, "999") == {})


def test_cookie_dict() -> None:
    print("\n== Parse cookie ==")
    from features.groups.group_sync_api import _cookie_dict

    result = _cookie_dict("a=1; b=2;  ; malformed; c=3")
    check("lấy đúng 3 cookie", result == {"a": "1", "b": "2", "c": "3"}, f"got {result}")


def test_member_count_not_frozen() -> None:
    print("\n== Số thành viên không bị đóng băng ==")
    from features.groups.group_manager import merge_group_summaries

    old = [{
        "groupId": "1",
        "name": "Nhóm cũ",
        "memberCount": 5,
        "groupLink": "https://zalo.me/g/abc",
    }]

    # API trả về 0 thành viên -> phải nhận 0, KHÔNG giữ số cũ (5).
    merged = merge_group_summaries(["1"], {"1": {"name": "Nhóm cũ", "memberCount": 0}}, old)
    check("0 thành viên không rơi về số cũ (5)",
          merged[0]["memberCount"] == 0,
          f"got {merged[0]['memberCount']}")

    # API trả về số mới -> phải dùng số mới.
    merged = merge_group_summaries(["1"], {"1": {"name": "X", "memberCount": 2}}, old)
    check("số mới từ API được dùng",
          merged[0]["memberCount"] == 2,
          f"got {merged[0]['memberCount']}")

    # Giữ lại link cũ (không mất khi đồng bộ lại).
    check("giữ lại link nhóm cũ",
          merged[0]["groupLink"] == "https://zalo.me/g/abc",
          f"got {merged[0]['groupLink']}")

    # API lỗi -> không phá dữ liệu cũ (fetchStatus = error).
    merged = merge_group_summaries(["1"], {"1": {"error": "timeout"}}, old)
    check("API lỗi -> đánh dấu error",
          merged[0]["fetchStatus"] == "error",
          f"got {merged[0]['fetchStatus']}")

    # Nhóm mới chưa từng có -> tạo placeholder hợp lệ.
    merged = merge_group_summaries(["2"], {"2": {"name": "Mới", "memberCount": 3}}, old)
    check("nhóm mới tạo đầy đủ",
          merged[0]["name"] == "Mới" and merged[0]["memberCount"] == 3,
          f"got {merged[0]}")


def test_refresh_endpoint_direct_api_flag() -> None:
    print("\n== Endpoint refresh trả cờ directApi ==")
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
    )
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    check("có nhánh API trực tiếp", '"directApi": True' in source)
    check("có nhánh dự phòng Chrome", '"directApi": False' in source)
    check("không xóa personalGroups khi sync trực tiếp",
          "personalGroups=[]" in source, "vẫn còn nhánh Chrome xóa (đúng)")


def test_js_handles_direct_api() -> None:
    print("\n== Frontend xử lý directApi ==")
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "js", "groups.js",
    )
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    check("JS có nhánh data.directApi", "data.directApi" in source)


def test_member_count_merge_in_app() -> None:
    print("\n== Merge memberCount trong app.py ==")
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
    )
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    check("không dùng `or old.get(memberCount)` (giữ số cũ)",
          'new.get("memberCount") or old.get("memberCount")' not in source,
          "vẫn còn fallback giữ số cũ")


def main() -> None:
    print("=" * 60)
    print("TEST ĐỒNG BỘ NHÓM NHANH + SỐ THÀNH VIÊN")
    print("=" * 60)
    test_sync_api_payload()
    test_cookie_dict()
    test_member_count_not_frozen()
    test_refresh_endpoint_direct_api_flag()
    test_js_handles_direct_api()
    test_member_count_merge_in_app()
    print("\n" + "=" * 60)
    print(f"PASSED: {PASSED}   FAILED: {FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
