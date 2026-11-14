"""
Quản lý lịch gửi tin nhắn Zalo.
Mỗi lịch được lưu trong file JSON riêng: data/message_schedules/sch_xyz.json
"""
import json
import os
import sys
import tempfile
import time
import uuid
from typing import Dict, List, Optional

def _base_dir():
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir: exe ở cùng folder với data/
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
SCHEDULES_DIR = os.path.join(DATA_DIR, "message_schedules")
OLD_SCHEDULES_FILE = os.path.join(DATA_DIR, "message_schedules.json")

def _ensure_dir():
    os.makedirs(SCHEDULES_DIR, exist_ok=True)

def _migrate_old_data():
    """Di chuyển dữ liệu từ message_schedules.json (cũ) sang folder message_schedules/ (mới)."""
    if not os.path.exists(OLD_SCHEDULES_FILE):
        return  # Không có dữ liệu cũ
    
    _ensure_dir()
    
    try:
        with open(OLD_SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        schedules = data.get("schedules", [])
        if not schedules:
            return
        
        # Di chuyển từng schedule
        for sch in schedules:
            schedule_id = sch.get("scheduleId")
            if schedule_id:
                filepath = _get_schedule_file(schedule_id)
                # Chỉ lưu nếu file chưa tồn tại
                if not os.path.exists(filepath):
                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump(sch, f, ensure_ascii=False, indent=2)
        
        print(f"[schedule_manager] Migrated {len(schedules)} schedules from old format")
        
        # Backup file cũ
        backup_file = OLD_SCHEDULES_FILE + ".backup"
        if not os.path.exists(backup_file):
            os.rename(OLD_SCHEDULES_FILE, backup_file)
            print(f"[schedule_manager] Old schedules backed up to {backup_file}")
    
    except Exception as e:
        print(f"[schedule_manager] Error migrating old data: {e}")

def _get_schedule_file(schedule_id: str) -> str:
    """Lấy đường dẫn file của lịch."""
    return os.path.join(SCHEDULES_DIR, f"{schedule_id}.json")

def load_schedules() -> List[dict]:
    """Tải tất cả lịch từ folder message_schedules."""
    _ensure_dir()
    _migrate_old_data()  # Migrate dữ liệu cũ nếu có
    schedules = []
    
    if not os.path.exists(SCHEDULES_DIR):
        return schedules
    
    try:
        for filename in os.listdir(SCHEDULES_DIR):
            if filename.startswith("sch_") and filename.endswith(".json"):
                filepath = os.path.join(SCHEDULES_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        sch = json.load(f)
                        schedules.append(sch)
                except (json.JSONDecodeError, IOError):
                    pass
    except Exception as e:
        print(f"[schedule_manager] Error loading schedules: {e}")
    
    # Sort by createdAt descending
    schedules.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    return schedules

def save_schedule(schedule: dict):
    """Lưu một lịch vào file riêng."""
    _ensure_dir()
    schedule_id = schedule.get("scheduleId")
    if not schedule_id:
        raise ValueError("Schedule không có scheduleId")
    
    filepath = _get_schedule_file(schedule_id)
    # Không cắt rỗng file tiến độ nếu ứng dụng bị đóng giữa lúc ghi.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SCHEDULES_DIR,
                                         suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(schedule, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

def _normalize_message_items(schedule: dict) -> List[dict]:
    """Chuẩn hóa danh sách nội dung tin nhắn (hỗ trợ nhiều tin + ảnh kèm từng tin)."""
    raw_items = schedule.get("messages")
    items = []
    if isinstance(raw_items, list):
        for entry in raw_items:
            if isinstance(entry, str):
                txt = entry.strip()
                if txt:
                    items.append({"text": txt, "photoPath": "", "photoName": ""})
            elif isinstance(entry, dict):
                txt = str(entry.get("text") if "text" in entry else (entry.get("message") or "")).strip()
                ppath = str(entry.get("photoPath") or "").strip()
                pname = str(entry.get("photoName") or "").strip()
                if txt or ppath:
                    items.append({"text": txt, "photoPath": ppath, "photoName": pname})
    if not items:
        txt = str(schedule.get("message") or "").strip()
        ppath = str(schedule.get("photoPath") or "").strip()
        pname = str(schedule.get("photoName") or "").strip()
        if txt or ppath:
            items.append({"text": txt, "photoPath": ppath, "photoName": pname})
    return items


def normalize_schedule(schedule: dict) -> dict:
    """Chuẩn hóa schedule."""
    now_ms = int(time.time() * 1000)
    messages = _normalize_message_items(schedule)
    first_msg = messages[0] if messages else {"text": "", "photoPath": "", "photoName": ""}
    mode = str(schedule.get("messageMode") or "sequence").strip()
    if mode not in ("sequence", "rotate_recipient"):
        mode = "sequence"

    return {
        "scheduleId": schedule.get("scheduleId", "sch_" + str(uuid.uuid4())[:8]),
        "title": (schedule.get("title") or "L\u1ecbch g\u1eedi tin").strip(),
        "accountId": schedule.get("accountId", ""),
        "accountName": schedule.get("accountName", ""),
        "senderName": schedule.get("senderName", schedule.get("accountName", "")),
        "source": schedule.get("source", "group"),
        "groupInfo": schedule.get("groupInfo") or {
            "groupId": "",
            "name": "",
            "totalMember": 0
        },
        "message": first_msg["text"],
        # Ảnh đính kèm (tùy chọn): đường dẫn file tương đối trong thư mục data.
        "photoPath": first_msg["photoPath"],
        "photoName": first_msg["photoName"],
        "messages": messages,
        "messageMode": mode,
        "runAt": schedule.get("runAt", ""),
        "status": schedule.get("status", "pending"),
        "rateLimit": {
            "minDelaySec": max(1, int(schedule.get("rateLimit", {}).get("minDelaySec", 3))),
            "maxDelaySec": max(2, int(schedule.get("rateLimit", {}).get("maxDelaySec", 5))),
            "maxConsecutiveErrors": max(1, int(schedule.get("rateLimit", {}).get("maxConsecutiveErrors", 5))),
        },
        "batchConfig": {
            "batchSize": max(1, int(schedule.get("batchConfig", {}).get("batchSize", 10))),
            "batchDelaySec": max(1, int(schedule.get("batchConfig", {}).get("batchDelaySec", 30))),
        },
        "recipients": schedule.get("recipients", []),
        "results": schedule.get("results", []),
        "createdAt": schedule.get("createdAt", now_ms),
        "updatedAt": schedule.get("updatedAt", now_ms),
    }

def create_schedule(payload: dict) -> dict:
    """Tạo lịch mới."""
    schedule = normalize_schedule(payload)
    
    # Validate
    if not schedule["accountId"]:
        raise ValueError("Chưa chọn tài khoản gửi")
    if not schedule["messages"] and not schedule["message"] and not schedule["photoPath"]:
        raise ValueError("Tin nhắn không được để trống (hoặc phải đính kèm ảnh)")
    if not schedule["recipients"]:
        raise ValueError("Chưa chọn người nhận")
    if not schedule["runAt"]:
        raise ValueError("Thời gian gửi không được để trống")
    
    save_schedule(schedule)
    return schedule

def get_schedule(schedule_id: str) -> Optional[dict]:
    """Lấy một lịch."""
    filepath = _get_schedule_file(schedule_id)
    if not os.path.exists(filepath):
        return None
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

def update_schedule(schedule_id: str, fields: dict) -> dict:
    """Cập nhật lịch."""
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise ValueError("Không tìm thấy lịch")
    
    schedule["updatedAt"] = int(time.time() * 1000)
    schedule.update(fields)
    save_schedule(schedule)
    return schedule

def delete_schedule(schedule_id: str):
    """Xóa lịch (có thể xóa bất kỳ status nào)."""
    filepath = _get_schedule_file(schedule_id)
    if not os.path.exists(filepath):
        raise ValueError("Không tìm thấy lịch")
    
    try:
        os.remove(filepath)
    except OSError as e:
        raise ValueError(f"Lỗi xóa lịch: {e}")

def cancel_schedule(schedule_id: str) -> dict:
    """Hủy lịch nếu pending/running."""
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise ValueError("Không tìm thấy lịch")
    
    if schedule.get("status") not in ["pending", "running"]:
        raise ValueError(f"Không thể hủy lịch đang {schedule.get('status')}")
    
    schedule["status"] = "cancelled"
    schedule["updatedAt"] = int(time.time() * 1000)
    save_schedule(schedule)
    return schedule

def append_schedule_result(schedule_id: str, result: dict):
    """Thêm kết quả gửi vào lịch."""
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise ValueError("Không tìm thấy lịch")
    
    schedule["results"].append(result)
    schedule["updatedAt"] = int(time.time() * 1000)
    save_schedule(schedule)

def get_all_schedules() -> List[dict]:
    """Lấy tất cả lịch."""
    return load_schedules()
