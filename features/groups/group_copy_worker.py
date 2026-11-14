"""Worker sao chép thành viên nhóm theo hạn mức kết bạn mỗi ngày.

Nguyên tắc:
- Sau khi quét nhóm nguồn, ưu tiên thêm toàn bộ thành viên đã là bạn bè.
- Người thêm trực tiếp thành công không bị tính vào hạn mức hằng ngày.
- Người chưa thêm được sẽ nhận lời mời kết bạn, tối đa X người/ngày.
- Những người đã được chiến dịch gửi kết bạn sẽ được thử add lại theo chu kỳ
  cấu hình, mặc định 30 phút, cho đến khi vào nhóm hoặc hết hạn chiến dịch.
- Zalo KHÔNG cho thêm trực tiếp người chưa kết bạn:
    - ``/api/group/invite/v2`` -> 314 (MAX_QUOTA_INVITE_STRANGER_PHONEID)
    - ``/api/group/invite/multi`` -> 269 (danh sách mời có người lạ)
  Vì vậy với người lạ, chiến dịch dùng hai kênh đã được Zalo chấp nhận:
    1. Gửi lời mời kết bạn kèm link nhóm (người nhận tự mở link và vào nhóm).
    2. Gửi thẻ link nhóm (link card) trong tin nhắn 1-1. Kênh này Zalo trả
       code 0 kể cả với người lạ, người nhận chỉ cần bấm là vào nhóm, không
       cần kết bạn trước.
- Người lạ không bị gọi lại API thêm trực tiếp liên tục (tránh đốt hạn mức
  và bị Zalo chặn anti-spam). Chỉ thử lại add trực tiếp khi họ đã là bạn bè.
- Khi thành viên vào nhóm, có thể xóa kết bạn đúng người do chiến dịch vừa
  kết bạn; bạn bè có từ trước không bao giờ bị xóa.
- Tác vụ dừng ngay khi đủ thành viên hoặc khi hết hạn chiến dịch.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from core.zalo.zalo_config import get_zpw_ver
from features.accounts.account_manager import get_account
from features.groups.add_group import create_group
from features.groups.group_copy_manager import claim_due_job, save_job, get_job, recover_running_jobs
from features.groups.group_link import create_group_link, get_group_link_detail
from features.groups.invite_group import invite_members_to_group
from features.groups.pending_members import approve_pending_members, list_pending_members
from features.members.get_members import get_members_by_group_id
from features.messaging.add_friend import send_friend_request
from features.messaging.remove_friend import remove_friend
from features.messaging.send_link import send_message_smart


def _decoded_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"raw": value}
        except Exception:
            return {"raw": value}
    return {}


def _error_code(response_json: dict, decoded: dict) -> int:
    raw = decoded.get("error_code", response_json.get("error_code", -1))
    try:
        return int(raw or 0)
    except Exception:
        return -1


def _error_message(response_json: dict, decoded: dict) -> str:
    return str(
        decoded.get("error_message")
        or decoded.get("message")
        or response_json.get("error_message")
        or response_json.get("message")
        or ""
    ).strip()


def _extract_group_id(response_json: dict, decoded: dict) -> str:
    candidates = [decoded.get("data"), response_json.get("data"), decoded, response_json]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        gid = item.get("groupId") or item.get("grid") or item.get("gridId") or item.get("id")
        if gid:
            return str(gid).strip()
    return ""


def _extract_error_member_ids(response_json: dict, decoded: dict) -> set[str]:
    result: set[str] = set()
    candidates = []
    for root in (decoded, response_json):
        if not isinstance(root, dict):
            continue
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        candidates.extend([data.get("errorMembers"), root.get("errorMembers")])

    for raw_items in candidates:
        if not raw_items:
            continue
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        for item in raw_items:
            if isinstance(item, dict):
                uid = item.get("userId") or item.get("uid") or item.get("id") or item.get("memberId")
            else:
                uid = item
            uid = str(uid or "").strip()
            if uid:
                result.add(uid)
    return result


def _daily_limit(job: dict) -> int:
    try:
        return max(
            1,
            min(
                int(
                    job.get("friendRequestDailyLimit")
                    or job.get("dailyLimit")
                    or job.get("batchSize")
                    or 10
                ),
                100,
            ),
        )
    except (TypeError, ValueError):
        return 10


def _verify_interval(job: dict) -> int:
    try:
        return max(1, min(int(job.get("verifyIntervalMinutes") or 30), 1440))
    except (TypeError, ValueError):
        return 30


def _status_poll_interval(job: dict) -> int:
    """Chu kỳ (phút) rà soát nhanh trạng thái người đã được gửi link.

    Chu kỳ kiểm tra trong UI (``verifyIntervalMinutes``) là chu kỳ *thử add
    lại vào nhóm* — mỗi lần đó đều gọi API mời nên rất nặng và phải giãn ra
    30 phút. Nhưng người nhận bấm link nhóm có thể vào nhóm sau vài phút; nếu
    chờ tới chu kỳ 30 phút thì bảng thống kê "đứng yên" rất lâu và người dùng
    tưởng chiến dịch hỏng. Vì vậy tách thêm một chu kỳ rà soát NHANH, chỉ
    đọc danh sách thành viên nhóm đích (không gọi API mời) để cập nhật bảng
    thống kê gần như tức thì.
    """
    try:
        return max(1, min(int(job.get("statusPollMinutes") or 2), 30))
    except (TypeError, ValueError):
        return 2


def _next_status_poll(job: dict, after: Optional[datetime] = None) -> str:
    after = after or datetime.now()
    return (after + timedelta(minutes=_status_poll_interval(job))).isoformat(timespec="seconds")


def _daily_run_time(job: dict) -> tuple[int, int]:
    raw = str(job.get("dailyRunTime") or "").strip()
    if raw:
        try:
            hour, minute = raw.split(":", 1)
            return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
        except (TypeError, ValueError):
            pass
    try:
        start = datetime.fromisoformat(str(job.get("startAt") or ""))
        return start.hour, start.minute
    except (TypeError, ValueError):
        now = datetime.now()
        return now.hour, now.minute


def _next_daily_run(job: dict, after: Optional[datetime] = None) -> str:
    after = after or datetime.now()
    hour, minute = _daily_run_time(job)
    next_day = after.date() + timedelta(days=1)
    candidate = datetime.combine(next_day, datetime.min.time()).replace(hour=hour, minute=minute)
    return candidate.isoformat(timespec="seconds")


def _next_verify_run(job: dict, after: Optional[datetime] = None) -> str:
    after = after or datetime.now()
    return (after + timedelta(minutes=_verify_interval(job))).isoformat(timespec="seconds")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _is_due(value: str, now: Optional[datetime] = None) -> bool:
    parsed = _parse_iso(value)
    return parsed is None or parsed <= (now or datetime.now())


def _cookie_dict(cookies: str) -> dict:
    result = {}
    for item in str(cookies or "").split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _reset_daily_friend_request_counter(job: dict, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    current_date = now.date().isoformat()
    if str(job.get("dailyFriendRequestDate") or "") != current_date:
        job["dailyFriendRequestDate"] = current_date
        job["dailyFriendRequestCount"] = 0


def _remaining_friend_requests_today(job: dict, now: Optional[datetime] = None) -> int:
    """Số lượt tiếp cận còn lại trong ngày (dùng chung cho lời mời kết bạn và thẻ link).

    Hai kênh này dùng chung một hạn mức để tổng số tin nhắn gửi ra/ngày không
    vượt quá giới hạn mà Zalo chấp nhận, tránh kích hoạt cơ chế chống spam.
    """
    _reset_daily_friend_request_counter(job, now)
    _reset_daily_link_card_counter(job, now)
    used = max(0, int(job.get("dailyFriendRequestCount") or 0))
    used += max(0, int(job.get("dailyLinkCardCount") or 0))
    return max(0, _daily_limit(job) - used)


def _consume_friend_request_slot(job: dict, now: Optional[datetime] = None) -> None:
    _reset_daily_friend_request_counter(job, now)
    job["dailyFriendRequestCount"] = int(job.get("dailyFriendRequestCount") or 0) + 1


def _reset_daily_link_card_counter(job: dict, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    current_date = now.date().isoformat()
    if str(job.get("dailyLinkCardDate") or "") != current_date:
        job["dailyLinkCardDate"] = current_date
        job["dailyLinkCardCount"] = 0


def _remaining_link_cards_today(job: dict, now: Optional[datetime] = None) -> int:
    """Số thẻ link nhóm còn được gửi trong ngày (cùng hạn mức với lời mời kết bạn).

    Gửi quá nhiều tin nhắn 1-1 cho người lạ sẽ kích hoạt cơ chế chống spam
    của Zalo và khiến cả lời mời kết bạn cũng bị chặn.
    """
    return _remaining_friend_requests_today(job, now)


# ─── Hạn mức theo NGƯỜI thay vì theo tin nhắn ────────────────────────────────
# Một người lạ có thể được gửi CẢ HAI kênh trong cùng một đợt (thẻ link nhóm
# + lời mời kết bạn kèm link). Hạn mức hằng ngày mang ý nghĩa "tiếp cận tối
# đa N người/ngày", nên phải đếm theo NGƯỜI, không đếm theo số tin nhắn.
# Nếu đếm theo tin nhắn, gửi cả hai kênh cho một người sẽ làm hết hạn mức
# gấp đôi và chiến dịch chỉ tiếp cận được nửa số người dự kiến.


def _reset_daily_reach_counter(job: dict, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    current_date = now.date().isoformat()
    if str(job.get("dailyReachDate") or "") != current_date:
        job["dailyReachDate"] = current_date
        job["dailyReachCount"] = 0


def _remaining_reach_today(job: dict, now: Optional[datetime] = None) -> int:
    """Số NGƯỜI còn được tiếp cận hôm nay (dùng chung cho cả hai kênh)."""
    _reset_daily_reach_counter(job, now)
    used = max(0, int(job.get("dailyReachCount") or 0))
    return max(0, _daily_limit(job) - used)


def _consume_reach_slot(job: dict, now: Optional[datetime] = None) -> None:
    _reset_daily_reach_counter(job, now)
    job["dailyReachCount"] = int(job.get("dailyReachCount") or 0) + 1


def _refund_reach_slot(job: dict, now: Optional[datetime] = None) -> None:
    _reset_daily_reach_counter(job, now)
    current = int(job.get("dailyReachCount") or 0)
    job["dailyReachCount"] = max(0, current - 1)


def _consume_link_card_slot(job: dict, now: Optional[datetime] = None) -> None:
    _reset_daily_link_card_counter(job, now)
    job["dailyLinkCardCount"] = int(job.get("dailyLinkCardCount") or 0) + 1


def _refund_link_card_slot(job: dict, now: Optional[datetime] = None) -> None:
    """Hoàn lại 1 lượt hạn mức khi tin nhắn không tới được người nhận.

    Zalo chỉ tính hạn mứn chống spam theo tin nhắn **thực sự gửi đi**. Khi
    người nhận đã chặn (mã 122) hoặc tài khoản gửi bị chặn, không có tin nhắn
    nào tới nơi nên lượt hạn mức bị trừ oan. Hoàn lại giúp chiến dịch không
    bị cạn hạn mức vì một loạt người đã chặn.
    """
    _reset_daily_link_card_counter(job, now)
    current = int(job.get("dailyLinkCardCount") or 0)
    job["dailyLinkCardCount"] = max(0, current - 1)


def _refund_friend_request_slot(job: dict, now: Optional[datetime] = None) -> None:
    """Hoàn lại 1 lượt hạn mức lời mời kết bạn khi Zalo từ chối rõ ràng."""
    _reset_daily_friend_request_counter(job, now)
    current = int(job.get("dailyFriendRequestCount") or 0)
    job["dailyFriendRequestCount"] = max(0, current - 1)


# Mã lỗi cho thấy yêu cầu KHÔNG tới được người nhận -> không nên trừ hạn mức.
_DELIVERY_FAILED_CODES = {122, 188, 166, 244, 245}


def _friend_request_text(job: dict, group_link: str) -> str:
    group_name = str(job.get("targetGroupName") or job.get("newGroupName") or "nhóm").strip()[:60]
    return f"Mời bạn tham gia nhóm {group_name}: {group_link}".strip()


def _group_link_message(job: dict, group_link: str, member: Optional[dict] = None) -> str:
    """Nội dung tin nhắn 1-1 gửi kèm thẻ link nhóm cho người lạ.

    Kênh này đã được kiểm chứng: ``/api/message/link`` trả code 0 ngay cả khi
    người nhận chưa kết bạn, và họ chỉ cần bấm là vào nhóm.

    Nội dung dựng từ template người dùng tự nhập (``inviteMessageTemplate``)
    với tên Zalo và xưng hô theo giới tính thật của người nhận. Chỉ MỘT thẻ
    link được gửi và thẻ đó luôn là link nhóm đích; các link phụ (nếu có) chỉ
    nằm trong phần chữ của tin nhắn.
    """
    from features.messaging.invite_message import (
        DEFAULT_INVITE_TEMPLATE,
        extract_extra_links,
        render_invite_template,
    )

    group_name = str(job.get("targetGroupName") or job.get("newGroupName") or "nhóm").strip()[:60]
    template = str(job.get("inviteMessageTemplate") or "").strip() or DEFAULT_INVITE_TEMPLATE
    extra_links = extract_extra_links(str(job.get("inviteExtraLinks") or ""), limit=2)

    member = member or {}
    name = str(
        member.get("zaloName")
        or member.get("displayName")
        or member.get("dName")
        or ""
    ).strip()
    gender = member.get("gender")

    return render_invite_template(
        template,
        name=name,
        gender=gender,
        link=group_link,
        group_name=group_name,
        extra_links=extra_links,
    )


def _send_group_link_card(
    uid: str,
    job: dict,
    group_link: str,
    zpw_enk: str,
    cookies: str,
    imei: str,
    link_info: Optional[dict] = None,
    member: Optional[dict] = None,
) -> tuple[bool, int, str]:
    """Gửi thẻ link nhóm cho một người lạ trong tin nhắn 1-1.

    Trả về ``(ok, code, message)``. Kênh này là cách duy nhất Zalo cho phép
    đưa người lạ vào nhóm mà không cần kết bạn trước.
    """
    if not group_link:
        return False, -1, "Chưa có link nhóm để gửi."
    message = _group_link_message(job, group_link, member=member)
    try:
        result = send_message_smart(
            uid,
            message,
            zpw_enk,
            cookies,
            imei,
            is_group=False,
            zpw_ver=get_zpw_ver(),
            link_info=link_info,
            # Ép thẻ card là link nhóm đích, không phải link phụ trong nội dung.
            link=group_link,
        )
    except Exception as exc:
        return False, -1, str(exc)

    decoded = result.get("decoded") if isinstance(result, dict) else None
    code = -1
    if isinstance(decoded, dict):
        try:
            code = int(decoded.get("error_code", -1))
        except (TypeError, ValueError):
            code = -1
    ok = bool(result.get("ok")) if isinstance(result, dict) else False
    detail = ""
    if isinstance(decoded, dict):
        detail = str(decoded.get("error_message") or decoded.get("message") or "").strip()
    if not ok and not detail:
        detail = str((result or {}).get("error") or "").strip()
    return ok, code, detail


def _ensure_group_link(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
    *,
    newly_created: bool = False,
    force: bool = False,
) -> str:
    """Lấy link nhóm và lưu vào tác vụ để tái sử dụng đến khi hết hạn."""
    now_ms = int(time.time() * 1000)
    cached_link = str(job.get("groupLink") or "").strip()
    try:
        expiration = int(job.get("groupLinkExpirationDate") or 0)
    except (TypeError, ValueError):
        expiration = 0
    enabled = int(job.get("groupLinkEnabled") or 0)
    cache_valid = bool(cached_link) and enabled == 1 and (expiration == 0 or expiration > now_ms + 60_000)
    if cache_valid and not force:
        return cached_link

    group_id = str(job.get("targetGroupId") or "").strip()
    if not group_id:
        raise ValueError("Tác vụ chưa có Group ID để lấy link tham gia.")

    if newly_created:
        result = create_group_link(
            group_id,
            imei,
            zpw_enk,
            cookies,
            zpw_ver=get_zpw_ver(),
        )
        job["groupLinkSource"] = "new"
    else:
        result = get_group_link_detail(
            group_id,
            imei,
            zpw_enk,
            cookies,
            zpw_ver=get_zpw_ver(),
        )
        job["groupLinkSource"] = "detail"

    job["groupLink"] = str(result.get("link") or "").strip()
    job["groupLinkExpirationDate"] = int(result.get("expirationDate") or 0)
    job["groupLinkEnabled"] = int(result.get("enabled") or 0)
    job["groupLinkUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
    return job["groupLink"]


def _friend_request_result(response_json: dict, decoded_raw) -> tuple[bool, int, str]:
    response_json = response_json if isinstance(response_json, dict) else {}
    decoded = _decoded_dict(decoded_raw)
    code = _error_code(response_json, decoded)
    message = _error_message(response_json, decoded)
    return code == 0, code, message


def _api_result(response_json: dict, decoded_raw) -> tuple[bool, int, str]:
    response_json = response_json if isinstance(response_json, dict) else {}
    decoded = _decoded_dict(decoded_raw)
    code = _error_code(response_json, decoded)
    message = _error_message(response_json, decoded)
    return code == 0, code, message


def _campaign_end(job: dict) -> Optional[datetime]:
    return _parse_iso(str(job.get("campaignEndAt") or ""))


def _campaign_expired(job: dict, now: Optional[datetime] = None) -> bool:
    end_at = _campaign_end(job)
    return bool(end_at and (now or datetime.now()) >= end_at)



def _member_map(job: dict) -> dict[str, dict]:
    return {
        str(member.get("userId") or "").strip(): member
        for member in (job.get("members") or [])
        if str(member.get("userId") or "").strip()
    }


def _pending_members(job: dict) -> list[dict]:
    return [
        member for member in (job.get("members") or [])
        if str(member.get("status") or "pending") == "pending"
    ]


def _awaiting_members(job: dict) -> list[dict]:
    return [
        member for member in (job.get("members") or [])
        if str(member.get("status") or "") == "invited"
    ]


def _escalation_members(job: dict) -> list[dict]:
    """Người lạ đã nhận thẻ link nhóm nhưng chưa nhận lời mời kết bạn.

    Sau khi gửi thẻ link nhóm, thành viên chuyển sang trạng thái ``invited``
    nên không còn nằm trong ``_pending_members``. Nếu không tách riêng nhóm này,
    kênh thứ hai (lời mời kết bạn kèm link nhóm) sẽ không bao giờ được gửi và
    lịch chạy hằng ngày cũng bị dừng.
    """
    result = []
    for member in job.get("members") or []:
        if str(member.get("status") or "") == "joined":
            continue
        if member.get("isFriend") is True:
            continue
        if str(member.get("messageDelivery") or "") != "sent":
            continue
        if str(member.get("friendRequestDelivery") or "") == "friend_request_with_group_link":
            continue
        result.append(member)
    return result


def _outreach_members(job: dict) -> list[dict]:
    """Toàn bộ thành viên còn cần tiếp cận (chưa vào nhóm)."""
    seen = set()
    result = []
    for member in list(_pending_members(job)) + list(_escalation_members(job)):
        uid = str(member.get("userId") or "").strip()
        key = uid or id(member)
        if key in seen:
            continue
        seen.add(key)
        result.append(member)
    return result


def _earliest_iso(values: list[str]) -> str:
    clean = [str(value or "").strip() for value in values if str(value or "").strip()]
    return min(clean) if clean else ""


def _sync_job_schedule(job: dict, now: Optional[datetime] = None) -> None:
    """Đồng bộ trạng thái tác vụ và lần chạy kế tiếp."""
    now = now or datetime.now()
    members = job.get("members") or []
    pending = [m for m in members if m.get("status") == "pending"]
    awaiting = [m for m in members if m.get("status") == "invited"]
    joined = [m for m in members if m.get("status") == "joined"]
    failed = [m for m in members if m.get("status") == "failed"]
    skipped = [m for m in members if m.get("status") == "skipped"]

    if _campaign_expired(job, now):
        job["status"] = "done" if members and len(joined) + len(skipped) == len(members) else "expired"
        job["campaignExpiredAt"] = str(job.get("campaignExpiredAt") or now.isoformat(timespec="seconds"))
        job["nextInviteAt"] = ""
        job["nextVerifyAt"] = ""
        job["nextStatusPollAt"] = ""
        job["nextRunAt"] = ""
        return

    if members and len(joined) + len(skipped) == len(members):
        # Đã đủ thành viên: hoàn thành chiến dịch và dừng toàn bộ lịch kiểm tra.
        job["status"] = "done"
        job["completedAt"] = str(job.get("completedAt") or now.isoformat(timespec="seconds"))
        job["nextInviteAt"] = ""
        job["nextVerifyAt"] = ""
        job["nextStatusPollAt"] = ""
        job["nextRunAt"] = ""
        return

    if pending or awaiting:
        job["status"] = "pending"
        candidates = []
        escalation = _escalation_members(job)
        if pending or escalation:
            if not str(job.get("nextInviteAt") or "").strip():
                job["nextInviteAt"] = _next_daily_run(job, now)
            candidates.append(str(job.get("nextInviteAt") or ""))
        else:
            job["nextInviteAt"] = ""

        # Nguoi da nhan the link nhom nhung chua nhan loi moi ket ban: cho phep
        # gui bu loi moi ket ban NGAY trong ngay hom nay neu con han muc, thay
        # vi phai doi toi dot hang ngay ke tiep. Neu het han muc thi van doi
        # den dot hang ngay ke tiep.
        if escalation and not pending:
            if _remaining_reach_today(job, now) > 0:
                job["nextInviteAt"] = now.isoformat(timespec="seconds")
            else:
                # Het han muc hom nay: dua dot gui sang ngay mai de tranh
                # worker chay lien tuc khong lam gi.
                job["nextInviteAt"] = _next_daily_run(job, now)
            candidates.append(str(job.get("nextInviteAt") or ""))

        if str(job.get("targetGroupId") or "").strip():
            if not str(job.get("nextVerifyAt") or "").strip():
                job["nextVerifyAt"] = _next_verify_run(job, now)
            candidates.append(str(job.get("nextVerifyAt") or ""))
            # Rà soát nhanh: chỉ khi còn người đang chờ vào nhóm thì mới bật,
            # để bảng thống kê cập nhật gần như tức thì sau khi họ bấm link.
            if awaiting:
                job["nextStatusPollAt"] = _next_status_poll(job, now)
                candidates.append(str(job.get("nextStatusPollAt") or ""))
            else:
                job["nextStatusPollAt"] = ""
        else:
            job["nextVerifyAt"] = ""
            job["nextStatusPollAt"] = ""

        job["nextRunAt"] = _earliest_iso(candidates) or now.isoformat(timespec="seconds")
        return

    job["status"] = "partial" if failed else "done"
    job["nextInviteAt"] = ""
    job["nextVerifyAt"] = ""
    job["nextStatusPollAt"] = ""
    job["nextRunAt"] = ""


# ─── Cache danh sách thành viên nhóm đích ────────────────────────────────────
# Một chu kỳ chạy chiến dịch gọi _verify_target_members tới 3 lần (trước add,
# sau add, kiểm tra cuối). Mỗi lần đọc TOÀN BỘ danh sách thành viên nhóm đích
# qua getmg (có thể hàng chục trang). Cache ngắn hạn giảm 3 lần đọc xuống còn
# 1 lần mà không ảnh hưởng độ chính xác (thay đổi thành viên không xảy ra
# trong vài chục giây giữa các bước của cùng một chu kỳ).
_TARGET_MEMBERS_CACHE: dict[str, tuple[float, dict]] = {}
_TARGET_MEMBERS_CACHE_LOCK = threading.Lock()


def _get_target_members_cached(
    group_id: str,
    zpw_enk: str,
    cookies: str,
    imei: str,
    *,
    max_age_seconds: int = 60,
) -> dict:
    """Đọc danh sách thành viên nhóm đích, có cache ngắn hạn theo groupId."""
    key = str(group_id or "").strip()
    now = time.time()
    with _TARGET_MEMBERS_CACHE_LOCK:
        cached = _TARGET_MEMBERS_CACHE.get(key)
        if cached:
            cached_at, cached_result = cached
            if now - cached_at <= max_age_seconds:
                return cached_result

    result = get_members_by_group_id(
        group_id=key,
        cookie_dict=_cookie_dict(cookies),
        zpw_enk=zpw_enk,
        imei=imei,
        zpw_ver=get_zpw_ver(),
        mcount=500,
        timeout=30,
        max_pages=200,
    ) or {}

    # Chỉ cache khi đọc thành công, tránh cache luôn lỗi rồi retry vô ích.
    if result.get("ok"):
        with _TARGET_MEMBERS_CACHE_LOCK:
            _TARGET_MEMBERS_CACHE[key] = (now, result)
    return result


def _invalidate_target_members_cache(group_id: str = "") -> None:
    """Xóa cache danh sách thành viên sau khi có thay đổi thành viên nhóm."""
    with _TARGET_MEMBERS_CACHE_LOCK:
        if group_id:
            _TARGET_MEMBERS_CACHE.pop(str(group_id).strip(), None)
        else:
            _TARGET_MEMBERS_CACHE.clear()


def _verify_target_members(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
    *,
    force: bool = False,
    reschedule_verify: bool = True,
) -> dict:
    """Đọc lại nhóm đích và cập nhật trạng thái thực tế của từng thành viên nguồn.

    ``reschedule_verify=False`` dùng cho lần rà soát nhanh: không đẩy lùi
    lịch thử add lại (``nextVerifyAt``), nếu không mỗi lần rà soát nhanh sẽ
    hoãn vô hạn chu kỳ thử add lại vào nhóm.
    """
    group_id = str(job.get("targetGroupId") or "").strip()
    if not group_id:
        return {"checked": False, "newlyJoined": 0, "leftCount": 0, "targetCount": 0}

    now = datetime.now()
    if not force and not _is_due(str(job.get("nextVerifyAt") or ""), now):
        return {"checked": False, "newlyJoined": 0, "leftCount": 0, "targetCount": int(job.get("targetMemberCount") or 0)}

    try:
        result = _get_target_members_cached(
            group_id,
            zpw_enk,
            cookies,
            imei,
            max_age_seconds=60,
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Không đọc được danh sách thành viên nhóm đích.")

        target_uids = {
            str(uid or "").strip()
            for uid in (result.get("uids") or [])
            if str(uid or "").strip()
        }
        now_ms = int(time.time() * 1000)
        newly_joined = 0
        left_count = 0

        for member in job.get("members") or []:
            uid = str(member.get("userId") or "").strip()
            if not uid:
                continue
            member["lastVerifiedAt"] = now_ms
            if uid in target_uids:
                if member.get("status") != "joined":
                    newly_joined += 1
                member["status"] = "joined"
                member["joinedAt"] = member.get("joinedAt") or now_ms
                if member.get("friendRequestCreatedByCampaign"):
                    # Chỉ đánh dấu người do chiến dịch vừa kết bạn; họ có thể
                    # vào nhóm qua lần add lại hoặc tự tham gia bằng link.
                    member["joinedAfterCampaignAddRetry"] = True
                member["error"] = ""
                continue

            # Nếu trước đó đã có mặt nhưng hiện không còn trong nhóm, đưa lại về
            # danh sách cần mời để hệ thống bổ sung ở đợt hằng ngày tiếp theo.
            if member.get("status") == "joined":
                left_count += 1
                member["leftDetectedAt"] = now_ms
                member["status"] = "pending"
                member["error"] = "Không còn trong nhóm đích; sẽ được đưa vào đợt mời tiếp theo."
            elif member.get("status") == "invited":
                if str(member.get("messageDelivery") or "") == "sent":
                    member["error"] = (
                        "Đã gửi thẻ link nhóm; đang chờ người này bấm link để vào nhóm."
                    )
                else:
                    member["error"] = "Đã gửi lời mời; đang chờ người dùng tham gia nhóm."

        job["targetMemberCount"] = int(result.get("total") or len(target_uids))
        job["lastVerifiedAt"] = now.isoformat(timespec="seconds")
        job["lastVerificationError"] = ""
        if reschedule_verify:
            job["nextVerifyAt"] = _next_verify_run(job, now)
        return {
            "checked": True,
            "newlyJoined": newly_joined,
            "leftCount": left_count,
            "targetCount": job["targetMemberCount"],
        }
    except Exception as exc:
        job["lastVerificationError"] = str(exc)
        if reschedule_verify:
            job["nextVerifyAt"] = _next_verify_run(job, now)
        print(f"[group_copy_worker] Verify target group failed: {exc}", flush=True)
        return {
            "checked": False,
            "newlyJoined": 0,
            "leftCount": 0,
            "targetCount": int(job.get("targetMemberCount") or 0),
            "error": str(exc),
        }


def _approve_pending_target_members(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
) -> dict:
    """Duyệt tự động các thành viên đang chờ vào nhóm đích.

    Đây là bước đóng nốt vòng "người lạ vào nhóm": khi nhóm bật chế độ phê
    duyệt (``joinAppr``), người lạ bấm link nhóm sẽ nằm ở hàng đợi chờ duyệt.
    Nếu không duyệt, họ kẹt vĩnh viễn dù đã bấm link.

    Chỉ duyệt những UID thuộc đúng chiến dịch này để không ảnh hưởng tới
    yêu cầu của người khác trong cùng nhóm.
    """
    group_id = str(job.get("targetGroupId") or "").strip()
    if not group_id:
        return {"attempted": 0, "approved": 0, "failed": 0}

    target_uids = {
        str(member.get("userId") or "").strip()
        for member in (job.get("members") or [])
        if str(member.get("userId") or "").strip()
    }
    if not target_uids:
        return {"attempted": 0, "approved": 0, "failed": 0}

    try:
        result = approve_pending_members(
            group_id,
            imei,
            zpw_enk,
            cookies,
            only_uids=target_uids,
            zpw_ver=get_zpw_ver(),
        )
    except Exception as exc:
        print(f"[group_copy_worker] Duyệt thành viên chờ thất bại: {exc}", flush=True)
        return {"attempted": 0, "approved": 0, "failed": 0, "error": str(exc)}

    approved_uids = [uid for uid in (result.get("approved") or []) if uid]
    if not approved_uids:
        return {
            "attempted": len(result.get("pending") or []),
            "approved": 0,
            "failed": len(result.get("failed") or []),
            "errorCode": result.get("errorCode", 0),
        }

    # Đánh dấu các thành viên vừa được duyệt là đã vào nhóm.
    approved_set = set(approved_uids)
    now_ms = int(time.time() * 1000)
    members_by_uid = _member_map(job)
    for uid in approved_uids:
        member = members_by_uid.get(uid)
        if not member:
            continue
        member["status"] = "joined"
        member["joinedAt"] = member.get("joinedAt") or now_ms
        member["lastVerifiedAt"] = now_ms
        member["pendingApprovedAt"] = now_ms
        member["error"] = ""

    print(
        f"[group_copy_worker] Đã duyệt {len(approved_uids)} thành viên chờ "
        f"vào nhóm {group_id}.",
        flush=True,
    )
    return {
        "attempted": len(result.get("pending") or []),
        "approved": len(approved_uids),
        "failed": len(result.get("failed") or []),
        "approvedUids": approved_set,
    }


def _refresh_friend_status(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
) -> set:
    """Cập nhật cờ ``isFriend`` cho thành viên theo danh sách bạn bè thật.

    Trả về set userId đã là bạn bè. Hàm này không ném lỗi: nếu không lấy được
    danh sách bạn bè thì giữ nguyên trạng thái hiện tại.
    """
    try:
        from features.friends.friend_service import get_friend_uid_set

        friend_uids = get_friend_uid_set(
            zpw_enk,
            cookies,
            imei=imei,
            zpw_ver=get_zpw_ver(),
        )
    except Exception as exc:
        print(f"[group_copy_worker] Không cập nhật được trạng thái bạn bè: {exc}", flush=True)
        return set()

    for member in job.get("members") or []:
        uid = str(member.get("userId") or "").strip()
        if not uid:
            continue
        member["isFriend"] = uid in friend_uids
        member["isFr"] = 1 if uid in friend_uids else 0
    return friend_uids


def _retry_awaiting_group_adds(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
) -> dict:
    """Thử add lại toàn bộ người đã được chiến dịch gửi kết bạn.

    Hàm này không gửi tin nhắn và không tiêu thụ hạn mức kết bạn hằng ngày.
    Trạng thái ``joined`` chỉ được xác nhận sau khi đọc lại danh sách nhóm đích.

    Chỉ những người ĐÃ LÀ BẠN BÈ mới được gọi lại API thêm trực tiếp. Người
    chưa kết bạn sẽ khiến Zalo trả lỗi 314/269 mỗi lần gọi, vừa tốn hạn mức
    vừa kích hoạt cơ chế chống spam của tài khoản.
    """
    candidates = [
        member for member in _awaiting_members(job)
        if str(member.get("userId") or "").strip()
    ]
    if not candidates:
        return {"attempted": 0}

    # Cập nhật quan hệ bạn bè trước: có thể người nhận đã chấp nhận lời mời
    # kết bạn từ chu kỳ trước, lúc này add trực tiếp sẽ thành công.
    friend_uids = _refresh_friend_status(job, zpw_enk, cookies, imei)

    now_ms = int(time.time() * 1000)
    friend_candidates = [
        member for member in candidates
        if str(member.get("userId") or "").strip() in friend_uids
    ]
    stranger_candidates = [
        member for member in candidates
        if str(member.get("userId") or "").strip() not in friend_uids
    ]

    # Người chưa kết bạn: ghi nhận trạng thái và chờ, không gọi lại API.
    for member in stranger_candidates:
        member["inviteDelivery"] = "stranger_blocked"
        member["inviteResultCode"] = 314
        member["inviteResultMessage"] = (
            "Chưa kết bạn nên Zalo không cho thêm trực tiếp vào nhóm "
            "(mã 314). Hệ thống chờ người này chấp nhận lời mời kết bạn rồi mới "
            "thêm trực tiếp; link nhóm đã gửi trong lời mời để họ tự vào nhóm."
        )
        member["error"] = member["inviteResultMessage"]

    if not friend_candidates:
        return {"attempted": 0, "skippedStrangers": len(stranger_candidates)}

    member_ids = [str(member.get("userId") or "").strip() for member in friend_candidates]
    result = invite_members_to_group(
        str(job.get("targetGroupId") or ""),
        member_ids,
        imei,
        zpw_enk,
        cookies,
        zpw_ver=get_zpw_ver(),
        batch_size=50,
    )
    result_by_uid = {
        str(item.get("userId") or "").strip(): item
        for item in (result.get("members") or [])
        if str(item.get("userId") or "").strip()
    }

    for member in friend_candidates:
        uid = str(member.get("userId") or "").strip()
        item = result_by_uid.get(uid) or {
            "status": "failed",
            "code": -1,
            "delivery": "failed",
            "message": "Không đọc được kết quả add lại vào nhóm.",
        }
        member["campaignAddRetryAttempts"] = int(member.get("campaignAddRetryAttempts") or 0) + 1
        member["campaignAddRetryAt"] = now_ms
        member["attemptedAt"] = now_ms
        member["inviteAttempts"] = int(member.get("inviteAttempts") or 0) + 1
        member["inviteResultCode"] = item.get("code", -1)
        member["inviteResultMessage"] = str(item.get("message") or "").strip()
        member["inviteDelivery"] = str(item.get("delivery") or "")
        member["status"] = "invited"
        member["error"] = str(
            item.get("message")
            or "Đã thử add lại; đang đọc nhóm đích để xác nhận kết quả."
        ).strip()

    return {
        "attempted": len(friend_candidates),
        "skippedStrangers": len(stranger_candidates),
    }

def _remove_joined_campaign_friends(
    job: dict,
    zpw_enk: str,
    cookies: str,
    imei: str,
) -> dict:
    """Xóa đúng người do chiến dịch kết bạn và đã add thành công vào nhóm."""
    if not bool(job.get("removeFriendAfterJoin")):
        return {"attempted": 0, "removed": 0, "failed": 0}

    candidates = [
        member for member in (job.get("members") or [])
        if member.get("status") == "joined"
        and bool(member.get("friendRequestCreatedByCampaign"))
        and bool(member.get("joinedAfterCampaignAddRetry"))
        and str(member.get("friendRemoveDelivery") or "") != "removed"
    ]
    removed = 0
    failed = 0
    for member in candidates:
        uid = str(member.get("userId") or "").strip()
        if not uid:
            continue
        member["friendRemoveAttempts"] = int(member.get("friendRemoveAttempts") or 0) + 1
        try:
            response_json, decoded_raw = remove_friend(
                uid,
                imei,
                zpw_enk,
                cookies,
                zpw_ver=get_zpw_ver(),
            )
            success, code, message = _api_result(response_json, decoded_raw)
        except Exception as exc:
            success, code, message = False, -1, str(exc)

        member["friendRemoveCode"] = code
        member["friendRemoveMessage"] = message
        if success:
            member["friendRemoveDelivery"] = "removed"
            member["friendRemovedAt"] = int(time.time() * 1000)
            member["isFriend"] = False
            removed += 1
        else:
            member["friendRemoveDelivery"] = "failed"
            failed += 1
    return {"attempted": len(candidates), "removed": removed, "failed": failed}


class GroupCopyWorker:
    def __init__(self) -> None:
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True, name="group-copy-worker")
        self.thread.start()
        print("[group_copy_worker] Worker started", flush=True)

    def stop(self) -> None:
        self.running = False

    def _worker_loop(self) -> None:
        # Backoff khi có lỗi liên tiếp để không spam API khi session hỏng.
        consecutive_errors = 0
        while self.running:
            try:
                job = claim_due_job(datetime.now().isoformat(timespec="seconds"))
                if job:
                    self._run_cycle(job)
                    consecutive_errors = 0
                    continue
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                print(f"[group_copy_worker] Loop error: {exc}", flush=True)
            # Không có việc -> ngủ ngắn. Có lỗi -> nghỉ lâu hơn theo cấp số nhân.
            time.sleep(min(60, 5 * (2 ** min(consecutive_errors, 4))))

    def _run_cycle(self, job: dict) -> None:
        job_id = str(job.get("jobId") or "")
        started_at = datetime.now()
        run_record = {
            "runNo": len(job.get("runs") or []) + 1,
            "startedAt": started_at.isoformat(timespec="seconds"),
            "completedAt": "",
            "status": "running",
            "action": "",
            "memberIds": [],
            "dailyLimit": _daily_limit(job),
            "sentCount": 0,
            "joinedCount": 0,
            "failedCount": 0,
            "newlyJoinedCount": 0,
            "conversionRate": float(job.get("conversionRate") or 0),
            "error": "",
        }
        job.setdefault("runs", []).append(run_record)

        try:
            account = get_account(str(job.get("accountId") or ""))
            if not account:
                raise ValueError("Không tìm thấy tài khoản thực hiện.")
            cookies = str(account.get("cookies") or "").strip()
            zpw_enk = str(account.get("zpwEnk") or "").strip()
            imei = str(account.get("imei") or "").strip()
            if not all([cookies, zpw_enk, imei]):
                raise ValueError("Tài khoản chưa đủ cookies, zpwEnk hoặc IMEI.")

            # Tài khoản phụ trong nhóm chung: chờ tài khoản chủ tạo xong nhóm đích
            # rồi mượn Group ID + link, không tạo nhóm riêng.
            shared_from = str(job.get("sharedTargetFromJobId") or "").strip()
            if shared_from and not str(job.get("targetGroupId") or "").strip():
                master = get_job(shared_from)
                master_gid = str((master or {}).get("targetGroupId") or "").strip()
                if master_gid:
                    job["targetGroupId"] = master_gid
                    job["targetGroupName"] = str((master or {}).get("targetGroupName") or master_gid)
                    job["groupLink"] = str((master or {}).get("groupLink") or "")
                    job["groupLinkEnabled"] = 1 if job["groupLink"] else 0
                    job["targetMode"] = "existing"
                    job["lastNotice"] = "Đã nhận nhóm đích chung từ tài khoản chính."
                else:
                    # Chủ chưa tạo nhóm xong: hoãn ~2 phút rồi thử lại.
                    run_record["action"] = "wait_shared_target"
                    run_record["status"] = "done"
                    job["status"] = "pending"
                    job["nextRunAt"] = (started_at + timedelta(minutes=2)).isoformat(timespec="seconds")
                    job["lastNotice"] = "Đang chờ tài khoản chính tạo nhóm đích chung..."
                    run_record["completedAt"] = datetime.now().isoformat(timespec="seconds")
                    save_job(job)
                    return

            if _campaign_expired(job, started_at):
                run_record["action"] = "campaign_expired"
                run_record["status"] = "done"
                job["lastNotice"] = "Chiến dịch đã hết thời gian chạy; hệ thống dừng gửi kết bạn và kiểm tra lại nhóm."
                _sync_job_schedule(job, started_at)
                run_record["completedAt"] = datetime.now().isoformat(timespec="seconds")
                save_job(job)
                return

            # Chỉ coi đợt mời là đến hạn dựa trên danh sách pending trước lúc
            # kiểm tra. Nếu lần kiểm tra phát hiện ai vừa rời nhóm, người đó được
            # đưa vào đợt hằng ngày kế tiếp, không bị mời lại ngay trong cùng chu kỳ.
            # `_outreach_members` gom ca nhung nguoi da nhan the link nhom
            # nhung chua nhan loi moi ket ban (escalation). Neu chi xet
            # `pending` thi loi moi ket ban cua nhom nay se khong bao gio
            # duoc gui, vi ho khong con o trang thai "pending" nua.
            outreach_before_verify = _outreach_members(job)
            invite_due = bool(outreach_before_verify) and _is_due(
                str(job.get("nextInviteAt") or ""), started_at
            )

            # Rà soát NHANH: đọc danh sách thành viên nhóm đích để cập nhật
            # bảng thống kê ngay khi người nhận bấm link vào nhóm. Chu kỳ này
            # KHÔNG gọi API mời (chỉ đọc), nên chạy vài phút một lần rất an toàn.
            has_target = bool(str(job.get("targetGroupId") or "").strip())
            status_poll_at = str(job.get("nextStatusPollAt") or "").strip()
            status_poll_due = (
                has_target
                and not invite_due
                and bool(status_poll_at)
                and _is_due(status_poll_at, started_at)
            )

            # Mỗi chu kỳ cấu hình, thử add lại toàn bộ người đã được chiến dịch
            # gửi kết bạn, sau đó đọc nhóm đích để xác nhận trạng thái thực tế.
            # Nhánh này gọi API mời nên chỉ chạy khi chu kỳ thử add lại thực sự
            # đến hạn, KHÔNG chạy khi chỉ tới hạn lần rà soát nhanh.
            pre_verify = {"checked": False, "newlyJoined": 0, "leftCount": 0}
            pending_approval_result = {"attempted": 0, "approved": 0, "failed": 0}
            verify_due = (
                has_target
                and (
                    _is_due(str(job.get("nextVerifyAt") or ""), started_at)
                    or invite_due
                )
            )
            if verify_due and not (status_poll_due and not invite_due):
                # Người lạ bấm link nhóm khi nhóm bật phê duyệt sẽ nằm ở hàng
                # đợi. Duyệt giúp họ trước khi đọc danh sách thành viên để
                # trạng thái "đã vào nhóm" được ghi nhận ngay.
                pending_approval_result = _approve_pending_target_members(
                    job,
                    zpw_enk,
                    cookies,
                    imei,
                )
                run_record["pendingApprovedCount"] = int(pending_approval_result.get("approved") or 0)
                retry_result = _retry_awaiting_group_adds(
                    job,
                    zpw_enk,
                    cookies,
                    imei,
                )
                pre_verify = _verify_target_members(job, zpw_enk, cookies, imei, force=True)
                remove_result = _remove_joined_campaign_friends(job, zpw_enk, cookies, imei)
                run_record["retryAddCount"] = int(retry_result.get("attempted") or 0)
                run_record["friendRemovedCount"] = int(remove_result.get("removed") or 0)
                run_record["friendRemoveFailedCount"] = int(remove_result.get("failed") or 0)

            if invite_due:
                self._send_daily_batch(job, run_record, zpw_enk, cookies, imei, started_at)
            elif status_poll_due:
                # Rà soát nhanh: chỉ đọc danh sách thành viên nhóm đích và cập
                # nhật trạng thái. Không gửi lời mời, không gọi API thêm vào nhóm.
                run_record["action"] = "poll_target_members"
                # Cache có thể đã cũ -> bỏ để đọc thật, nếu không bảng thống kê
                # sẽ tiếp tục đứng yên.
                _invalidate_target_members_cache(str(job.get("targetGroupId") or ""))
                poll_result = _verify_target_members(
                    job, zpw_enk, cookies, imei, force=True, reschedule_verify=False
                )
                run_record["newlyJoinedCount"] = int(poll_result.get("newlyJoined") or 0)
                run_record["status"] = "done" if not poll_result.get("error") else "partial"
                run_record["error"] = str(poll_result.get("error") or "")
                job["lastNotice"] = self._progress_notice(
                    job,
                    prefix=(
                        f"Đã rà soát nhóm đích; {int(poll_result.get('newlyJoined') or 0)} "
                        "người vừa vào nhóm."
                    ),
                )
            else:
                run_record["action"] = "retry_add_verify_and_cleanup"
                verify_result = pre_verify
                if not verify_result.get("checked") and has_target:
                    verify_result = _verify_target_members(job, zpw_enk, cookies, imei, force=True)
                run_record["newlyJoinedCount"] = int(verify_result.get("newlyJoined") or 0)
                run_record["status"] = "done" if not verify_result.get("error") else "partial"
                run_record["error"] = str(verify_result.get("error") or "")
                prefix = (
                    f"Đã thử add lại {int(run_record.get('retryAddCount') or 0)} người đang chờ; "
                    f"duyệt {int(run_record.get('pendingApprovedCount') or 0)} người đang chờ duyệt; "
                    f"xóa kết bạn {int(run_record.get('friendRemovedCount') or 0)} người theo thiết lập."
                )
                job["lastNotice"] = self._progress_notice(job, prefix=prefix)

            _sync_job_schedule(job, datetime.now())
            run_record["completedAt"] = datetime.now().isoformat(timespec="seconds")
            # save_job sẽ tính lại joinedCount/conversionRate từ danh sách thành viên.
            saved = save_job(job)
            prev_joined = int(run_record.get("joinedCount") or 0)
            prev_rate = float(run_record.get("conversionRate") or 0)
            run_record["joinedCount"] = int(saved.get("joinedCount") or 0)
            run_record["conversionRate"] = float(saved.get("conversionRate") or 0)
            # Lần lưu thứ hai này trước đây luôn chạy -> ghi file 2 lần mỗi
            # chu kỳ dù nội dung gần như không đổi. Giờ chỉ lưu khi số liệu
            # thực sự thay đổi so với lần lưu đầu.
            if (
                int(run_record.get("joinedCount") or 0) != prev_joined
                or float(run_record.get("conversionRate") or 0) != prev_rate
            ):
                save_job(saved)
            print(
                f"[group_copy_worker] {job_id}: action={run_record.get('action')} "
                f"sent={run_record.get('sentCount')} joined={saved.get('joinedCount')} "
                f"conversion={saved.get('conversionRate')}% status={saved.get('status')}",
                flush=True,
            )
        except Exception as exc:
            run_record["completedAt"] = datetime.now().isoformat(timespec="seconds")
            run_record["status"] = "failed"
            run_record["error"] = str(exc)
            job["status"] = "failed"
            job["lastError"] = str(exc)
            job["lastNotice"] = ""
            save_job(job)
            print(f"[group_copy_worker] {job_id} failed: {exc}", flush=True)

    def _send_daily_batch(
        self,
        job: dict,
        run_record: dict,
        zpw_enk: str,
        cookies: str,
        imei: str,
        started_at: datetime,
    ) -> None:
        pending = _outreach_members(job)
        if not pending:
            run_record["action"] = "verify_target"
            run_record["status"] = "done"
            return

        target_mode = str(job.get("targetMode") or "existing")
        creating_new_group = target_mode == "new" and not str(job.get("targetGroupId") or "").strip()
        run_record["memberIds"] = [str(member.get("userId") or "").strip() for member in pending]
        run_record["friendRequestDailyLimit"] = _daily_limit(job)
        run_record["directInviteCount"] = 0
        run_record["friendRequestCount"] = 0
        run_record["friendRequestFailedCount"] = 0
        run_record["linkCardCount"] = 0
        run_record["linkCardFailedCount"] = 0

        # Cập nhật quan hệ bạn bè TRƯỚC khi gọi bất kỳ API nào. Danh sách bạn bè
        # thật là nguồn duy nhất đáng tin; cờ isFr từ getmg có thể gắn nhầm
        # người lạ thành bạn bè và khiến Zalo trả lỗi 314 cho cả batch.
        friend_uids = _refresh_friend_status(job, zpw_enk, cookies, imei)

        if creating_new_group:
            group_name = str(job.get("newGroupName") or job.get("targetGroupName") or "Nhóm mới").strip()
            # Chỉ dùng bạn bè thật làm thành viên khởi tạo: tạo nhóm kèm người lạ
            # sẽ khiến Zalo từ chối toàn bộ request (mã 314).
            seed_ids = [
                str(member.get("userId") or "").strip()
                for member in pending
                if member.get("isFriend") is True
                and str(member.get("userId") or "").strip()
            ][:50]
            if not seed_ids and friend_uids:
                # Có bạn bè nhưng không ai trong nhóm nguồn -> thử tạo nhóm trống.
                seed_ids = []
            response_json, decoded_raw = create_group(
                group_name,
                seed_ids,
                zpw_enk,
                cookies,
                imei,
                zpw_ver=get_zpw_ver(),
            )
            response_json = response_json if isinstance(response_json, dict) else {}
            decoded = _decoded_dict(decoded_raw)
            group_id = _extract_group_id(response_json, decoded)
            error_code = _error_code(response_json, decoded)
            error_message = _error_message(response_json, decoded)
            if not group_id:
                if error_code in (314, 269):
                    raise RuntimeError(
                        "Zalo không cho tạo nhóm kèm người chưa kết bạn (mã "
                        f"{error_code}). Cần ít nhất 1 bạn bè trong nhóm nguồn, "
                        "sau đó hệ thống sẽ mời người lạ qua link nhóm."
                    )
                raise RuntimeError(
                    error_message
                    or (
                        f"Zalo trả về mã lỗi {error_code} khi tạo nhóm mới."
                        if error_code != 0
                        else "Tạo nhóm thành công nhưng không đọc được Group ID."
                    )
                )

            job["targetGroupId"] = group_id
            job["targetGroupName"] = str(job.get("newGroupName") or job.get("targetGroupName") or group_id)
            job["groupCreatedAt"] = datetime.now().isoformat(timespec="seconds")
            run_record["action"] = "create_group_get_link_and_copy"
            group_link = _ensure_group_link(
                job,
                zpw_enk,
                cookies,
                imei,
                newly_created=True,
                force=True,
            )
            # Xác nhận ngay những người đã được thêm trong request tạo nhóm.
            _verify_target_members(job, zpw_enk, cookies, imei, force=True)
        else:
            target_group_id = str(job.get("targetGroupId") or "").strip()
            if not target_group_id:
                raise ValueError("Tác vụ chưa có nhóm đích.")
            run_record["action"] = "get_link_invite_friends_and_send_friend_requests"
            group_link = _ensure_group_link(
                job,
                zpw_enk,
                cookies,
                imei,
                newly_created=False,
            )

        run_record["targetGroupId"] = str(job.get("targetGroupId") or "")
        run_record["groupLink"] = group_link

        # Sau bước tạo nhóm/kiểm tra ban đầu, chỉ xử lý những người chưa có mặt.
        # CHỈ bạn bè thật mới được gọi API thêm trực tiếp. Người lạ sẽ khiến
        # Zalo trả lỗi 314 cho toàn bộ batch, nên tách riêng và đi đường link.
        direct_candidates = [
            member for member in _pending_members(job)
            if member.get("isFriend") is True
        ]
        direct_ids = [
            str(member.get("userId") or "").strip()
            for member in direct_candidates
            if str(member.get("userId") or "").strip()
        ]
        direct_result = {
            "members": [],
            "acceptedCount": 0,
            "failedCount": 0,
        }
        if direct_ids:
            direct_result = invite_members_to_group(
                str(job.get("targetGroupId") or ""),
                direct_ids,
                imei,
                zpw_enk,
                cookies,
                zpw_ver=get_zpw_ver(),
                batch_size=50,
            )
            # Danh sách thành viên nhóm đích vừa thay đổi -> cache cũ không
            # còn đúng, buộc phải đọc lại ở lần verify tiếp theo.
            _invalidate_target_members_cache(str(job.get("targetGroupId") or ""))

        attempted_at = int(time.time() * 1000)
        members_by_uid = _member_map(job)
        direct_by_uid = {
            str(item.get("userId") or ""): item
            for item in (direct_result.get("members") or [])
            if str(item.get("userId") or "").strip()
        }
        for uid in direct_ids:
            member = members_by_uid.get(uid)
            if not member:
                continue
            result = direct_by_uid.get(uid) or {
                "status": "failed",
                "code": -1,
                "delivery": "failed",
                "message": "Không đọc được kết quả thêm trực tiếp vào nhóm.",
            }
            member["attemptedAt"] = attempted_at
            member["inviteAttempts"] = int(member.get("inviteAttempts") or 0) + 1
            member["inviteResultCode"] = result.get("code", -1)
            member["inviteResultMessage"] = str(result.get("message") or "").strip()
            member["inviteDelivery"] = str(result.get("delivery") or "")
            # Chưa đánh dấu invited ở đây. Chỉ khi kiểm tra thấy UID có trong nhóm
            # mới coi là thêm trực tiếp thành công; nếu chưa có sẽ gửi kết bạn
            # theo hạn mức hằng ngày và thử add lại theo chu kỳ.
            if str(result.get("status") or "") == "joined":
                member["status"] = "joined"
                member["joinedAt"] = member.get("joinedAt") or attempted_at
                member["error"] = ""
            else:
                member["status"] = "pending"
                member["error"] = str(result.get("message") or "Chưa thêm trực tiếp được vào nhóm.").strip()

        run_record["directInviteCount"] = len(direct_ids)
        verify_after_direct = _verify_target_members(job, zpw_enk, cookies, imei, force=True)
        run_record["newlyJoinedCount"] = int(verify_after_direct.get("newlyJoined") or 0)

        # Hạn mức tính theo NGƯỜI được tiếp cận, không theo số tin nhắn. Một
        # người lạ có thể nhận cả thẻ link nhóm lẫn lời mời kết bạn trong
        # cùng một đợt mà vẫn chỉ tính 1 lượt.
        remaining_quota = _remaining_reach_today(job, started_at)
        # Gửi lời mời kết bạn kèm link ngay cùng đợt gửi thẻ link nhóm.
        # Tắt (False) thì chỉ gửi thẻ link, lời mời kết bạn để dành cho đợt sau.
        friend_request_with_link = job.get("friendRequestWithLink") is not False
        friend_request_sent = 0
        friend_request_failed = 0
        quota_deferred = 0
        friend_text = _friend_request_text(job, group_link)

        # Người lạ (chưa kết bạn): Zalo chặn add trực tiếp (mã 314). Hai kênh
        # Zalo chấp nhận với người lạ:
        #   1. Thẻ link nhóm trong tin nhắn 1-1 (/api/message/link, code 0) —
        #      người nhận bấm là vào nhóm ngay, không cần kết bạn trước.
        #   2. Lời mời kết bạn kèm link nhóm — sau khi họ chấp nhận, ta add
        #      trực tiếp được và xóa bạn theo thiết lập được.
        # Mỗi người chỉ nhận 1 lần tiếp cận mỗi đợt, ưu tiên người chưa từng
        # được liên hệ để mở rộng tối đa trong hạn mức hằng ngày.
        pending_members = list(_outreach_members(job))
        pending_members.sort(
            key=lambda member: (
                0 if str(member.get("messageDelivery") or "") != "sent"
                and str(member.get("friendRequestDelivery") or "") != "friend_request_with_group_link"
                else 1
            )
        )

        link_card_sent = 0
        link_card_failed = 0
        link_info = None
        for member in pending_members:
            uid = str(member.get("userId") or "").strip()
            if not uid:
                continue
            if remaining_quota <= 0:
                quota_deferred += 1
                member["error"] = (
                    "Da dat han muc tiep can hom nay; se tiep tuc vao ngay ke tiep."
                )
                continue

            already_link_card = str(member.get("messageDelivery") or "") == "sent"
            already_friend_request = (
                str(member.get("friendRequestDelivery") or "") == "friend_request_with_group_link"
            )

            # Da gui ca hai kenh -> cho phan hoi, khong gui lai.
            if already_link_card and already_friend_request:
                member["error"] = (
                    "Da gui link nhom va loi moi ket ban; dang cho nguoi nay "
                    "chap nhan hoac tu mo link vao nhom."
                )
                continue

            # Uu tien the link nhom cho nguoi la: day la kenh duy nhat dua
            # nguoi la vao nhom ma khong can ho chap nhan loi moi ket ban.
            # Ban be that da goi API add truc tiep o tren; neu add truc tiep
            # khong duoc thi the link nhom van la kenh du phong hieu qua.
            link_card_ok = already_link_card
            if not already_link_card and group_link:
                if link_info is None:
                    try:
                        from features.messaging.send_link import resolve_group_link_info

                        link_info = resolve_group_link_info(
                            group_link,
                            zpw_enk,
                            cookies,
                            zpw_ver=get_zpw_ver(),
                            imei=imei,
                        )
                    except Exception as exc:
                        print(
                            f"[group_copy_worker] Khong lay duoc metadata link nhom: {exc}",
                            flush=True,
                        )
                        link_info = None

                _consume_link_card_slot(job, started_at)
                _consume_reach_slot(job, started_at)
                remaining_quota -= 1
                ok, code, message = _send_group_link_card(
                    uid,
                    job,
                    group_link,
                    zpw_enk,
                    cookies,
                    imei,
                    link_info=link_info,
                    member=member,
                )
                member["messageAttempts"] = int(member.get("messageAttempts") or 0) + 1
                member["messageAt"] = int(time.time() * 1000)
                member["messageCode"] = code
                member["messageMessage"] = message
                member["messageDelivery"] = "sent" if ok else "failed"
                if ok:
                    link_card_sent += 1
                    link_card_ok = True
                    member["status"] = "invited"
                    member["invitedAt"] = member.get("invitedAt") or member["messageAt"]
                    member["error"] = (
                        "Da gui the link nhom. Nguoi nay chi can bam la vao nhom, "
                        "khong can ket ban truoc."
                    )
                else:
                    link_card_failed += 1
                    member["status"] = "pending"
                    member["error"] = message or "Khong gui duoc the link nhom."
                    # Khong toi noi -> hoan lai luot han muc da tru.
                    if code in _DELIVERY_FAILED_CODES:
                        _refund_link_card_slot(job, started_at)
                        _refund_reach_slot(job, started_at)
                        remaining_quota += 1

            # Gui loi moi ket ban kem link nhom (neu chua gui). Kenh nay mo
            # duong add truc tiep ve sau, khi ho chap nhan loi moi ket ban.
            # Gui NGAY trong cung dot voi the link de nguoi nhan co ca hai
            # duong vao nhom, khong phai doi toi dot hang ngay ke tiep.
            if not already_friend_request and friend_request_with_link:
                if remaining_quota <= 0:
                    quota_deferred += 1
                    continue
                _consume_friend_request_slot(job, started_at)
                remaining_quota -= 1
                member["friendRequestAttempts"] = int(member.get("friendRequestAttempts") or 0) + 1
                member["friendRequestAt"] = int(time.time() * 1000)
                try:
                    response_json, decoded_raw = send_friend_request(
                        toid=uid,
                        msg=friend_text,
                        imei=imei,
                        zpw_enk=zpw_enk,
                        cookies=cookies,
                        zpw_ver=get_zpw_ver(),
                    )
                    success, code, message = _friend_request_result(response_json, decoded_raw)
                except Exception as exc:
                    success, code, message = False, -1, str(exc)

                member["friendRequestCode"] = code
                member["friendRequestMessage"] = message
                if success:
                    member["status"] = "invited"
                    member["invitedAt"] = member.get("invitedAt") or member["friendRequestAt"]
                    member["friendRequestDelivery"] = "friend_request_with_group_link"
                    member["friendRequestCreatedByCampaign"] = True
                    member["friendRemoveAttempts"] = 0
                    member["friendRemoveAt"] = 0
                    member["friendRemoveCode"] = -1
                    member["friendRemoveMessage"] = ""
                    member["friendRemoveDelivery"] = ""
                    member["joinedAfterCampaignAddRetry"] = False
                    if link_card_ok:
                        member["error"] = (
                            "Da gui the link nhom kem loi moi ket ban. He thong se "
                            f"thu add lai vao nhom moi {_verify_interval(job)} phut."
                        )
                    else:
                        member["error"] = (
                            "Da gui loi moi ket ban kem link nhom. He thong se thu "
                            f"add lai vao nhom moi {_verify_interval(job)} phut."
                        )
                    friend_request_sent += 1
                else:
                    member["status"] = "pending"
                    member["friendRequestDelivery"] = "failed"
                    member["error"] = message or "Zalo tu choi gui loi moi ket ban."
                    friend_request_failed += 1
                    if code in _DELIVERY_FAILED_CODES:
                        _refund_friend_request_slot(job, started_at)
                        remaining_quota += 1
                        # Khong kenh nao toi noi -> hoan lai ca luot tiep can.
                        if not link_card_ok:
                            _refund_reach_slot(job, started_at)
                            remaining_quota += 1

        run_record["linkCardCount"] = link_card_sent
        run_record["linkCardFailedCount"] = link_card_failed

        # Người đã là bạn bè nhưng add trực tiếp chưa được (lời mời còn trong
        # hộp thư chờ, nhóm chờ duyệt, ...) -> gửi lời mời kết bạn như một
        # lựa chọn dự phòng vẫn có tác dụng.
        for member in list(_pending_members(job)):
            uid = str(member.get("userId") or "").strip()
            if not uid:
                continue
            if member.get("isFriend") is not True:
                continue
            if str(member.get("friendRequestDelivery") or "") == "friend_request_with_group_link":
                continue
            if remaining_quota <= 0:
                quota_deferred += 1
                member["error"] = (
                    "Đã đạt hạn mức tiếp cận hôm nay; sẽ tiếp tục vào ngày kế tiếp."
                )
                continue

            _consume_friend_request_slot(job, started_at)
            _consume_reach_slot(job, started_at)
            remaining_quota -= 1
            member["friendRequestAttempts"] = int(member.get("friendRequestAttempts") or 0) + 1
            member["friendRequestAt"] = int(time.time() * 1000)
            try:
                response_json, decoded_raw = send_friend_request(
                    toid=uid,
                    msg=friend_text,
                    imei=imei,
                    zpw_enk=zpw_enk,
                    cookies=cookies,
                    zpw_ver=get_zpw_ver(),
                )
                success, code, message = _friend_request_result(response_json, decoded_raw)
            except Exception as exc:
                success, code, message = False, -1, str(exc)

            member["friendRequestCode"] = code
            member["friendRequestMessage"] = message
            if success:
                member["status"] = "invited"
                member["invitedAt"] = member.get("invitedAt") or member["friendRequestAt"]
                member["friendRequestDelivery"] = "friend_request_with_group_link"
                member["friendRequestCreatedByCampaign"] = True
                member["friendRemoveAttempts"] = 0
                member["friendRemovedAt"] = 0
                member["friendRemoveCode"] = -1
                member["friendRemoveMessage"] = ""
                member["friendRemoveDelivery"] = ""
                member["joinedAfterCampaignAddRetry"] = False
                member["error"] = (
                    "Đã gửi lời mời kết bạn. Hệ thống sẽ thử add lại vào nhóm "
                    f"mỗi {_verify_interval(job)} phút."
                )
                friend_request_sent += 1
            else:
                member["status"] = "pending"
                member["friendRequestDelivery"] = "failed"
                member["error"] = message or "Zalo từ chối gửi lời mời kết bạn."
                friend_request_failed += 1
                if code in _DELIVERY_FAILED_CODES:
                    _refund_friend_request_slot(job, started_at)
                    _refund_reach_slot(job, started_at)
                    remaining_quota += 1

        run_record["friendRequestCount"] = friend_request_sent
        run_record["friendRequestFailedCount"] = friend_request_failed
        run_record["dailyFriendRequestCount"] = int(job.get("dailyFriendRequestCount") or 0)
        # `sentCount` phải phản ánh TẤT CẢ tin nhắn đã gửi ra, không chỉ lời
        # mời kết bạn. Thẻ link nhóm (kênh chính cho người lạ) trước đây bị
        # bỏ sót nên báo cáo cho ra 0 lượt gửi dù đã gửi thành công.
        run_record["sentCount"] = friend_request_sent + link_card_sent
        run_record["outreachCount"] = friend_request_sent + link_card_sent
        run_record["failedCount"] = friend_request_failed + link_card_failed
        run_record["deferredCount"] = quota_deferred
        run_record["status"] = "done" if (friend_request_failed + link_card_failed) == 0 else "partial"
        run_record["error"] = (
            "" if (friend_request_failed + link_card_failed) == 0
            else (
                f"Có {friend_request_failed + link_card_failed} tin nhắn chưa gửi được; "
                "hệ thống sẽ thử lại vào đợt sau."
            )
        )

        # Con nguoi can tiep can -> lich dot moi. Neu chi con nguoi da nhan
        # the link nhom ma chua nhan loi moi ket ban, va con han muc hom nay,
        # thi cho phep gui bu loi moi ket ban ngay (khong phai doi sang ngay
        # mai) - do la thoi diem quan trong nhat de ti le vao nhom cao hon.
        remaining_after = _outreach_members(job)
        if remaining_after:
            if (
                _escalation_members(job)
                and not _pending_members(job)
                and _remaining_reach_today(job, started_at) > 0
            ):
                job["nextInviteAt"] = started_at.isoformat(timespec="seconds")
            else:
                job["nextInviteAt"] = _next_daily_run(job, started_at)
        else:
            job["nextInviteAt"] = ""

        # Kiểm tra lần cuối để cập nhật trường hợp thành viên vừa vào nhóm sau khi
        # nhận lời mời trực tiếp hoặc tự mở link trong lời mời kết bạn.
        # Danh sách nhóm có thể vừa thay đổi (vừa add trực tiếp, vừa duyệt
        # người chờ) nên phải bỏ cache trước khi đọc lại.
        _invalidate_target_members_cache(str(job.get("targetGroupId") or ""))
        final_verify = _verify_target_members(job, zpw_enk, cookies, imei, force=True)
        run_record["newlyJoinedCount"] += int(final_verify.get("newlyJoined") or 0)
        remove_result = _remove_joined_campaign_friends(job, zpw_enk, cookies, imei)
        run_record["friendRemovedCount"] = int(run_record.get("friendRemovedCount") or 0) + int(remove_result.get("removed") or 0)
        run_record["friendRemoveFailedCount"] = int(run_record.get("friendRemoveFailedCount") or 0) + int(remove_result.get("failed") or 0)
        job["lastError"] = ""
        prefix = (
            f"Đã tạo nhóm, kích hoạt link và thử thêm trực tiếp {len(direct_ids)} người"
            if creating_new_group
            else f"Đã lấy link nhóm và thử thêm trực tiếp {len(direct_ids)} người"
        )
        if link_card_sent or link_card_failed:
            prefix += (
                f"; đã gửi thẻ link nhóm cho {link_card_sent} người lạ"
                + (f" ({link_card_failed} lỗi)" if link_card_failed else "")
            )
        prefix += f"; đã gửi {friend_request_sent}/{_daily_limit(job)} lời mời kết bạn hôm nay"
        if quota_deferred:
            prefix += f", còn {quota_deferred} người chờ hạn mức ngày kế tiếp"
        prefix += "."
        job["lastNotice"] = self._progress_notice(job, prefix=prefix)

    @staticmethod
    def _progress_notice(job: dict, prefix: str = "") -> str:
        total = len(job.get("members") or [])
        joined = sum(1 for m in (job.get("members") or []) if m.get("status") == "joined")
        invited = sum(1 for m in (job.get("members") or []) if m.get("status") == "invited")
        pending = sum(1 for m in (job.get("members") or []) if m.get("status") == "pending")
        rate = round((joined * 100 / total), 1) if total else 0.0
        parts = [prefix.strip()] if prefix.strip() else []
        parts.append(
            f"Thực tế đã vào nhóm {joined}/{total} người ({rate}%). "
            f"Đang chờ chấp nhận {invited}, chưa gửi {pending}."
        )
        if job.get("lastVerificationError"):
            parts.append(f"Lần kiểm tra gần nhất chưa thành công: {job.get('lastVerificationError')}")
        else:
            parts.append(f"Hệ thống sẽ kiểm tra lại mỗi {_verify_interval(job)} phút.")
        return " ".join(parts)


_WORKER: Optional[GroupCopyWorker] = None


def start_group_copy_worker() -> None:
    global _WORKER
    recovered = recover_running_jobs()
    if recovered:
        print(f"[group-copy-worker] Đã khôi phục {recovered} tác vụ đang chạy dở.", flush=True)
    if _WORKER is None:
        _WORKER = GroupCopyWorker()
    _WORKER.start()


def stop_group_copy_worker() -> None:
    if _WORKER:
        _WORKER.stop()
