# -*- coding: utf-8 -*-
"""Soạn nội dung tin nhắn mời vào nhóm cho người lạ.

Bối cảnh:
    Zalo chặn thêm trực tiếp người chưa kết bạn vào nhóm (mã 314
    MAX_QUOTA_INVITE_STRANGER_PHONEID / 269 "danh sách mời có người lạ").
    Cách duy nhất đưa người lạ vào nhóm là gửi THẺ LINK NHÓM trong tin nhắn
    1-1 (``/api/message/link``) — người nhận bấm vào là vào nhóm, không cần
    kết bạn trước.

Vì vậy nội dung tin nhắn cần đạt 3 yêu cầu:
    1. Xưng hô đúng theo giới tính thật của người nhận (Anh / Chị), khi không
       xác định thì dùng "Anh/Chị".
    2. Gọi đúng tên Zalo của người nhận.
    3. Chỉ gửi MỘT thẻ link, và thẻ đó phải là link nhóm đích. Các link phụ
       (nếu có) chỉ nằm trong phần chữ của tin nhắn.

Template người dùng nhập ở UI hỗ trợ placeholder, cả hai dạng ``{name}`` và
``<name>``:

    {name}    tên Zalo của người nhận
    {gender}  xưng hô: "Anh" / "Chị" / "Anh/Chị"
    {link}    link nhóm đích
    {group}   tên nhóm đích
    {link2}   link phụ thứ nhất
    {link3}   link phụ thứ hai
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Template mặc định — bám sát ví dụ của người dùng:
#   "Xin chào mời Anh/Chị dựa theo giới tính vào nhóm này" + link nhóm.
DEFAULT_INVITE_TEMPLATE = (
    "Xin chào mời {gender} {name} tham gia nhóm {group} này:\n{link}"
)

# Chuỗi cần thay trong template. Hỗ trợ cả {key} và <key>.
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}|<(\w+)>")

# Chuỗi thay thế an toàn khi giá trị rỗng (không để lại "Anh/Chị  vào nhóm").
_EMPTY_PLACEHOLDER_RE = re.compile(r"[ \t]{2,}")


def normalize_gender(value) -> str:
    """Chuẩn hóa giới tính về ``"male"`` / ``"female"`` / ``""`` (không rõ).

    Quy ước của Zalo Web trong tool này: ``0 = Nam``, ``1 = Nữ``; một số
    endpoint cũ trả ``2 = Nữ``. Ngoài ra chấp nhận chuỗi "nam"/"nữ"/"male"/
    "female" để không phụ thuộc vào kiểu dữ liệu đầu vào.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""

    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text in {"-", "unknown", "null", "none", "undefined"}:
            return ""
        if text in {"nam", "male", "m", "man"}:
            return "male"
        if text in {"nữ", "nu", "female", "f", "woman"}:
            return "female"
        try:
            value = int(text)
        except (TypeError, ValueError):
            return ""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""

    if number == 0:
        return "male"
    if number in (1, 2):
        return "female"
    return ""


def gender_title(value) -> str:
    """Xưng hô dùng trong lời chào: "Anh" / "Chị" / "Anh/Chị"."""
    normalized = normalize_gender(value)
    if normalized == "male":
        return "Anh"
    if normalized == "female":
        return "Chị"
    return "Anh/Chị"


def gender_label(value) -> str:
    """Nhãn giới tính để hiển thị: "Nam" / "Nữ" / ""."""
    normalized = normalize_gender(value)
    if normalized == "male":
        return "Nam"
    if normalized == "female":
        return "Nữ"
    return ""


def _clean_name(value) -> str:
    name = str(value or "").strip()
    # Bỏ khoảng trắng thừa ở giữa và ở cuối.
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip()


def render_invite_template(
    template: str,
    *,
    name: str = "",
    gender: str = "",
    link: str = "",
    group_name: str = "",
    extra_links: Optional[Iterable[str]] = None,
) -> str:
    """Thay placeholder trong template bằng dữ liệu của từng người nhận.

    - ``name`` rỗng -> dùng "bạn" để câu vẫn đọc được.
    - ``gender`` rỗng/không rõ -> "Anh/Chị".
    - Nếu template không chứa ``{link}`` thì link nhóm vẫn được tự động gắn
      vào cuối, vì gửi link là mục đích chính của tin nhắn này.
    - Link phụ (tối đa 2) được thêm vào cuối nội dung, mỗi link một dòng.
    """
    template = str(template or "").strip()
    if not template:
        template = DEFAULT_INVITE_TEMPLATE

    clean_name = _clean_name(name) or "bạn"
    title = gender_title(gender)
    link = str(link or "").strip()
    group = _clean_name(group_name) or "nhóm"

    links = []
    for item in (extra_links or []):
        text = str(item or "").strip()
        if text and text not in links:
            links.append(text)
    links = links[:2]

    values = {
        "name": clean_name,
        "gender": title,
        "link": link,
        "group": group,
        "link2": links[0] if len(links) > 0 else "",
        "link3": links[1] if len(links) > 1 else "",
    }

    def _replace(match) -> str:
        key = match.group(1) or match.group(2) or ""
        return values.get(key.lower(), match.group(0))

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    rendered = _EMPTY_PLACEHOLDER_RE.sub(" ", rendered)
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    rendered = rendered.strip()

    # Bảo đảm link nhóm luôn có mặt trong nội dung.
    if link and link not in rendered:
        rendered = (rendered + "\n" + link).strip() if rendered else link

    # Gắn link phụ vào cuối (mỗi link một dòng).
    for extra in links:
        if extra not in rendered:
            rendered = (rendered + "\n" + extra).strip()

    return rendered


def extract_extra_links(text: str, limit: int = 2) -> list[str]:
    """Tách các link phụ từ một đoạn text do người dùng nhập.

    Mỗi dòng là một link. Bỏ qua dòng trống, link trùng nhau và link trùng
    với link nhóm đích (link nhóm đích do hệ thống tự chèn).
    """
    result: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Cho phép gõ kèm mô tả: "link 1 - https://zalo.me/g/abc" -> lấy URL.
        for found in re.findall(r"https?://\S+", line):
            url = found.rstrip(".,;)")
            if url and url not in result:
                result.append(url)
            if len(result) >= limit:
                return result
        if len(result) >= limit:
            break
    return result[:limit]
