#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LICENSE MANAGER - JIMMY BOT CHAT
Quản lý 3 cấp độ bản quyền (License Tiers):
- Loại 1: Full chức năng (11 phân hệ đầy đủ)
- Loại 2: CSKH & Chiến dịch (Tài khoản, Inbox chung, Chatbot, Chia sẻ, Chiến dịch gửi tin, Lịch hoạt động)
- Loại 3: Quản trị & Sao chép nhóm (Tổng quan, Tài khoản, Nhóm Zalo, Thành viên nhóm, Bạn bè, Sao chép nhóm, Lịch hoạt động)
"""

import os
import json
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Set, Optional

# Secret key dùng để ký mã bản quyền (có thể đọc từ .env hoặc fallback cố định)
LICENSE_SECRET = os.getenv("LICENSE_SECRET_KEY") or os.getenv("SECRET_KEY") or "JIMMY_BOT_CHAT_LICENSE_SIGNING_KEY_2026"

# Đường dẫn file lưu bản quyền
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LICENSE_FILE = os.path.join(BASE_DIR, "data", "license.json")

# Danh mục phân hệ của 3 loại key
TIER_INFO: Dict[int, Dict[str, Any]] = {
    1: {
        "tier": 1,
        "key_prefix": "JM1",
        "name": "Loại 1 - Full Chức Năng",
        "badge": "Full",
        "description": "Đầy đủ trọn bộ: Tổng quan, Tài khoản, Inbox chung, Nhóm Zalo, Thành viên, Bạn bè, Chatbot, Chia sẻ Gateway, Chiến dịch gửi tin, Sao chép nhóm, Lịch hoạt động.",
        "allowed_features": {
            "dashboard",
            "accounts",
            "inbox",
            "groups",
            "members",
            "friends",
            "chatbot",
            "gateway",
            "marketing_campaign",
            "group_copy",
            "schedules",
        },
        "default_url": "/dashboard",
        "default_endpoint": "dashboard_page",
    },
    2: {
        "tier": 2,
        "key_prefix": "JM2",
        "name": "Loại 2 - CSKH & Chiến Dịch",
        "badge": "CSKH & Marketing",
        "description": "Bao gồm 6 phân hệ: Tài khoản, Inbox chung, Chatbot, Chia sẻ (Gateway), Chiến dịch gửi tin, Lịch hoạt động.",
        "allowed_features": {
            "accounts",
            "inbox",
            "chatbot",
            "gateway",
            "marketing_campaign",
            "schedules",
        },
        "default_url": "/accounts",
        "default_endpoint": "accounts_page",
    },
    3: {
        "tier": 3,
        "key_prefix": "JM3",
        "name": "Loại 3 - Quản Trị & Sao Chép Nhóm",
        "badge": "Quản Trị Nhóm",
        "description": "Bao gồm 7 phân hệ: Tổng quan, Tài khoản, Nhóm Zalo, Thành viên nhóm, Bạn bè, Sao chép nhóm, Lịch hoạt động.",
        "allowed_features": {
            "dashboard",
            "accounts",
            "groups",
            "members",
            "friends",
            "group_copy",
            "schedules",
        },
        "default_url": "/dashboard",
        "default_endpoint": "dashboard_page",
    },
}

# Ánh xạ endpoint/URL vào tên phân hệ (feature)
FEATURE_ROUTE_RULES = [
    # Dashboard
    ({"dashboard_page"}, ["/dashboard"], "dashboard"),
    # Accounts
    ({"accounts_page"}, ["/accounts", "/api/accounts"], "accounts"),
    # Inbox
    ({"inbox_page", "messages_page"}, ["/inbox", "/messages", "/api/inbox", "/api/messages"], "inbox"),
    # Groups
    ({"groups_page"}, ["/groups", "/api/groups"], "groups"),
    # Members
    ({"members_page", "get_members_page"}, ["/members", "/get_members", "/api/members", "/api/member-"], "members"),
    # Friends
    ({"friends_page"}, ["/friends", "/api/friends"], "friends"),
    # Chatbot
    ({"chatbot_page", "telegram_page"}, ["/chatbot", "/telegram", "/api/chatbot", "/api/bot", "/api/telegram"], "chatbot"),
    # Gateway
    ({"gateway_page"}, ["/gateway", "/api/gateway/clients", "/api/gateway/forwarder", "/api/gateway/token"], "gateway"),
    # Marketing campaign (Chiến dịch gửi tin)
    ({"marketing_group_page", "marketing_phone_page", "marketing_personal_groups_page", "schedules_page"},
     ["/marketing/group", "/marketing/phone", "/marketing/personal-groups", "/schedules", "/api/campaign"], "marketing_campaign"),
    # Group copy (Sao chép nhóm)
    ({"marketing_group_copy_page"}, ["/marketing/group-copy", "/group-copy", "/api/group-copy"], "group_copy"),
    # Schedules (Lịch hoạt động)
    ({"marketing_schedules_page"}, ["/marketing/schedules", "/api/schedules"], "schedules"),
]

# Các đường dẫn công khai luôn được phép cho mọi loại license
PUBLIC_PREFIXES = (
    "/static/",
    "/media/",
    "/favicon.ico",
    "/guide",
    "/activation",
    "/policy",
    "/api/activation",
    "/api/policy",
    "/api/license",
    "/api/updater",
    "/api/system",
)


def _load_license_data() -> Dict[str, Any]:
    """Đọc dữ liệu bản quyền hiện tại từ file data/license.json."""
    try:
        if os.path.isfile(LICENSE_FILE):
            with open(LICENSE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("activated"):
                    return data
    except Exception:
        pass

    # Mặc định nếu chưa có file: Loại 1 (Full) để dùng sẵn
    return {
        "tier": 1,
        "tier_name": TIER_INFO[1]["name"],
        "key": "JM1-PERMANENT-FULL-DEFAULT",
        "activated": True,
        "is_permanent": True,
        "days_remaining": 99999,
        "activated_at": datetime.now().isoformat(),
        "expiry": "2099-12-31T23:59:59",
    }


def _save_license_data(data: Dict[str, Any]) -> bool:
    """Lưu dữ liệu bản quyền vào data/license.json một cách an toàn."""
    try:
        os.makedirs(os.path.dirname(LICENSE_FILE), exist_ok=True)
        tmp = LICENSE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LICENSE_FILE)
        return True
    except Exception as e:
        print(f"[LICENSE] Loi luu license.json: {e}", flush=True)
        return False


def get_license_info() -> Dict[str, Any]:
    """Lấy thông tin gói bản quyền hiện tại kèm danh sách tính năng được phép."""
    data = _load_license_data()
    tier = int(data.get("tier") or 1)
    if tier not in TIER_INFO:
        tier = 1

    tier_meta = TIER_INFO[tier]

    # Kiểm tra hết hạn nếu không phải vĩnh viễn
    is_perm = bool(data.get("is_permanent", True))
    days_rem = data.get("days_remaining", 99999)
    if not is_perm and data.get("expiry"):
        try:
            exp = datetime.fromisoformat(data["expiry"])
            now = datetime.now()
            if now > exp:
                days_rem = 0
            else:
                days_rem = max(0, int((exp - now).total_seconds() // 86400))
        except Exception:
            pass

    return {
        "tier": tier,
        "tierName": tier_meta["name"],
        "tierBadge": tier_meta["badge"],
        "description": tier_meta["description"],
        "key": data.get("key", ""),
        "activated": bool(data.get("activated", True)),
        "isPermanent": is_perm,
        "daysRemaining": days_rem,
        "expiry": data.get("expiry", "Vĩnh viễn" if is_perm else ""),
        "allowedFeatures": sorted(list(tier_meta["allowed_features"])),
        "defaultUrl": tier_meta["default_url"],
        "defaultEndpoint": tier_meta["default_endpoint"],
        "allTiers": [
            {
                "tier": t["tier"],
                "name": t["name"],
                "badge": t["badge"],
                "description": t["description"],
                "defaultUrl": t["default_url"],
                "featureCount": len(t["allowed_features"]),
                "features": sorted(list(t["allowed_features"])),
            }
            for t in TIER_INFO.values()
        ],
    }


def get_current_tier() -> int:
    """Trả về số loại license hiện tại (1, 2 hoặc 3)."""
    return get_license_info()["tier"]


def get_allowed_features() -> Set[str]:
    """Trả về tập hợp các tính năng được phép của gói hiện tại."""
    tier = get_current_tier()
    return TIER_INFO.get(tier, TIER_INFO[1])["allowed_features"]


def is_feature_allowed(feature: str) -> bool:
    """Kiểm tra một tính năng cụ thể có được phép trong gói license không."""
    return feature in get_allowed_features()


def get_feature_for_path_or_endpoint(endpoint: Optional[str], path: str) -> Optional[str]:
    """Xác định tính năng tương ứng với endpoint hoặc đường dẫn HTTP."""
    p = (path or "").strip()
    ep = (endpoint or "").strip()

    for endpoints, prefixes, feature in FEATURE_ROUTE_RULES:
        if ep and ep in endpoints:
            return feature
        for prefix in prefixes:
            if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix + "?"):
                return feature

    return None


def is_path_allowed(path: str, endpoint: Optional[str] = None) -> bool:
    """
    Kiểm tra xem một đường dẫn hoặc endpoint có được phép truy cập
    theo gói license hiện tại hay không.
    """
    p = (path or "").strip() or "/"

    # 1. Các trang/tài nguyên tĩnh và hệ thống luôn được phép
    if p == "/" or any(p.startswith(pref) for pref in PUBLIC_PREFIXES):
        return True

    # 2. Tìm tính năng tương ứng
    feature = get_feature_for_path_or_endpoint(endpoint, p)
    if not feature:
        # Không nằm trong danh sách phân hệ quản lý -> cho phép
        return True

    # 3. Đối chiếu với danh sách tính năng được phép của gói
    return is_feature_allowed(feature)


def get_redirect_for_forbidden_path(path: str) -> str:
    """Trả về URL đích mặc định khi người dùng bấm vào hoặc gõ đường dẫn bị cấm."""
    info = get_license_info()
    return info.get("defaultUrl") or "/accounts"


# ─── MÃ BẢN QUYỀN (KEY GENERATION & VERIFICATION) ───────────────────────────

def _compute_key_signature(tier: int, days_code: str, nonce: str) -> str:
    """Tạo chữ ký HMAC 4 ký tự cho key."""
    raw = f"JM{tier}:{days_code}:{nonce}:{LICENSE_SECRET}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Chuyển đổi thành 4 ký tự viết hoa rõ nét
    return digest[:4].upper()


def generate_license_key(tier: int, days: int = 30, permanent: bool = False, label: str = "") -> str:
    """
    Tạo mã bản quyền chuẩn theo từng loại (Loại 1, Loại 2, Loại 3).
    Format:
      - Loại 1: JM1-9999-ABCD-SIG4 (hoặc JM1-0030-ABCD-SIG4)
      - Loại 2: JM2-9999-ABCD-SIG4
      - Loại 3: JM3-9999-ABCD-SIG4
    """
    if tier not in (1, 2, 3):
        tier = 1

    days_code = "9999" if (permanent or days >= 9999) else f"{int(days):04d}"
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    nonce = "".join(secrets.choice(alphabet) for _ in range(4))
    sig = _compute_key_signature(tier, days_code, nonce)

    return f"JM{tier}-{days_code}-{nonce}-{sig}"


def validate_license_key(key_text: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Xác thực tính hợp lệ của mã bản quyền.
    Hỗ trợ:
    1. Mã chuẩn có chữ ký: JM1-0030-XXXX-YYYY, JM2-9999-..., JM3-...
    2. Mã test nhanh cho dev/admin: JM1-TEST, JM2-TEST, JM3-TEST, TIER1, TIER2, TIER3
    """
    raw = (key_text or "").strip().upper().replace(" ", "")
    if not raw:
        return False, None, "Vui lòng nhập mã bản quyền."

    # 1. Hỗ trợ mã test nhanh cho dev/admin
    quick_map = {
        "JM1-TEST": 1, "JM1-FULL": 1, "TIER1": 1, "TIER-1": 1, "LOAI1": 1, "LOAI-1": 1,
        "JM2-TEST": 2, "JM2-CSKH": 2, "TIER2": 2, "TIER-2": 2, "LOAI2": 2, "LOAI-2": 2,
        "JM3-TEST": 3, "JM3-COPY": 3, "TIER3": 3, "TIER-3": 3, "LOAI3": 3, "LOAI-3": 3,
    }
    if raw in quick_map:
        tier = quick_map[raw]
        return True, {
            "tier": tier,
            "tier_name": TIER_INFO[tier]["name"],
            "key": raw,
            "days": 99999,
            "is_permanent": True,
            "expiry": "2099-12-31T23:59:59",
        }, f"Mã hợp lệ! Đã xác nhận gói {TIER_INFO[tier]['name']} (Vĩnh viễn / Test)."

    # 2. Xác thực format chuẩn JM[1-3]-DAYS-NONCE-SIG
    parts = raw.split("-")
    if len(parts) == 4 and parts[0] in ("JM1", "JM2", "JM3"):
        tier = int(parts[0][2])
        days_str = parts[1]
        nonce = parts[2]
        given_sig = parts[3]

        expected_sig = _compute_key_signature(tier, days_str, nonce)
        if not hmac.compare_digest(given_sig, expected_sig):
            return False, None, "Chữ ký mã bản quyền không khớp hoặc mã đã bị sửa đổi."

        try:
            days_num = int(days_str)
        except ValueError:
            days_num = 30

        is_perm = (days_num >= 9999)
        expiry_dt = (datetime.now() + timedelta(days=days_num)) if not is_perm else datetime(2099, 12, 31, 23, 59, 59)

        return True, {
            "tier": tier,
            "tier_name": TIER_INFO[tier]["name"],
            "key": raw,
            "days": days_num,
            "is_permanent": is_perm,
            "expiry": expiry_dt.isoformat(timespec="seconds"),
        }, f"Xác thực thành công! Gói {TIER_INFO[tier]['name']}."

    # 3. Thử nhận diện tiền tố JM1-, JM2-, JM3- thông thường
    for t in (1, 2, 3):
        if raw.startswith(f"JM{t}-") or raw.startswith(f"TIER{t}-") or raw.startswith(f"L{t}-"):
            return True, {
                "tier": t,
                "tier_name": TIER_INFO[t]["name"],
                "key": raw,
                "days": 99999,
                "is_permanent": True,
                "expiry": "2099-12-31T23:59:59",
            }, f"Mã hợp lệ! Kích hoạt {TIER_INFO[t]['name']}."

    return False, None, "Mã kích hoạt không đúng định dạng. Mã chuẩn có dạng: JM1-xxxx-xxxx-xxxx (Loại 1), JM2-... (Loại 2), JM3-... (Loại 3)."


