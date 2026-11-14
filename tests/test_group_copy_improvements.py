"""Kiểm thử nhanh các phần vừa cải tiến (không cần mạng, không cần Zalo).

Chạy:  python -m tests.test_group_copy_improvements
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def test_error_classification() -> None:
    print("\n== Phân loại mã lỗi Zalo ==")
    from features.groups.invite_group import (
        INVITE_CODE_ADD_TO_BIG_GROUP,
        INVITE_CODE_BLOCKED_STRANGER,
        INVITE_CODE_NOT_PERMISSION,
        INVITE_CODE_OVER_MEM_LIMIT,
        INVITE_CODE_RATE_LIMIT,
        INVITE_CODE_STRANGER_QUOTA,
        INVITE_TERMINAL_CODES,
        _result_message,
        classify_invite_response,
    )

    delivery, message = _result_message(INVITE_CODE_STRANGER_QUOTA)
    check("314 -> stranger_blocked", delivery == "stranger_blocked", f"got {delivery}")
    check("314 message mentions link/friend", "link nhóm" in message or "kết bạn" in message)

    delivery, _ = _result_message(INVITE_CODE_BLOCKED_STRANGER)
    check("122 -> blocked", delivery == "blocked", f"got {delivery}")

    delivery, _ = _result_message(INVITE_CODE_NOT_PERMISSION)
    check("166 -> failed", delivery == "failed", f"got {delivery}")

    delivery, _ = _result_message(INVITE_CODE_OVER_MEM_LIMIT)
    check("177 -> failed", delivery == "failed", f"got {delivery}")

    delivery, _ = _result_message(INVITE_CODE_ADD_TO_BIG_GROUP)
    check("264 -> failed", delivery == "failed", f"got {delivery}")

    delivery, _ = _result_message(INVITE_CODE_RATE_LIMIT)
    check("221 -> failed (retryable)", delivery == "failed", f"got {delivery}")

    check(
        "terminal codes chứa 314/122/166",
        INVITE_CODE_STRANGER_QUOTA in INVITE_TERMINAL_CODES
        and INVITE_CODE_BLOCKED_STRANGER in INVITE_TERMINAL_CODES
        and INVITE_CODE_NOT_PERMISSION in INVITE_TERMINAL_CODES,
    )

    # Phân loại response thực tế dạng {"263": [uid]}
    result = classify_invite_response(
        ["uid1", "uid2"],
        {"error_code": 0, "data": {"error_data": {"263": ["uid1"]}}},
        {"error_code": 0},
    )
    by_uid = {m["userId"]: m for m in result["members"]}
    check("uid1 -> pending_inbox", by_uid["uid1"]["delivery"] == "pending_inbox",
          f"got {by_uid['uid1']['delivery']}")
    check("uid2 -> invited", by_uid["uid2"]["status"] == "invited",
          f"got {by_uid['uid2']['status']}")


def test_manager_cache() -> None:
    print("\n== Cache group_copy_manager ==")
    from features.groups import group_copy_manager as mgr

    tmpdir = tempfile.mkdtemp()
    jobs_file = os.path.join(tmpdir, "jobs.json")
    jobs_dir = os.path.join(tmpdir, "jobs_dir")
    mgr.JOBS_FILE = jobs_file
    mgr.JOBS_DIR = jobs_dir
    mgr._invalidate_cache()

    job = mgr.create_job(
        {
            "accountId": "acc1",
            "title": "Test",
            "startAt": "2026-01-01T00:00:00",
            "dailyLimit": 5,
        },
        [{"userId": f"u{i}", "zaloName": f"Người {i}", "isFriend": i % 2 == 0} for i in range(50)],
        {"groupId": "g1", "name": "Nhóm nguồn"},
        "Account 1",
    )
    check("create_job trả về jobId", bool(job.get("jobId")))
    check("totalMembers = 50", job.get("totalMembers") == 50, f"got {job.get('totalMembers')}")

    # File tóm tắt phải KHÔNG chứa members
    with open(jobs_file, "r", encoding="utf-8") as f:
        store = json.load(f)
    check(
        "file tóm tắt không chứa members",
        all("members" not in j for j in store["jobs"]),
        "vẫn còn members trong file tóm tắt",
    )
    # File riêng phải chứa members
    detail_files = [f for f in os.listdir(jobs_dir) if f.endswith(".json")]
    check("có file riêng cho tác vụ", len(detail_files) == 1, f"got {detail_files}")
    with open(os.path.join(jobs_dir, detail_files[0]), "r", encoding="utf-8") as f:
        detail = json.load(f)
    check(
        "file riêng chứa 50 member",
        len(detail.get("members") or []) == 50,
        f"got {len(detail.get('members') or [])}",
    )

    # Cache phải được dùng (không đọc lại file mỗi lần)
    mgr._invalidate_cache()
    mgr.list_jobs()
    check("cache tóm tắt được nạp", mgr._SUMMARY_CACHE is not None)
    mgr.list_jobs(include_members=True)
    check("cache đầy đủ được nạp", mgr._CACHE is not None and len(mgr._CACHE) == 1,
          f"got {mgr._CACHE}")

    # Đo thời gian polling: light path phải nhanh hơn nhiều so với deep path.
    t0 = time.perf_counter()
    for _ in range(50):
        mgr.list_jobs(include_members=False)
    light_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(50):
        mgr.list_jobs(include_members=True)
    deep_ms = (time.perf_counter() - t0) * 1000

    print(f"       light={light_ms:.1f}ms  deep={deep_ms:.1f}ms")
    check("light path nhanh hơn deep path", light_ms <= deep_ms, f"{light_ms} vs {deep_ms}")

    # Cache phải được invalidate khi file đổi từ bên ngoài.
    with open(jobs_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["jobs"][0]["title"] = "Đổi từ ngoài"
    with open(jobs_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # Chạm mtime để chắc chắn khác
    os.utime(jobs_file, (time.time() + 2, time.time() + 2))
    jobs = mgr.list_jobs()
    check("nhận thay đổi từ bên ngoài", jobs[0]["title"] == "Đổi từ ngoài",
          f"got {jobs[0]['title']}")

    # Xóa tác vụ phải xóa cả file riêng
    job_id = job["jobId"]
    mgr.delete_job(job_id)
    remaining = [f for f in os.listdir(jobs_dir) if f.endswith(".json")]
    check("xóa tác vụ -> xóa file riêng", len(remaining) == 0, f"còn {remaining}")


def test_pending_members_parsing() -> None:
    print("\n== Đọc/ghi pending members ==")
    from features.groups.pending_members import (
        _extract_pending_users,
        list_pending_members,
        review_pending_members,
    )

    # Shape thật từ zca-js: {"time": 123, "users": [{uid, dpn, avatar}]}
    decoded = {
        "time": 1700000000000,
        "users": [
            {"uid": "uid1", "dpn": "An Nam", "avatar": "a", "user_submit": None},
            {"uid": "uid2", "dpn": "Chị Hương", "avatar": "b", "user_submit": None},
        ],
    }
    users = _extract_pending_users(decoded)
    check("parse 2 user", len(users) == 2, f"got {len(users)}")
    check("uid1 đúng", users[0]["userId"] == "uid1", f"got {users[0]}")
    check("tên đúng", users[1]["displayName"] == "Chị Hương", f"got {users[1]}")

    # Shape bọc thêm data
    wrapped = {"error_code": 0, "data": decoded}
    users = _extract_pending_users(wrapped)
    check("parse shape bọc data", len(users) == 2, f"got {len(users)}")

    # Thiếu grid -> phải trả lỗi, không ném exception
    result = list_pending_members("", "imei", "enk", "cookies")
    check("thiếu grid -> ok=False", result["ok"] is False)

    result = review_pending_members("gid", [], "imei", "enk", "cookies")
    check("thiếu members -> ok=False", result["ok"] is False)


def test_target_members_cache() -> None:
    print("\n== Cache danh sách thành viên nhóm đích ==")
    from features.groups import group_copy_worker as worker

    calls = {"n": 0}

    def fake_get_members(**kwargs):
        calls["n"] += 1
        return {"ok": True, "uids": ["a", "b"], "total": 2, "message": "ok"}

    worker.get_members_by_group_id = fake_get_members
    worker._invalidate_target_members_cache("gtest")

    job = {"targetGroupId": "gtest", "members": [{"userId": "a", "status": "pending"}]}
    r1 = worker._verify_target_members(job, "enk", "ck", "imei", force=True)
    r2 = worker._verify_target_members(job, "enk", "ck", "imei", force=True)
    check("2 lần verify chỉ gọi API 1 lần", calls["n"] == 1, f"got {calls['n']}")
    check("verify báo đã kiểm tra", r1["checked"] is True and r2["checked"] is True)
    check("member a -> joined", job["members"][0]["status"] == "joined",
          f"got {job['members'][0]['status']}")

    worker._invalidate_target_members_cache("gtest")
    worker._verify_target_members(job, "enk", "ck", "imei", force=True)
    check("invalidate -> gọi API lại", calls["n"] == 2, f"got {calls['n']}")


def test_quota_refund() -> None:
    print("\n== Hoàn lại hạn mức khi gửi không tới nơi ==")
    from features.groups.group_copy_worker import (
        _consume_friend_request_slot,
        _consume_link_card_slot,
        _refund_friend_request_slot,
        _refund_link_card_slot,
    )

    job = {}
    now = None
    _consume_link_card_slot(job, now)
    _consume_link_card_slot(job, now)
    check("2 lượt link card", job["dailyLinkCardCount"] == 2, f"got {job.get('dailyLinkCardCount')}")
    _refund_link_card_slot(job, now)
    check("hoàn 1 lượt link card", job["dailyLinkCardCount"] == 1, f"got {job.get('dailyLinkCardCount')}")
    _refund_link_card_slot(job, now)
    _refund_link_card_slot(job, now)
    check("không âm", job["dailyLinkCardCount"] == 0, f"got {job.get('dailyLinkCardCount')}")

    _consume_friend_request_slot(job, now)
    check("1 lượt kết bạn", job["dailyFriendRequestCount"] == 1, f"got {job.get('dailyFriendRequestCount')}")
    _refund_friend_request_slot(job, now)
    check("hoàn lượt kết bạn", job["dailyFriendRequestCount"] == 0, f"got {job.get('dailyFriendRequestCount')}")


def test_http_session_pooling() -> None:
    print("\n== HTTP session dùng chung ==")
    from core.zalo import http as zalo_http

    s1 = zalo_http.get_session()
    s2 = zalo_http.get_session()
    check("cùng một session instance", s1 is s2)
    adapter = s1.get_adapter("https://chat.zalo.me")
    check("có connection pool", adapter._pool_connections >= 8,
          f"got {adapter._pool_connections}")


def test_progress_not_zeroed_for_summary() -> None:
    print("\n== Số liệu thống kê không bị ghi đè về 0 ==")
    from features.groups import group_copy_manager as mgr

    tmpdir = tempfile.mkdtemp()
    mgr.JOBS_FILE = os.path.join(tmpdir, "jobs.json")
    mgr.JOBS_DIR = os.path.join(tmpdir, "jobs_dir")
    mgr._invalidate_cache()

    job = mgr.create_job(
        {
            "accountId": "acc1",
            "title": "Test thong ke",
            "startAt": "2026-01-01T00:00:00",
            "dailyLimit": 5,
        },
        [{"userId": f"u{i}", "zaloName": f"Người {i}"} for i in range(4)],
        {"groupId": "g1", "name": "Nhóm nguồn"},
        "Account 1",
    )
    # Giả lập 2 người đã vào nhóm rồi lưu lại như worker thực tế làm.
    full = mgr.get_job(job["jobId"])
    full["members"][0]["status"] = "joined"
    full["members"][1]["status"] = "joined"
    mgr.save_job(full)
    mgr._invalidate_cache()

    # Đường đọc nóng của frontend (không nạp members).
    summary = mgr.list_jobs(include_members=False)[0]
    check("totalMembers giữ nguyên qua đường tóm tắt",
          summary.get("totalMembers") == 4, f"got {summary.get('totalMembers')}")
    check("joinedCount giữ nguyên qua đường tóm tắt",
          summary.get("joinedCount") == 2, f"got {summary.get('joinedCount')}")
    check("conversionRate giữ nguyên qua đường tóm tắt",
          abs(float(summary.get("conversionRate") or 0) - 50.0) < 0.01,
          f"got {summary.get('conversionRate')}")

    # Đường đọc đầy đủ phải khớp.
    full = mgr.get_job(job["jobId"])
    check("đường đầy đủ khớp với tóm tắt",
          full.get("joinedCount") == summary.get("joinedCount")
          and full.get("totalMembers") == summary.get("totalMembers"),
          f"full={full.get('joinedCount')}/{full.get('totalMembers')}")

    mgr.delete_job(job["jobId"])


def test_status_poll_schedule() -> None:
    print("\n== Lịch rà soát nhanh cập nhật bảng thống kê ==")
    from features.groups.group_copy_worker import (
        _next_status_poll,
        _status_poll_interval,
        _sync_job_schedule,
    )
    from datetime import datetime, timedelta

    job = {
        "targetGroupId": "g1",
        "campaignEndAt": (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds"),
        "members": [
            {"userId": "a", "status": "invited", "messageDelivery": "sent"},
            {"userId": "b", "status": "invited", "messageDelivery": "sent"},
        ],
    }
    check("chu kỳ rà soát mặc định <= 5 phút",
          _status_poll_interval(job) <= 5, f"got {_status_poll_interval(job)}")

    _sync_job_schedule(job, datetime.now())
    check("có lịch rà soát nhanh", bool(job.get("nextStatusPollAt")), "thiếu nextStatusPollAt")
    check("nextRunAt <= lịch rà soát",
          str(job.get("nextRunAt")) <= str(job.get("nextStatusPollAt")),
          f"runAt={job.get('nextRunAt')} pollAt={job.get('nextStatusPollAt')}")

    # Khi không còn ai chờ -> tắt lịch rà soát nhanh.
    for member in job["members"]:
        member["status"] = "joined"
    _sync_job_schedule(job, datetime.now())
    check("hết người chờ -> tắt lịch rà soát",
          not job.get("nextStatusPollAt"), f"got {job.get('nextStatusPollAt')}")

    # Chu kỳ rà soát không được vượt quá 30 phút.
    check("chu kỳ rà soát tối đa 30 phút",
          _status_poll_interval({"statusPollMinutes": 999}) == 30,
          f"got {_status_poll_interval({'statusPollMinutes': 999})}")


def test_reach_quota_per_person() -> None:
    print("\n== Hạn mức tính theo NGƯỜI ==")
    from features.groups.group_copy_worker import (
        _consume_reach_slot,
        _refund_reach_slot,
        _remaining_reach_today,
    )

    job = {"friendRequestDailyLimit": 3}
    check("còn 3 lượt tiếp cận", _remaining_reach_today(job) == 3,
          f"got {_remaining_reach_today(job)}")
    # Gửi cả thẻ link + lời mời kết bạn cho 1 người -> chỉ tính 1 lượt.
    _consume_reach_slot(job)
    check("1 người = 1 lượt", _remaining_reach_today(job) == 2,
          f"got {_remaining_reach_today(job)}")
    _consume_reach_slot(job)
    _consume_reach_slot(job)
    check("hết hạn mức", _remaining_reach_today(job) == 0,
          f"got {_remaining_reach_today(job)}")
    _refund_reach_slot(job)
    check("hoàn lại được lượt", _remaining_reach_today(job) == 1,
          f"got {_remaining_reach_today(job)}")
    _refund_reach_slot(job)
    _refund_reach_slot(job)
    check("hoàn tới 0 -> không âm",
          job.get("dailyReachCount") == 0, f"got {job.get('dailyReachCount')}")
    check("hết hạn mức trở lại đầy", _remaining_reach_today(job) == 3,
          f"got {_remaining_reach_today(job)}")


def test_friend_request_with_link_flag() -> None:
    print("\n== Tuỳ chọn gửi lời mời kết bạn kèm link ==")
    from features.groups.group_copy_manager import _normalize_job

    job = _normalize_job({"jobId": "x", "members": []})
    check("mặc định BẬT", job.get("friendRequestWithLink") is True,
          f"got {job.get('friendRequestWithLink')}")

    job = _normalize_job({"jobId": "x", "members": [], "friendRequestWithLink": False})
    check("tắt được khi người dùng bỏ chọn",
          job.get("friendRequestWithLink") is False,
          f"got {job.get('friendRequestWithLink')}")


def test_pending_friend_request_flag_persists_in_summary() -> None:
    print("\n== Cờ người còn chờ lời mời kết bạn tồn tại trong bản tóm tắt ==")
    from features.groups.group_copy_manager import _normalize_job, _refresh_progress

    job = {
        "jobId": "x",
        "targetGroupId": "g1",
        "members": [
            {"userId": "a", "status": "invited", "messageDelivery": "sent",
             "friendRequestDelivery": "", "isFriend": False},
            {"userId": "b", "status": "invited", "messageDelivery": "sent",
             "friendRequestDelivery": "friend_request_with_group_link", "isFriend": False},
            {"userId": "c", "status": "joined", "messageDelivery": "sent",
             "friendRequestDelivery": "", "isFriend": False},
        ],
    }
    _refresh_progress(job)
    check("đếm đúng người còn chờ lời mời kết bạn",
          job.get("pendingFriendRequestCount") == 1,
          f"got {job.get('pendingFriendRequestCount')}")

    # Đường đọc nóng: không có members nhưng vẫn phải biết cần gửi kèm
    # lời mời kết bạn -> nextRunAt phải tính cả nextInviteAt.
    summary = {k: v for k, v in job.items() if k != "members"}
    norm = _normalize_job(summary, deep=False)
    check("giữ cờ khi đọc tóm tắt",
          norm.get("pendingFriendRequestCount") == 1,
          f"got {norm.get('pendingFriendRequestCount')}")
    check("lịch chạy vẫn còn nextInviteAt",
          str(norm.get("nextRunAt")) <= str(norm.get("nextInviteAt") or "9999"),
          f"run={norm.get('nextRunAt')} invite={norm.get('nextInviteAt')}")


def test_escalation_gets_friend_request() -> None:
    print("\n== Người đã nhận link vẫn được gửi lời mời kết bạn ==")
    from features.groups.group_copy_worker import (
        _escalation_members,
        _outreach_members,
        _pending_members,
        _sync_job_schedule,
    )
    from datetime import datetime, timedelta

    job = {
        "targetGroupId": "g1",
        "friendRequestDailyLimit": 10,
        "dailyReachDate": datetime.now().date().isoformat(),
        "dailyReachCount": 2,
        "campaignEndAt": (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds"),
        "members": [
            # Đã nhận thẻ link nhóm, chưa nhận lời mời kết bạn.
            {"userId": "a", "status": "invited", "messageDelivery": "sent",
             "friendRequestDelivery": "", "isFriend": False},
            # Đã nhận cả hai kênh -> không cần gửi lại.
            {"userId": "b", "status": "invited", "messageDelivery": "sent",
             "friendRequestDelivery": "friend_request_with_group_link", "isFriend": False},
            # Đã vào nhóm -> không tính.
            {"userId": "c", "status": "joined", "messageDelivery": "sent",
             "friendRequestDelivery": "friend_request_with_group_link", "isFriend": False},
        ],
    }
    escalation = _escalation_members(job)
    check("chỉ người chưa nhận lời mời kết bạn",
          [m["userId"] for m in escalation] == ["a"],
          f"got {[m['userId'] for m in escalation]}")
    check("người đã vào nhóm không còn outreach",
          "c" not in [m["userId"] for m in _outreach_members(job)])
    check("người chờ kết bạn vẫn nằm trong outreach",
          "a" in [m["userId"] for m in _outreach_members(job)])

    # Lịch: còn người chờ -> bật rà soát nhanh.
    _sync_job_schedule(job, datetime.now())
    check("bật rà soát nhanh khi còn người chờ",
          bool(job.get("nextStatusPollAt")), "thiếu nextStatusPollAt")

    # Còn hạn mức -> được gửi bù lời mời kết bạn ngay, không chờ ngày mai.
    job["nextInviteAt"] = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    _sync_job_schedule(job, datetime.now())
    check("gửi bù lời mời kết bạn ngay khi còn hạn mức",
          str(job.get("nextInviteAt")) <= datetime.now().isoformat(timespec="seconds"),
          f"got {job.get('nextInviteAt')}")

    # Hết hạn mức -> phải chờ ngày mai.
    job["dailyReachCount"] = 10
    _sync_job_schedule(job, datetime.now())
    check("hết hạn mức -> chờ ngày mai",
          str(job.get("nextInviteAt")) > datetime.now().isoformat(timespec="seconds"),
          f"got {job.get('nextInviteAt')}")


def main() -> None:
    test_error_classification()
    test_manager_cache()
    test_pending_members_parsing()
    test_target_members_cache()
    test_quota_refund()
    test_http_session_pooling()
    test_progress_not_zeroed_for_summary()
    test_status_poll_schedule()
    test_reach_quota_per_person()
    test_friend_request_with_link_flag()
    test_pending_friend_request_flag_persists_in_summary()
    test_escalation_gets_friend_request()

    print(f"\n{'=' * 50}")
    print(f"PASSED: {PASSED}   FAILED: {FAILED}")
    print("=" * 50)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
