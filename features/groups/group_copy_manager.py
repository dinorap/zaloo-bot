"""Lưu trữ và quản lý tiến độ tác vụ sao chép thành viên giữa các nhóm Zalo."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── Lưu trữ tách file theo từng tác vụ ──────────────────────────────────────
# Vấn đề: trước đây MỌI thao tác lưu đều ghi lại toàn bộ danh sách tác vụ vào
# một file JSON duy nhất. Với 10 tác vụ x 500 thành viên, mỗi lần lưu phải
# serialize + ghi ~3.5MB, và mỗi lần đọc phải parse lại 3.5MB. Đây là nguyên
# nhân chính khiến ứng dụng "chậm" khi chiến dịch lớn.
#
# Cách sửa:
#   - JOBS_FILE  -> file CHỈ chứa phần tóm tắt (không có "members"), nhỏ gọn.
#   - JOBS_DIR   -> mỗi tác vụ một file riêng <jobId>.json chứa đầy đủ.
# Khi lưu chỉ ghi lại đúng 1 file tác vụ thay vì ghi lại tất cả.
#
# Tương thích ngược: nếu file cũ vẫn tồn tại và chứa "members", nó được tự
# động tách ra thành các file riêng ngay lần đọc đầu tiên.
BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
JOBS_FILE = os.path.join(DATA_DIR, "group_copy_jobs.json")
JOBS_DIR = os.path.join(DATA_DIR, "group_copy_jobs")
_LOCK = threading.RLock()

_TERMINAL_STATUSES = {"done", "partial", "expired", "failed", "cancelled"}
_MEMBER_STATUSES = {"pending", "invited", "joined", "failed", "skipped"}

# Các chỉ số tiến độ được tính lại từ danh sách thành viên. Dùng chung cho
# cả job có members và job chỉ có phần tóm tắt (đường đọc nóng của frontend).
_PROGRESS_FIELDS = (
    "totalMembers",
    "friendCount",
    "pendingInviteCount",
    "eligibleCount",
    "waitingFriendCount",
    "invitedCount",
    "inboxInviteCount",
    "awaitingJoinCount",
    "joinedCount",
    "successCount",
    "acceptedInviteCount",
    "preExistingCount",
    "campaignFriendCount",
    "messageSentCount",
    "removedFriendCount",
    "failedCount",
    "skippedCount",
    "remainingCount",
    "pendingCount",
)

# Cache trong bộ nhớ: danh sách job đầy đủ (kèm members).
_CACHE: Optional[list[dict]] = None
_CACHE_MTIME: Optional[float] = None
# Cache phần tóm tắt (không kèm members) cho đường đọc nóng.
_SUMMARY_CACHE: Optional[list[dict]] = None
_SUMMARY_MTIME: Optional[float] = None


def _file_mtime() -> Optional[float]:
    try:
        return os.path.getmtime(JOBS_FILE)
    except OSError:
        return None


def _job_file_path(job_id: str) -> str:
    safe = "".join(ch for ch in str(job_id or "") if ch.isalnum() or ch in ("-", "_"))
    return os.path.join(JOBS_DIR, f"{safe or 'unknown'}.json")


def _invalidate_cache() -> None:
    global _CACHE, _CACHE_MTIME, _SUMMARY_CACHE, _SUMMARY_MTIME
    _CACHE = None
    _CACHE_MTIME = None
    _SUMMARY_CACHE = None
    _SUMMARY_MTIME = None


def _split_legacy_store_unlocked() -> list[dict]:
    """Tách file cũ (chứa cả members) thành các file riêng theo từng tác vụ.

    Chạy một lần duy nhất khi phát hiện dữ liệu cũ. Sau đó JOBS_FILE chỉ còn
    phần tóm tắt và mỗi tác vụ nằm ở file riêng.
    """
    global _CACHE, _CACHE_MTIME, _SUMMARY_CACHE, _SUMMARY_MTIME

    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
    except OSError:
        pass

    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    jobs = data.get("jobs", data if isinstance(data, list) else [])
    if not isinstance(jobs, list):
        return []

    has_members = any(
        isinstance(job, dict) and isinstance(job.get("members"), list) and job.get("members")
        for job in jobs
    )
    if not has_members:
        return jobs

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("jobId") or "").strip()
        if not job_id:
            continue
        path = _job_file_path(job_id)
        try:
            temp_path = path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            os.replace(temp_path, path)
        except OSError:
            pass

    # Ghi lại file tóm tắt (không kèm members).
    summaries = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        summary = dict(job)
        summary.pop("members", None)
        summaries.append(summary)
    try:
        temp_path = JOBS_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"jobs": summaries}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, JOBS_FILE)
    except OSError:
        pass

    _CACHE = None
    _CACHE_MTIME = None
    _SUMMARY_CACHE = None
    _SUMMARY_MTIME = None
    return jobs


def _load_summaries_unlocked() -> list[dict]:
    """Đọc danh sách tóm tắt tác vụ (không kèm members) — rất nhỏ gọn."""
    global _SUMMARY_CACHE, _SUMMARY_MTIME
    if _SUMMARY_CACHE is not None:
        current_mtime = _file_mtime()
        if current_mtime == _SUMMARY_MTIME:
            return _SUMMARY_CACHE
        _SUMMARY_CACHE = None
        _SUMMARY_MTIME = None

    _ensure_store()
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        jobs = data.get("jobs", data if isinstance(data, list) else [])
        if not isinstance(jobs, list):
            jobs = []
    except (OSError, json.JSONDecodeError):
        jobs = []

    _SUMMARY_CACHE = jobs
    _SUMMARY_MTIME = _file_mtime()
    return _SUMMARY_CACHE


def _load_job_detail_unlocked(job_id: str) -> Optional[dict]:
    """Đọc đầy đủ một tác vụ từ file riêng của nó."""
    path = _job_file_path(job_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
        return job if isinstance(job, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _load_jobs_unlocked() -> list[dict]:
    """Trả về danh sách job đầy đủ (kèm members), nạp từ cache nếu có."""
    global _CACHE, _CACHE_MTIME
    if _CACHE is not None:
        return _CACHE

    summaries = _split_legacy_store_unlocked()
    if not summaries:
        summaries = _load_summaries_unlocked()

    jobs: list[dict] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        job_id = str(summary.get("jobId") or "").strip()
        detail = _load_job_detail_unlocked(job_id) if job_id else None
        if detail is None:
            detail = summary
        merged = dict(summary)
        merged.update(detail)
        jobs.append(merged)

    _CACHE = jobs
    _CACHE_MTIME = _file_mtime()
    return _CACHE


def _flush_jobs_unlocked(jobs: list[dict], dirty_job_ids: Optional[set[str]] = None) -> None:
    """Ghi thay đổi ra đĩa.

    Tối ưu quan trọng: chỉ ghi lại các file tác vụ thực sự thay đổi
    (``dirty_job_ids``). Worker thường chỉ sửa 1 tác vụ trong mỗi chu kỳ,
    nên ghi lại toàn bộ (hàng chục file, vài MB) là lãng phí I/O.
    """
    global _CACHE, _CACHE_MTIME, _SUMMARY_CACHE, _SUMMARY_MTIME

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)

    summaries = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("jobId") or "").strip()
        summary = dict(job)
        summary.pop("members", None)
        summaries.append(summary)
        if not job_id:
            continue
        # Nếu biết rõ job nào đổi thì chỉ ghi file đó.
        if dirty_job_ids is not None and job_id not in dirty_job_ids:
            continue
        path = _job_file_path(job_id)
        try:
            temp_path = path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            os.replace(temp_path, path)
        except OSError:
            pass

    try:
        temp_path = JOBS_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"jobs": summaries}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, JOBS_FILE)
    except OSError:
        pass

    _CACHE = jobs
    _CACHE_MTIME = _file_mtime()
    _SUMMARY_CACHE = summaries
    _SUMMARY_MTIME = _file_mtime()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_store() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(JOBS_FILE):
        _write_jobs_unlocked([])


def _write_jobs_unlocked(jobs: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_path = JOBS_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({"jobs": jobs}, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, JOBS_FILE)


def _read_jobs_unlocked() -> list[dict]:
    """Tương thích ngược: đọc job từ cache trong RAM."""
    return _load_jobs_unlocked()


def _normalize_friend_state(value):
    """Giữ trạng thái bạn bè để ưu tiên thêm trực tiếp vào nhóm."""
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "friend", "friends"}:
        return True
    if text in {"0", "false", "no", "not_friend", "not-friend"}:
        return False
    return None


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_member(item) -> Optional[dict]:
    if isinstance(item, str):
        uid = item.strip()
        if not uid:
            return None
        item = {"userId": uid, "zaloName": uid}
    if not isinstance(item, dict):
        return None

    uid = str(item.get("userId") or item.get("uid") or item.get("id") or "").strip()
    if not uid:
        return None

    friend_state = _normalize_friend_state(
        item.get("isFriend", item.get("isFr", item.get("friend")))
    )
    status = str(item.get("status") or "pending").strip().lower()

    # Tương thích dữ liệu từ các bản cũ.
    if status == "waiting_friend":
        status = "pending"
        item = dict(item)
        item["error"] = ""
    elif status == "success":
        status = "joined"
    if status not in _MEMBER_STATUSES:
        status = "pending"

    invited_at = int(item.get("invitedAt") or item.get("attemptedAt") or 0)
    joined_at = int(item.get("joinedAt") or 0)
    invite_attempts = max(0, _to_int(item.get("inviteAttempts"), 0))
    if status in {"invited", "joined"} and invite_attempts == 0 and invited_at:
        invite_attempts = 1

    invite_result_code = _to_int(item.get("inviteResultCode", item.get("inviteCode")), -1)
    invite_delivery = str(item.get("inviteDelivery") or "").strip()
    invite_result_message = str(item.get("inviteResultMessage") or "").strip()
    friend_request_attempts = max(0, _to_int(item.get("friendRequestAttempts"), 0))
    friend_request_at = int(item.get("friendRequestAt") or 0)
    friend_request_code = _to_int(item.get("friendRequestCode"), -1)
    friend_request_message = str(item.get("friendRequestMessage") or "").strip()
    friend_request_delivery = str(item.get("friendRequestDelivery") or "").strip()
    friend_request_created_by_campaign = bool(item.get("friendRequestCreatedByCampaign")) or (
        friend_request_attempts > 0
        and friend_request_delivery == "friend_request_with_group_link"
    )
    message_attempts = max(0, _to_int(item.get("messageAttempts"), 0))
    message_at = int(item.get("messageAt") or 0)
    message_code = _to_int(item.get("messageCode"), -1)
    message_message = str(item.get("messageMessage") or "").strip()
    message_delivery = str(item.get("messageDelivery") or "").strip()
    friend_remove_attempts = max(0, _to_int(item.get("friendRemoveAttempts"), 0))
    friend_removed_at = int(item.get("friendRemovedAt") or 0)
    friend_remove_code = _to_int(item.get("friendRemoveCode"), -1)
    friend_remove_message = str(item.get("friendRemoveMessage") or "").strip()
    friend_remove_delivery = str(item.get("friendRemoveDelivery") or "").strip()
    campaign_add_retry_attempts = max(0, _to_int(item.get("campaignAddRetryAttempts"), 0))
    campaign_add_retry_at = int(item.get("campaignAddRetryAt") or 0)
    joined_after_campaign_add_retry = bool(item.get("joinedAfterCampaignAddRetry"))

    # Chuyển dữ liệu cũ từng đánh dấu nhầm 262/263 là lỗi sang trạng thái đã gửi.
    if status == "failed" and invite_result_code in {262, 263, 240, 252}:
        status = "invited"
        item = dict(item)
        invite_delivery = "pending_inbox" if invite_result_code in {262, 263} else "pending_approval"
        invite_result_message = invite_result_message or (
            "Đã gửi lời mời vào tin nhắn chờ của người nhận."
            if invite_result_code in {262, 263}
            else "Đã gửi yêu cầu và đang chờ xử lý."
        )
        item["error"] = invite_result_message
    elif status == "failed" and invite_result_code == 178:
        status = "joined"
        item = dict(item)
        item["error"] = ""

    return {
        "userId": uid,
        "zaloName": str(item.get("zaloName") or item.get("displayName") or item.get("name") or uid).strip(),
        "displayName": str(item.get("displayName") or item.get("zaloName") or item.get("name") or uid).strip(),
        # Giới tính để xưng hô đúng (Anh/Chị) trong tin nhắn mời người lạ.
        # None = chưa xác định -> dùng "Anh/Chị".
        "gender": item.get("gender", None),
        "avatar": str(item.get("avatar") or item.get("avatarUrl") or "").strip(),
        "isFriend": friend_state,
        "status": status,
        "error": str(item.get("error") or "").strip(),
        "attemptedAt": int(item.get("attemptedAt") or invited_at or 0),
        "invitedAt": invited_at,
        "joinedAt": joined_at,
        "lastVerifiedAt": int(item.get("lastVerifiedAt") or 0),
        "leftDetectedAt": int(item.get("leftDetectedAt") or 0),
        "inviteAttempts": invite_attempts,
        "inviteResultCode": invite_result_code,
        "inviteResultMessage": invite_result_message,
        "inviteDelivery": invite_delivery,
        "friendRequestAttempts": friend_request_attempts,
        "friendRequestAt": friend_request_at,
        "friendRequestCode": friend_request_code,
        "friendRequestMessage": friend_request_message,
        "friendRequestDelivery": friend_request_delivery,
        "friendRequestCreatedByCampaign": friend_request_created_by_campaign,
        "messageAttempts": message_attempts,
        "messageAt": message_at,
        "messageCode": message_code,
        "messageMessage": message_message,
        "messageDelivery": message_delivery,
        "friendRemoveAttempts": friend_remove_attempts,
        "friendRemovedAt": friend_removed_at,
        "friendRemoveCode": friend_remove_code,
        "friendRemoveMessage": friend_remove_message,
        "friendRemoveDelivery": friend_remove_delivery,
        "campaignAddRetryAttempts": campaign_add_retry_attempts,
        "campaignAddRetryAt": campaign_add_retry_at,
        "joinedAfterCampaignAddRetry": joined_after_campaign_add_retry,
    }


def _refresh_progress(job: dict, *, mutate: bool = True) -> dict:
    """Tính lại toàn bộ chỉ số tiến độ từ danh sách thành viên.

    QUAN TRỌNG: hàm này CHỈ chạy được khi ``job`` có ``members``. Đường đọc
    tóm tắt (``list_jobs(include_members=False)``) cố tình KHÔNG nạp members
    để nhẹ và nhanh; nếu gọi ở đó thì mọi chỉ số bị ghi đè về 0 và bảng thống
    kê trên UI luôn hiển thị "0 người" dù chiến dịch đã có người vào nhóm.
    Vì vậy khi không có thành viên thì giữ nguyên chỉ số đã lưu sẵn.
    """
    members = job.get("members") or []
    if not isinstance(members, list):
        members = []
        if mutate:
            job["members"] = members

    if not members:
        # Không có dữ liệu thành viên -> không tính lại (sẽ xóa sạch số liệu).
        for key in _PROGRESS_FIELDS:
            job.setdefault(key, 0)
        job.setdefault("pendingFriendRequestCount", 0)
        job.setdefault("conversionRate", 0.0)
        job.setdefault("progressPercent", 0.0)
        job.setdefault("inviteConversionRate", 0.0)
        return job

    counts = {status: 0 for status in _MEMBER_STATUSES}
    friend_count = 0
    invited_sent_count = 0
    accepted_invite_count = 0
    preexisting_count = 0
    inbox_invite_count = 0
    campaign_friend_count = 0
    message_sent_count = 0
    removed_friend_count = 0

    for member in members:
        if not isinstance(member, dict):
            member = {}
        status = str(member.get("status") or "pending").lower()
        if status not in counts:
            status = "pending"
            if mutate:
                member["status"] = status
        counts[status] += 1
        if member.get("isFriend") is True:
            friend_count += 1
        # Inline _to_int cho vòng lặp nóng: giá trị đã là int thì khỏi gọi hàm.
        attempts = member.get("inviteAttempts") or 0
        if not isinstance(attempts, int) or attempts < 0:
            try:
                attempts = max(0, int(attempts))
            except (TypeError, ValueError):
                attempts = 0
        friend_request_attempts = member.get("friendRequestAttempts") or 0
        if not isinstance(friend_request_attempts, int) or friend_request_attempts < 0:
            try:
                friend_request_attempts = max(0, int(friend_request_attempts))
            except (TypeError, ValueError):
                friend_request_attempts = 0
        # Chỉ tính đã gửi khi Zalo thực sự chấp nhận yêu cầu. Thành viên failed
        # không còn bị cộng nhầm vào chỉ số đã gửi lời mời.
        if (attempts > 0 or friend_request_attempts > 0) and status in {"invited", "joined"}:
            invited_sent_count += 1
            if member.get("friendRequestDelivery") == "friend_request_with_group_link":
                inbox_invite_count += 1
            elif member.get("inviteDelivery") == "pending_inbox":
                inbox_invite_count += 1
            if status == "joined":
                accepted_invite_count += 1
        elif status == "joined":
            preexisting_count += 1
        if member.get("friendRequestCreatedByCampaign"):
            campaign_friend_count += 1
        if member.get("messageDelivery") == "sent":
            message_sent_count += 1
        if member.get("friendRemoveDelivery") == "removed":
            removed_friend_count += 1

    total = len(members)
    joined_count = counts["joined"]
    awaiting_join_count = counts["invited"]
    pending_invite_count = counts["pending"]
    active_count = pending_invite_count + awaiting_join_count
    # Số người đã nhận thẻ link nhóm nhưng CHƯA nhận lời mời kết bạn. Lưu
    # thành chỉ số riêng vì đường đọc tóm tắt không có "members" nhưng vẫn
    # cần biết để hẹn lịch gửi lời mời kết bạn.
    pending_friend_request_count = 0
    for member in members:
        if not isinstance(member, dict):
            continue
        if str(member.get("status") or "") == "joined":
            continue
        if member.get("isFriend") is True:
            continue
        if str(member.get("messageDelivery") or "") != "sent":
            continue
        if str(member.get("friendRequestDelivery") or "") == "friend_request_with_group_link":
            continue
        pending_friend_request_count += 1
    conversion_rate = round((joined_count * 100 / total), 1) if total else 0.0
    invite_conversion_rate = (
        round((accepted_invite_count * 100 / invited_sent_count), 1)
        if invited_sent_count else 0.0
    )

    job["totalMembers"] = total
    job["friendCount"] = friend_count
    job["pendingInviteCount"] = pending_invite_count
    job["eligibleCount"] = pending_invite_count  # tương thích UI/API cũ
    job["waitingFriendCount"] = awaiting_join_count
    job["invitedCount"] = invited_sent_count
    job["inboxInviteCount"] = inbox_invite_count
    job["awaitingJoinCount"] = awaiting_join_count
    job["joinedCount"] = joined_count
    job["successCount"] = joined_count  # tương thích UI/API cũ
    job["acceptedInviteCount"] = accepted_invite_count
    job["preExistingCount"] = preexisting_count
    job["campaignFriendCount"] = campaign_friend_count
    job["messageSentCount"] = message_sent_count
    job["removedFriendCount"] = removed_friend_count
    job["failedCount"] = counts["failed"]
    job["skippedCount"] = counts["skipped"]
    job["remainingCount"] = max(0, total - joined_count)
    # pendingCount bao gồm cả người đã gửi lời mời nhưng chưa thực sự vào nhóm,
    # để worker tiếp tục kiểm tra định kỳ.
    job["pendingCount"] = active_count
    job["pendingFriendRequestCount"] = pending_friend_request_count
    job["progressPercent"] = conversion_rate
    job["conversionRate"] = conversion_rate
    job["inviteConversionRate"] = invite_conversion_rate
    return job


def _extract_daily_time(start_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(start_at or "").strip())
        return parsed.strftime("%H:%M")
    except (TypeError, ValueError):
        return datetime.now().strftime("%H:%M")


def _earliest_iso(values: list[str]) -> str:
    clean = [str(value or "").strip() for value in values if str(value or "").strip()]
    return min(clean) if clean else ""


def _normalize_members(job: dict, raw_members: list) -> list[dict]:
    """Chuẩn hóa danh sách thành viên, bỏ trùng theo userId."""
    normalized_members: list[dict] = []
    seen: set[str] = set()
    for raw in raw_members:
        member = _normalize_member(raw)
        if not member or member["userId"] in seen:
            continue
        seen.add(member["userId"])
        normalized_members.append(member)
    return normalized_members


def _normalize_job(job: dict, *, deep: bool = True) -> dict:
    """Chuẩn hóa một tác vụ.

    ``deep=False`` bỏ qua việc dựng lại dict 40-field cho từng thành viên.
    Dùng cho đường đọc (polling danh sách tác vụ) vốn không cần chi tiết
    thành viên -> tiết kiệm O(job x member) mỗi lần frontend poll.
    """
    job = dict(job or {})
    raw_members = job.get("members") or []
    if deep:
        normalized_members = _normalize_members(job, raw_members)
    else:
        # Copy mỏng: rẻ hơn nhiều so với dựng lại dict 40-field cho từng
        # thành viên, nhưng vẫn an toàn vì không sửa trực tiếp object trong cache.
        normalized_members = [dict(m) for m in raw_members if isinstance(m, dict)]
    legacy_waiting_found = any(
        isinstance(raw, dict) and str(raw.get("status") or "").strip().lower() == "waiting_friend"
        for raw in raw_members
    )
    legacy_invite_retry_found = False
    for member in normalized_members:
        note = str(member.get("error") or "").strip().lower().rstrip(".")
        code = _to_int(member.get("inviteResultCode"), -1)
        if member.get("status") == "failed" and (
            note in {"successful", "success"}
            or code in {262, 263, 240, 252}
        ):
            member["status"] = "pending"
            member["error"] = "Sẽ gửi lại bằng cùng logic Mời vào nhóm."
            legacy_invite_retry_found = True

    job["members"] = normalized_members
    job.setdefault("runs", [])
    job.setdefault("status", "pending")
    job.setdefault("createdAt", _now_ms())
    job.setdefault("updatedAt", job["createdAt"])
    job.setdefault("targetGroupId", "")
    job.setdefault("targetGroupName", "")
    job.setdefault("accountAvatar", "")
    job.setdefault("lastVerifiedAt", "")
    job.setdefault("lastVerificationError", "")
    job.setdefault("targetMemberCount", 0)
    job.setdefault("nextStatusPollAt", "")
    # Chu kỳ rà soát nhanh (phút): chỉ đọc danh sách thành viên nhóm đích để
    # bảng thống kê cập nhật ngay khi người nhận bấm link vào nhóm.
    try:
        job["statusPollMinutes"] = max(1, min(int(job.get("statusPollMinutes") or 2), 30))
    except (TypeError, ValueError):
        job["statusPollMinutes"] = 2

    daily_limit = job.get("friendRequestDailyLimit", job.get("dailyLimit", job.get("batchSize", 10)))
    try:
        daily_limit = max(1, min(int(daily_limit or 10), 100))
    except (TypeError, ValueError):
        daily_limit = 10
    job["dailyLimit"] = daily_limit
    job["batchSize"] = daily_limit
    job["friendRequestDailyLimit"] = daily_limit
    job.setdefault("dailyFriendRequestDate", "")
    job.setdefault("dailyFriendRequestCount", 0)
    # Hạn mức gửi thẻ link nhóm cho người lạ trong tin nhắn 1-1. Dùng chung
    # hạn mức với lời mời kết bạn để tránh chạm trần anti-spam của Zalo.
    job.setdefault("dailyLinkCardDate", "")
    job.setdefault("dailyLinkCardCount", 0)
    # Hạn mức tiếp cận theo NGƯỜI (một người có thể nhận cả thẻ link nhóm
    # lẫn lời mời kết bạn mà chỉ tính 1 lượt).
    job.setdefault("dailyReachDate", "")
    job.setdefault("dailyReachCount", 0)
    # Migration: job cũ chỉ đếm theo thẻ link nhóm. Quy đổi sang bộ đếm
    # tiếp cận theo NGƯỜI để không tính lại lượt đã dùng hôm nay.
    if not str(job.get("dailyReachDate") or "").strip() and str(job.get("dailyLinkCardDate") or "").strip():
        job["dailyReachDate"] = str(job.get("dailyLinkCardDate"))
        job["dailyReachCount"] = max(
            int(job.get("dailyReachCount") or 0),
            int(job.get("dailyLinkCardCount") or 0),
        )
    # Template tin nhắn mời gửi cho người lạ + tối đa 2 link phụ.
    job.setdefault("inviteMessageTemplate", "")
    job.setdefault("inviteExtraLinks", "")
    job.setdefault("groupLink", "")
    job.setdefault("groupLinkExpirationDate", 0)
    job.setdefault("groupLinkEnabled", 0)
    job.setdefault("groupLinkUpdatedAt", "")
    job["removeFriendAfterJoin"] = bool(job.get("removeFriendAfterJoin"))
    # Gửi lời mời kết bạn kèm link ngay cùng đợt gửi thẻ link nhóm.
    # Mặc định BẬT: người nhận có cả hai đường vào nhóm (bấm link hoặc
    # chấp nhận lời mời kết bạn) nên tỉ lệ vào nhóm cao hơn hẳn.
    job["friendRequestWithLink"] = job.get("friendRequestWithLink") is not False
    job["scheduleMode"] = "daily"
    job["dailyRunTime"] = str(job.get("dailyRunTime") or _extract_daily_time(job.get("startAt"))).strip()

    try:
        verify_minutes = max(1, min(int(job.get("verifyIntervalMinutes") or 30), 1440))
    except (TypeError, ValueError):
        verify_minutes = 30
    job["verifyIntervalMinutes"] = verify_minutes

    try:
        campaign_days = max(1, min(int(job.get("campaignDurationDays") or 30), 365))
    except (TypeError, ValueError):
        campaign_days = 30
    job["campaignDurationDays"] = campaign_days

    start_at = str(job.get("startAt") or _now_iso()).strip()
    job["startAt"] = start_at
    campaign_end_at = str(job.get("campaignEndAt") or "").strip()
    if not campaign_end_at:
        try:
            campaign_end_at = (datetime.fromisoformat(start_at) + timedelta(days=campaign_days)).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            campaign_end_at = (datetime.now() + timedelta(days=campaign_days)).isoformat(timespec="seconds")
    job["campaignEndAt"] = campaign_end_at
    job.setdefault("campaignExpiredAt", "")
    job.setdefault("nextInviteAt", job.get("nextRunAt") or start_at)

    # Tác vụ từ bản cũ từng giữ người chưa kết bạn ở waiting_friend. Sau khi
    # nâng cấp, đưa toàn bộ những người đó vào đợt mời ngay một lần.
    if legacy_waiting_found and not job.get("inviteAllMigrationApplied") and job.get("status") not in {"cancelled", "running"}:
        migrate_at = _now_iso()
        job["status"] = "pending"
        job["nextInviteAt"] = migrate_at
        job["nextRunAt"] = migrate_at
        if str(job.get("targetGroupId") or "").strip():
            job["nextVerifyAt"] = migrate_at
        job["inviteAllMigrationApplied"] = True
    if legacy_invite_retry_found and not job.get("sharedInviteMigrationApplied") and job.get("status") not in {"cancelled", "running"}:
        migrate_at = _now_iso()
        job["status"] = "pending"
        job["nextInviteAt"] = migrate_at
        job["nextRunAt"] = migrate_at
        if str(job.get("targetGroupId") or "").strip():
            job["nextVerifyAt"] = migrate_at
        job["sharedInviteMigrationApplied"] = True
    if str(job.get("targetGroupId") or "").strip():
        job.setdefault("nextVerifyAt", job.get("nextRunAt") or start_at)
    else:
        job.setdefault("nextVerifyAt", "")

    # Tác vụ tạo trước khi có cơ chế rà soát nhanh: bật ngay chu kỳ rà soát
    # 2 phút cho những người đang chờ vào nhóm, để bảng thống kê cập nhật
    # ngay khi họ bấm link.
    if (
        job.get("awaitingJoinCount")
        and str(job.get("targetGroupId") or "").strip()
        and not str(job.get("nextStatusPollAt") or "").strip()
        and job.get("status") not in {"cancelled", "running", "done", "expired"}
    ):
        job["nextStatusPollAt"] = _now_iso()
        job["nextRunAt"] = _now_iso()

    _refresh_progress(job, mutate=deep)

    # Đã đủ thành viên thì kết thúc tác vụ, không tiếp tục theo dõi hay kiểm tra.
    if job.get("totalMembers") and (
        int(job.get("joinedCount") or 0) + int(job.get("skippedCount") or 0)
        >= int(job.get("totalMembers") or 0)
    ):
        job["status"] = "done"
        job["completedAt"] = str(job.get("completedAt") or _now_iso())
        job["nextInviteAt"] = ""
        job["nextVerifyAt"] = ""
        job["nextRunAt"] = ""

    if job.get("status") in {"pending", "monitoring"}:
        try:
            if datetime.now() >= datetime.fromisoformat(campaign_end_at):
                job["status"] = "done" if not job.get("pendingCount") else "expired"
                job["campaignExpiredAt"] = job.get("campaignExpiredAt") or _now_iso()
                job["nextInviteAt"] = ""
                job["nextVerifyAt"] = ""
                job["nextRunAt"] = ""
        except (TypeError, ValueError):
            pass

    if job.get("status") in {"pending", "monitoring"}:
        candidates = []
        # Người đã nhận thẻ link nhóm nhưng chưa nhận lời mời kết bạn vẫn
        # cần một lượt gửi nữa -> phải tính nextInviteAt vào lịch chạy, nếu
        # không worker sẽ không bao giờ dậy để gửi kèm lời mời kết bạn.
        needs_friend_request = bool(job.get("pendingFriendRequestCount"))
        if not needs_friend_request:
            needs_friend_request = any(
                str(m.get("status") or "") != "joined"
                and m.get("isFriend") is not True
                and str(m.get("messageDelivery") or "") == "sent"
                and str(m.get("friendRequestDelivery") or "")
                != "friend_request_with_group_link"
                for m in (job.get("members") or [])
                if isinstance(m, dict)
            )
        if job.get("pendingInviteCount") or needs_friend_request:
            candidates.append(str(job.get("nextInviteAt") or start_at))
        if str(job.get("targetGroupId") or "").strip() and (
            job.get("awaitingJoinCount") or job.get("pendingInviteCount") or job.get("status") == "monitoring"
        ):
            candidates.append(str(job.get("nextVerifyAt") or start_at))
            # Rà soát nhanh: giữ bảng thống kê cập nhật gần như tức thì khi
            # người nhận bấm link vào nhóm.
            if job.get("awaitingJoinCount") and str(job.get("nextStatusPollAt") or "").strip():
                candidates.append(str(job.get("nextStatusPollAt") or ""))
        job["nextRunAt"] = _earliest_iso(candidates) or str(job.get("nextRunAt") or start_at)
    elif job.get("status") in _TERMINAL_STATUSES:
        job.setdefault("nextRunAt", "")

    return job


def create_job(
    payload: dict,
    members: list[dict],
    source_group: dict,
    account_name: str,
    account_avatar: str = "",
) -> dict:
    clean_members = []
    seen = set()
    for raw in members or []:
        member = _normalize_member(raw)
        if not member or member["userId"] in seen:
            continue
        seen.add(member["userId"])
        # Mọi thành viên đã là bạn bè được ưu tiên thêm trực tiếp trước. Chỉ
        # người chưa thêm được mới nhận lời mời kết bạn theo hạn mức mỗi ngày.
        member["status"] = "pending"
        member["error"] = ""
        member["attemptedAt"] = 0
        member["invitedAt"] = 0
        member["joinedAt"] = 0
        member["lastVerifiedAt"] = 0
        member["inviteAttempts"] = 0
        member["inviteResultCode"] = -1
        member["inviteResultMessage"] = ""
        member["inviteDelivery"] = ""
        member["friendRequestAttempts"] = 0
        member["friendRequestAt"] = 0
        member["friendRequestCode"] = -1
        member["friendRequestMessage"] = ""
        member["friendRequestDelivery"] = ""
        member["friendRequestCreatedByCampaign"] = False
        member["friendRemoveAttempts"] = 0
        member["friendRemovedAt"] = 0
        member["friendRemoveCode"] = -1
        member["friendRemoveMessage"] = ""
        member["friendRemoveDelivery"] = ""
        member["campaignAddRetryAttempts"] = 0
        member["campaignAddRetryAt"] = 0
        member["joinedAfterCampaignAddRetry"] = False
        clean_members.append(member)

    if not clean_members:
        raise ValueError("Nhóm nguồn không có thành viên hợp lệ để lập lịch.")

    start_at = str(payload.get("startAt") or datetime.now().isoformat(timespec="minutes")).strip()
    try:
        daily_limit = max(1, min(int(payload.get("friendRequestDailyLimit") or payload.get("dailyLimit") or payload.get("batchSize") or 10), 100))
    except (TypeError, ValueError):
        daily_limit = 10
    try:
        verify_minutes = max(1, min(int(payload.get("verifyIntervalMinutes") or 30), 1440))
    except (TypeError, ValueError):
        verify_minutes = 30
    try:
        campaign_days = max(1, min(int(payload.get("campaignDurationDays") or 30), 365))
    except (TypeError, ValueError):
        campaign_days = 30

    target_mode = str(payload.get("targetMode") or "existing").strip()
    target_group_id = str(payload.get("targetGroupId") or "").strip()
    target_group_link = str(payload.get("targetGroupLink") or "").strip()
    if target_group_link.startswith("//"):
        target_group_link = "https:" + target_group_link
    elif target_group_link and not target_group_link.lower().startswith(("http://", "https://")):
        token = target_group_link.strip().strip("/")
        target_group_link = ("https://" + token) if token.lower().startswith("zalo.me/g/") else ("https://zalo.me/g/" + token)
    try:
        campaign_end_at = (datetime.fromisoformat(start_at) + timedelta(days=campaign_days)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        campaign_end_at = (datetime.now() + timedelta(days=campaign_days)).isoformat(timespec="seconds")
    now = _now_ms()
    job = {
        "jobId": "gcopy_" + uuid.uuid4().hex[:12],
        "title": str(payload.get("title") or "Sao chép thành viên nhóm").strip(),
        "accountId": str(payload.get("accountId") or "").strip(),
        "accountName": str(account_name or payload.get("accountName") or "").strip(),
        "accountAvatar": str(account_avatar or payload.get("accountAvatar") or "").strip(),
        "sourceInput": str(payload.get("sourceInput") or "").strip(),
        "sourceGroup": source_group or {},
        "targetMode": target_mode,
        "targetGroupId": target_group_id,
        "targetGroupName": str(payload.get("targetGroupName") or payload.get("newGroupName") or "").strip(),
        "newGroupName": str(payload.get("newGroupName") or "").strip(),
        "dailyLimit": daily_limit,
        "batchSize": daily_limit,
        "friendRequestDailyLimit": daily_limit,
        "dailyFriendRequestDate": "",
        "dailyFriendRequestCount": 0,
        "dailyLinkCardDate": "",
        "dailyLinkCardCount": 0,
        "inviteMessageTemplate": str(payload.get("inviteMessageTemplate") or "").strip(),
        "inviteExtraLinks": str(payload.get("inviteExtraLinks") or "").strip(),
        "scheduleMode": "daily",
        "dailyRunTime": _extract_daily_time(start_at),
        "verifyIntervalMinutes": verify_minutes,
        "campaignDurationDays": campaign_days,
        "campaignEndAt": campaign_end_at,
        "campaignExpiredAt": "",
        "removeFriendAfterJoin": bool(payload.get("removeFriendAfterJoin")),
        "friendRequestWithLink": payload.get("friendRequestWithLink") is not False,
        "statusPollMinutes": max(1, min(int(payload.get("statusPollMinutes") or 2), 30)),
        "startAt": start_at,
        "nextInviteAt": start_at,
        "nextVerifyAt": start_at if target_mode == "existing" and target_group_id else "",
        "nextRunAt": start_at,
        "status": "pending",
        "consentConfirmed": bool(payload.get("consentConfirmed")),
        "createdAt": now,
        "updatedAt": now,
        "lastRunAt": "",
        "lastError": "",
        "lastNotice": "",
        "lastVerifiedAt": "",
        "lastVerificationError": "",
        "targetMemberCount": 0,
        "groupLink": target_group_link if target_mode == "existing" else "",
        "groupLinkExpirationDate": 0,
        "groupLinkEnabled": 1 if (target_mode == "existing" and target_group_link) else 0,
        "groupLinkUpdatedAt": datetime.now().isoformat(timespec="seconds") if (target_mode == "existing" and target_group_link) else "",
        "groupLinkSource": "personal_groups" if (target_mode == "existing" and target_group_link) else "",
        "members": clean_members,
        "runs": [],
    }
    _refresh_progress(job)

    with _LOCK:
        jobs = _load_jobs_unlocked()
        jobs.append(job)
        _flush_jobs_unlocked(jobs)
    return job


def list_jobs(include_members: bool = False) -> list[dict]:
    # Đường đọc nóng: frontend poll endpoint này mỗi 10 giây.
    # - Không cần members -> chỉ đọc file TÓM TẮT (vài KB), không đụng tới
    #   các file tác vụ riêng (vài MB). Đây là đường đọc siêu nhanh.
    # - Cần members -> nạp đầy đủ rồi normalize sâu.
    with _LOCK:
        if include_members:
            jobs = [
                _normalize_job(item, deep=True)
                for item in _load_jobs_unlocked()
                if isinstance(item, dict)
            ]
        else:
            jobs = [
                _normalize_job(item, deep=False)
                for item in _load_summaries_unlocked()
                if isinstance(item, dict)
            ]
    jobs.sort(key=lambda item: int(item.get("createdAt") or 0), reverse=True)
    if include_members:
        return jobs
    summaries = []
    for job in jobs:
        summary = dict(job)
        summary.pop("members", None)
        summaries.append(summary)
    return summaries


def get_job(job_id: str) -> Optional[dict]:
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    with _LOCK:
        # Thử cache trước (đã nạp đầy đủ), sau đó đọc file riêng của tác vụ.
        for item in _load_jobs_unlocked():
            if str(item.get("jobId") or "") == job_id:
                return _normalize_job(item)
        detail = _load_job_detail_unlocked(job_id)
        if detail is not None:
            return _normalize_job(detail)
    return None


def save_job(job: dict) -> dict:
    job_id = str(job.get("jobId") or "").strip()
    if not job_id:
        raise ValueError("Tác vụ không có jobId.")
    # `job` từ worker đã là dạng đã chuẩn hóa (đọc ra từ get_job/
    # claim_due_job) nên không cần normalize lại lần nữa -> tiết kiệm
    # O(member) mỗi lần lưu.
    job = _normalize_job(job, deep=False)
    job["updatedAt"] = _now_ms()

    with _LOCK:
        jobs = _load_jobs_unlocked()
        for index, item in enumerate(jobs):
            if str(item.get("jobId") or "") == job_id:
                jobs[index] = job
                # Chỉ ghi lại file của tác vụ này -> tiết kiệm I/O.
                _flush_jobs_unlocked(jobs, {job_id})
                return job
    raise ValueError("Không tìm thấy tác vụ sao chép nhóm.")


def mutate_job(job_id: str, mutator: Callable[[dict], None]) -> dict:
    with _LOCK:
        jobs = _load_jobs_unlocked()
        for index, item in enumerate(jobs):
            if str(item.get("jobId") or "") != str(job_id):
                continue
            job = _normalize_job(item)
            mutator(job)
            job["updatedAt"] = _now_ms()
            _refresh_progress(job)
            jobs[index] = job
            _flush_jobs_unlocked(jobs, {str(job_id)})
            return job
    raise ValueError("Không tìm thấy tác vụ sao chép nhóm.")


def claim_due_job(now_iso: str) -> Optional[dict]:
    """Đánh dấu một tác vụ đến hạn là running để worker xử lý độc quyền."""
    with _LOCK:
        jobs = _load_jobs_unlocked()
        due_items = []
        now_ms = _now_ms()
        store_changed = False
        for index, raw in enumerate(jobs):
            job = _normalize_job(raw)
            if job.get("status") == "running" and now_ms - int(job.get("updatedAt") or 0) > 15 * 60 * 1000:
                job["status"] = "pending"
                job["nextRunAt"] = now_iso
                if job.get("pendingInviteCount"):
                    job["nextInviteAt"] = now_iso
                elif job.get("awaitingJoinCount"):
                    job["nextVerifyAt"] = now_iso
                job["lastError"] = "Tác vụ được khôi phục sau khi ứng dụng dừng giữa đợt."
                job["updatedAt"] = now_ms
                jobs[index] = job
                store_changed = True
            if job.get("status") not in {"pending", "monitoring"}:
                continue
            has_work = bool(job.get("pendingCount")) or bool(
                str(job.get("targetGroupId") or "").strip()
                and (
                    str(job.get("nextVerifyAt") or "").strip()
                    or str(job.get("nextStatusPollAt") or "").strip()
                )
            )
            if not has_work:
                continue
            next_run = str(job.get("nextRunAt") or "")
            if next_run and next_run <= now_iso:
                due_items.append((next_run, index, job))
        if not due_items:
            if store_changed:
                _flush_jobs_unlocked(jobs)
            return None
        due_items.sort(key=lambda item: item[0])
        _, index, job = due_items[0]
        job["status"] = "running"
        job["lastRunAt"] = now_iso
        job["updatedAt"] = _now_ms()
        jobs[index] = job
        _flush_jobs_unlocked(jobs)
        return job


def recover_running_jobs(now_iso: str = "") -> int:
    """Đưa các tác vụ đang chạy dở về hàng đợi khi ứng dụng khởi động lại."""
    now_iso = now_iso or _now_iso()
    recovered = 0
    with _LOCK:
        jobs = _load_jobs_unlocked()
        for index, raw in enumerate(jobs):
            job = _normalize_job(raw)
            if job.get("status") != "running":
                continue
            if not job.get("pendingCount"):
                job["status"] = "monitoring" if str(job.get("targetGroupId") or "").strip() else "done"
                job["nextRunAt"] = job.get("nextVerifyAt") or ""
            else:
                job["status"] = "pending"
                job["nextRunAt"] = now_iso
                if job.get("pendingInviteCount"):
                    job["nextInviteAt"] = now_iso
                if job.get("awaitingJoinCount") and str(job.get("targetGroupId") or "").strip():
                    job["nextVerifyAt"] = now_iso
            job["lastError"] = "Tác vụ được tiếp tục sau khi ứng dụng khởi động lại."
            job["updatedAt"] = _now_ms()
            jobs[index] = job
            recovered += 1
        if recovered:
            _flush_jobs_unlocked(jobs)
    return recovered


def cancel_job(job_id: str) -> dict:
    def apply(job: dict) -> None:
        if job.get("status") == "running":
            raise ValueError("Đợt hiện tại đang chạy. Vui lòng chờ đợt này kết thúc rồi hủy.")
        if job.get("status") in _TERMINAL_STATUSES and job.get("status") != "failed":
            raise ValueError("Tác vụ đã kết thúc, không thể hủy.")
        job["status"] = "cancelled"
        job["nextInviteAt"] = ""
        job["nextVerifyAt"] = ""
        job["nextRunAt"] = ""
        job["lastError"] = ""

    return mutate_job(job_id, apply)


def resume_job(job_id: str, next_run_at: str = "") -> dict:
    def apply(job: dict) -> None:
        campaign_end = None
        try:
            campaign_end = datetime.fromisoformat(str(job.get("campaignEndAt") or ""))
        except (TypeError, ValueError):
            pass
        if campaign_end and datetime.now() >= campaign_end:
            raise ValueError("Chiến dịch đã hết thời gian chạy, không thể tiếp tục.")
        if not job.get("pendingCount"):
            raise ValueError("Tác vụ không còn thành viên đang chờ.")
        if job.get("status") == "running":
            raise ValueError("Tác vụ đang chạy.")
        run_at = next_run_at or datetime.now().isoformat(timespec="seconds")
        job["status"] = "pending"
        if job.get("pendingInviteCount"):
            job["nextInviteAt"] = run_at
        if str(job.get("targetGroupId") or "").strip():
            job["nextVerifyAt"] = run_at
        job["nextRunAt"] = run_at
        job["lastError"] = ""

    return mutate_job(job_id, apply)


def request_verification(job_id: str) -> dict:
    """Đưa tác vụ về hàng đợi để kiểm tra ngay danh sách thành viên nhóm đích."""
    def apply(job: dict) -> None:
        campaign_end = None
        try:
            campaign_end = datetime.fromisoformat(str(job.get("campaignEndAt") or ""))
        except (TypeError, ValueError):
            pass
        if campaign_end and datetime.now() >= campaign_end:
            raise ValueError("Chiến dịch đã hết thời gian chạy, không thể kiểm tra thêm.")
        if not str(job.get("targetGroupId") or "").strip():
            raise ValueError("Tác vụ chưa có nhóm đích để kiểm tra.")
        if job.get("status") == "running":
            raise ValueError("Tác vụ đang chạy. Vui lòng chờ đợt hiện tại kết thúc.")
        now_iso = datetime.now().isoformat(timespec="seconds")
        job["status"] = "pending" if job.get("pendingCount") else "monitoring"
        # Nút "Kiểm tra ngay" -> rà soát nhanh (chỉ đọc danh sách thành viên
        # nhóm đích) để bảng thống kê cập nhật tức thì mà không gọi API mời.
        job["nextStatusPollAt"] = now_iso
        job["nextVerifyAt"] = now_iso
        job["nextRunAt"] = now_iso
        job["lastVerificationError"] = ""

    return mutate_job(job_id, apply)


def delete_job(job_id: str) -> None:
    with _LOCK:
        jobs = _load_jobs_unlocked()
        for index, item in enumerate(jobs):
            if str(item.get("jobId") or "") != str(job_id):
                continue
            if str(item.get("status") or "") == "running":
                raise ValueError("Không thể xóa tác vụ đang chạy.")
            jobs.pop(index)
            _flush_jobs_unlocked(jobs)
            # Xóa luôn file riêng của tác vụ để không rò rỉ dữ liệu thành viên.
            try:
                os.remove(_job_file_path(job_id))
            except OSError:
                pass
            return
    raise ValueError("Không tìm thấy tác vụ sao chép nhóm.")