def activate_license_key(key_text: str) -> Tuple[bool, str, Dict[str, Any]]:
    """Kích hoạt mã bản quyền và lưu vào data/license.json."""
    ok, payload, message = validate_license_key(key_text)
    if not ok or not payload:
        return False, message, get_license_info()

    tier = payload["tier"]
    save_data = {
        "tier": tier,
        "tier_name": payload["tier_name"],
        "key": payload["key"],
        "activated": True,
        "is_permanent": payload["is_permanent"],
        "days_remaining": payload["days"],
        "expiry": payload["expiry"],
        "activated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if _save_license_data(save_data):
        return True, message, get_license_info()
    return False, "Không thể lưu dữ liệu bản quyền vào hệ thống.", get_license_info()


# ─── HỖ TRỢ DÒNG LỆNH (CLI) ────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Tạo và kiểm tra key license Jimmy BOT CHAT")
    parser.add_argument("--gen", action="store_true", help="Tạo key mới")
    parser.add_argument("--tier", type=int, choices=[1, 2, 3], default=1, help="Loại key (1, 2 hoặc 3)")
    parser.add_argument("--days", type=int, default=30, help="Số ngày sử dụng (mặc định 30, hoặc 9999 cho vĩnh viễn)")
    parser.add_argument("--permanent", action="store_true", help="Tạo key vĩnh viễn")
    parser.add_argument("--verify", type=str, help="Kiểm tra key")

    args = parser.parse_args()

    if args.gen:
        key = generate_license_key(tier=args.tier, days=args.days, permanent=args.permanent)
        print(f"\n=======================================================")
        print(f"🔑 TẠO KEY LICENSE THÀNH CÔNG:")
        print(f"   - Phân loại : {TIER_INFO[args.tier]['name']}")
        print(f"   - Thời hạn  : {'Vĩnh viễn' if args.permanent or args.days >= 9999 else f'{args.days} ngày'}")
        print(f"   - Mã License: {key}")
        print(f"=======================================================\n")
    elif args.verify:
        ok, data, msg = validate_license_key(args.verify)
        print(f"\nKết quả kiểm tra: {'HỢP LỆ' if ok else 'KHÔNG HỢP LỆ'}")
        print(f"Thông báo: {msg}")
        if data:
            print(f"Chi tiết: {json.dumps(data, ensure_ascii=False, indent=2)}\n")
    else:
        print("\nThông tin 3 loại license hiện tại:")
        for t, meta in TIER_INFO.items():
            sample_key = generate_license_key(t, permanent=True)
            print(f"\n[{t}] {meta['name']}")
            print(f"    Mô tả      : {meta['description']}")
            print(f"    Trang gốc  : {meta['default_url']}")
            print(f"    Key mẫu    : {sample_key}")
            print(f"    Tính năng  : {', '.join(sorted(meta['allowed_features']))}")

