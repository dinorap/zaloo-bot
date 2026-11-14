"""Chuyển các module gọi API Zalo sang dùng session dùng chung (connection pooling).

Chạy 1 lần:  python tools\\migrate_to_shared_http.py
"""

from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Các module thuần Zalo API. Không gồm telegram/gemini/gateway vì chúng nói
# chuyện với máy chủ khác và cần cấu hình riêng.
TARGETS = [
    "features/accounts/profile_me_v2.py",
    "features/accounts/account_network_monitor.py",
    "features/groups/add_group.py",
    "features/groups/get_group.py",
    "features/groups/group_join_leave.py",
    "features/groups/join_leave_group.py",
    "features/groups/send_sms_group.py",
    "features/members/group_getmg.py",
    "features/members/group_getmg_v2.py",
    "features/messaging/board_pin.py",
    "features/messaging/last_messages.py",
    "features/messaging/quote_message.py",
    "features/messaging/remove_friend.py",
    "features/messaging/send_file.py",
    "features/messaging/send_photo.py",
    "features/messaging/send_sms.py",
    "features/messaging/undo_msg.py",
    "features/profiles/get_single_profile.py",
]

IMPORT_LINE = "from core.zalo import http as zalo_http"


def migrate(rel_path: str) -> bool:
    path = os.path.join(ROOT, rel_path)
    if not os.path.isfile(path):
        print(f"  [skip] {rel_path} (không tồn tại)")
        return False

    with io.open(path, encoding="utf-8") as f:
        src = f.read()

    if "zalo_http." in src:
        print(f"  [skip] {rel_path} (đã chuyển rồi)")
        return False

    # Chỉ thay các call requests.get/post -> zalo_http.get/post
    new_src = re.sub(r"\brequests\.(get|post|put|delete)\(", r"zalo_http.\1(", src)
    if new_src == src:
        print(f"  [skip] {rel_path} (không có call requests trực tiếp)")
        return False

    # Thêm import
    if IMPORT_LINE not in new_src:
        # Chèn sau block import đầu tiên
        lines = new_src.split("\n")
        insert_at = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                insert_at = index + 1
            elif stripped and not stripped.startswith("#") and insert_at:
                break
        lines.insert(insert_at, IMPORT_LINE)
        new_src = "\n".join(lines)

    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_src)

    count = len(re.findall(r"zalo_http\.(get|post)\(", new_src))
    print(f"  [ok]   {rel_path} -> {count} call")
    return True


def main() -> None:
    print("Chuyển sang session dùng chung:")
    changed = 0
    for rel in TARGETS:
        if migrate(rel):
            changed += 1
    print(f"\nĐã chuyển {changed}/{len(TARGETS)} module.")


if __name__ == "__main__":
    main()
