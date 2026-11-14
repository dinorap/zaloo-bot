"""Đo hiệu năng đường đọc/ghi tác vụ sao chép nhóm (trước vs sau).

Mô phỏng chiến dịch lớn và đo đúng thao tác frontend thực hiện mỗi 10 giây.

Chạy:  python tests\\bench_group_copy_read.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.groups import group_copy_manager as mgr  # noqa: E402


def build_jobs(count: int, members_per_job: int) -> list[dict]:
    jobs = []
    for j in range(count):
        members = [
            {
                "userId": f"uid-{j}-{i}",
                "zaloName": f"Người dùng {i}",
                "displayName": f"Người dùng {i}",
                "gender": i % 3,
                "avatar": "",
                "isFriend": i % 2 == 0,
                "status": ["pending", "invited", "joined", "failed", "skipped"][i % 5],
                "error": "",
                "attemptedAt": 0,
                "invitedAt": 0,
                "joinedAt": 0,
                "lastVerifiedAt": 0,
                "inviteAttempts": i % 3,
                "inviteResultCode": 0,
                "inviteResultMessage": "",
                "inviteDelivery": "",
                "friendRequestAttempts": 0,
                "friendRequestAt": 0,
                "friendRequestCode": -1,
                "friendRequestMessage": "",
                "friendRequestDelivery": "",
                "friendRequestCreatedByCampaign": False,
                "friendRemoveAttempts": 0,
                "friendRemovedAt": 0,
                "friendRemoveCode": -1,
                "friendRemoveMessage": "",
                "friendRemoveDelivery": "",
                "campaignAddRetryAttempts": 0,
                "campaignAddRetryAt": 0,
                "joinedAfterCampaignAddRetry": False,
            }
            for i in range(members_per_job)
        ]
        jobs.append({
            "jobId": f"job-{j}",
            "title": f"Chiến dịch {j}",
            "accountId": "acc",
            "status": "pending",
            "createdAt": 1000 + j,
            "updatedAt": 1000 + j,
            "members": members,
            "runs": [],
        })
    return jobs


def old_read_all(jobs_file: str) -> list[dict]:
    """Mô phỏng đường đọc CŨ: parse toàn bộ file + normalize sâu mọi member."""
    with open(jobs_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("jobs", [])
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        job = dict(item)
        normalized = []
        seen = set()
        for m in job.get("members") or []:
            if not isinstance(m, dict):
                continue
            uid = str(m.get("userId") or "").strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            normalized.append(mgr._normalize_member(m))
        job["members"] = normalized
        mgr._refresh_progress(job)
        summary = dict(job)
        summary.pop("members", None)
        out.append(summary)
    out.sort(key=lambda item: int(item.get("createdAt") or 0), reverse=True)
    return out


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    jobs_file = os.path.join(tmpdir, "jobs.json")
    jobs_dir = os.path.join(tmpdir, "jobs_dir")
    job_count = 10
    members_per_job = 500

    jobs = build_jobs(job_count, members_per_job)
    with open(jobs_file, "w", encoding="utf-8") as f:
        json.dump({"jobs": jobs}, f, ensure_ascii=False)
    size_mb = os.path.getsize(jobs_file) / (1024 * 1024)

    print(f"Kịch bản: {job_count} tác vụ x {members_per_job} thành viên "
          f"= {job_count * members_per_job} member")
    print(f"Dữ liệu gốc: {size_mb:.1f} MB\n")

    # ── Đường đọc CŨ ──
    t0 = time.perf_counter()
    for _ in range(20):
        old_read_all(jobs_file)
    old_ms = (time.perf_counter() - t0) * 1000 / 20

    # ── Đường đọc MỚI ──
    mgr.JOBS_FILE = jobs_file
    mgr.JOBS_DIR = jobs_dir
    mgr._invalidate_cache()
    mgr.list_jobs(include_members=False)  # nạp cache lần đầu
    t0 = time.perf_counter()
    for _ in range(20):
        mgr.list_jobs(include_members=False)
    new_ms = (time.perf_counter() - t0) * 1000 / 20

    print("── Frontend poll danh sách tác vụ (mỗi 10s) ──")
    print(f"  CŨ (parse toàn bộ + normalize sâu): {old_ms:8.2f} ms/lần")
    print(f"  MỚI (đọc file tóm tắt + cache):  {new_ms:8.2f} ms/lần")
    if new_ms:
        print(f"  => Nhanh hơn {old_ms / new_ms:.1f}x")

    # ── Ghi 1 tác vụ ──
    job = mgr.get_job("job-0")

    def old_save():
        with open(jobs_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["jobs"][0] = job
        tmp = jobs_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, jobs_file)

    t0 = time.perf_counter()
    for _ in range(10):
        old_save()
    old_save_ms = (time.perf_counter() - t0) * 1000 / 10

    t0 = time.perf_counter()
    for _ in range(10):
        mgr.save_job(job)
    new_save_ms = (time.perf_counter() - t0) * 1000 / 10

    print("\n── Lưu 1 tác vụ (worker gọi rất nhiều) ──")
    print(f"  CŨ (ghi lại toàn bộ file): {old_save_ms:8.2f} ms/lần")
    print(f"  MỚI (ghi đúng 1 file):    {new_save_ms:8.2f} ms/lần")
    if new_save_ms:
        print(f"  => Nhanh hơn {old_save_ms / new_save_ms:.1f}x")

    # Kích thước file sau khi tách
    summary_kb = os.path.getsize(jobs_file) / 1024
    detail_kb = sum(
        os.path.getsize(os.path.join(jobs_dir, f)) for f in os.listdir(jobs_dir)
    ) / 1024
    print(f"\n── Kích thước trên đĩa ──")
    print(f"  File tóm tắt (đọc mỗi poll): {summary_kb:8.1f} KB")
    print(f"  Các file tác vụ riêng:       {detail_kb:8.1f} KB")


if __name__ == "__main__":
    main()
