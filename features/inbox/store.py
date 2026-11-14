"""Inbox store: hoi thoai chung cho 5 may, claim, read, presence, audit."""
import json
import os
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
INBOX_PATH = os.path.join(DATA_DIR, "inbox.json")
USERS_PATH = os.path.join(DATA_DIR, "inbox_users.json")
AUDIT_PATH = os.path.join(DATA_DIR, "inbox_audit.json")
DIRECTORY_PATH = os.path.join(DATA_DIR, "inbox_directory.json")
CONFIG_PATH = os.path.join(DATA_DIR, "inbox_config.json")
MEDIA_DIR = os.path.join(DATA_DIR, "inbox_media")
MEDIA_MAP_PATH = os.path.join(DATA_DIR, "inbox_media.json")

_lock = threading.RLock()
_claim_ttl = 10 * 60  # 10 phut

DEFAULT_USERS = {
    "admin": {"name": "admin", "role": "admin", "createdAt": 0},
}


def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d
    except Exception:
        pass
    return default


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_inbox():
    d = _read_json(INBOX_PATH, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("conversations", {})
    d.setdefault("seq", 0)
    return d


def _save_inbox(d):
    _write_json(INBOX_PATH, d)


def _conv_id(account_id, kind, key):
    return f"{account_id}:{kind}:{key}"


def get_directory(account_id=""):
    d = _read_json(DIRECTORY_PATH, {})
    if not isinstance(d, dict):
        return {"groups": [], "friends": [], "accountId": account_id, "updatedAt": 0}
    if account_id and str(d.get("accountId") or "") not in ("", str(account_id)):
        return {"groups": [], "friends": [], "accountId": account_id, "updatedAt": 0}
    d.setdefault("groups", [])
    d.setdefault("friends", [])
    return d


def rebuild_directory(account_id, groups, friends):
    """Luu danh ba that: groups [{groupId,name,avatar,memberCount}], friends [{userId,name,avatar}]."""
    account_id = str(account_id or "").strip()
    clean_groups = []
    seen_g = set()
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("groupId") or g.get("gridId") or g.get("id") or "").strip()
        if not gid or gid in seen_g:
            continue
        seen_g.add(gid)
        clean_groups.append({
            "groupId": gid,
            "name": str(g.get("name") or g.get("grid_name") or g.get("groupName") or ("Nhom " + gid[:8])).strip(),
            "avatar": str(g.get("avatar") or g.get("fullAvt") or g.get("avt") or "").strip(),
            "memberCount": int(g.get("memberCount") or g.get("grid_totalMember") or g.get("totalMember") or 0),
        })
    clean_friends = []
    seen_f = set()
    for f in friends or []:
        if not isinstance(f, dict):
            continue
        uid = str(f.get("userId") or f.get("uid") or f.get("id") or "").strip()
        if not uid or uid in seen_f:
            continue
        seen_f.add(uid)
        clean_friends.append({
            "userId": uid,
            "name": str(f.get("zaloName") or f.get("displayName") or f.get("name") or uid).strip(),
            "avatar": str(f.get("avatar") or f.get("avt") or "").strip(),
        })
    clean_groups.sort(key=lambda x: (x.get("name") or "").lower())
    clean_friends.sort(key=lambda x: (x.get("name") or "").lower())
    d = {"accountId": account_id, "groups": clean_groups, "friends": clean_friends,
         "totalGroups": len(clean_groups), "totalFriends": len(clean_friends),
         "updatedAt": _now_ms()}
    _write_json(DIRECTORY_PATH, d)
    return d


def ensure_conversations(account_id, groups, friends):
    """Tao vo hoi thoai cho TAT CA nhom + ban be de cot trai hien nhu that.
    Tra ve (created, updated). Khong xoa tin cu."""
    account_id = str(account_id or "").strip()
    if not account_id:
        return (0, 0)
    created, updated = 0, 0
    with _lock:
        db = _load_inbox()
        convs = db.setdefault("conversations", {})
        for g in groups or []:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("groupId") or "").strip()
            if not gid:
                continue
            cid = _conv_id(account_id, "group", gid)
            name = str(g.get("name") or gid).strip()
            avatar = str(g.get("avatar") or g.get("fullAvt") or "").strip()
            conv = convs.get(cid)
            if not isinstance(conv, dict):
                convs[cid] = {"convId": cid, "accountId": account_id, "kind": "group", "key": gid,
                              "name": name, "avatar": avatar,
                              "memberCount": int(g.get("memberCount") or 0),
                              "messages": [], "lastMsgId": "", "lastTs": 0, "lastText": "",
                              "claim": None, "reads": {}, "updatedAt": _now_ms()}
                created += 1
            else:
                ch = False
                if name and conv.get("name") != name:
                    conv["name"] = name
                    ch = True
                if avatar and conv.get("avatar") != avatar:
                    conv["avatar"] = avatar
                    ch = True
                try:
                    mc = int(g.get("memberCount") or 0)
                    if mc and conv.get("memberCount") != mc:
                        conv["memberCount"] = mc
                        ch = True
                except Exception:
                    pass
                if ch:
                    updated += 1
        for f in friends or []:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("userId") or "").strip()
            if not uid:
                continue
            cid = _conv_id(account_id, "sms", uid)
            name = str(f.get("name") or uid).strip()
            avatar = str(f.get("avatar") or "").strip()
            conv = convs.get(cid)
            if not isinstance(conv, dict):
                convs[cid] = {"convId": cid, "accountId": account_id, "kind": "sms", "key": uid,
                              "name": name, "avatar": avatar, "memberCount": 0,
                              "messages": [], "lastMsgId": "", "lastTs": 0, "lastText": "",
                              "claim": None, "reads": {}, "updatedAt": _now_ms()}
                created += 1
            else:
                ch = False
                if name and _is_placeholder_name(conv.get("name"), uid):
                    conv["name"] = name
                    ch = True
                if avatar and not conv.get("avatar"):
                    conv["avatar"] = avatar
                    ch = True
                if ch:
                    updated += 1
        _save_inbox(db)
    return (created, updated)


def _is_placeholder_name(name, uid=""):
    s = str(name or "").strip()
    u = str(uid or "").strip()
    if not s or s in ("Khach", "Người lạ"):
        return True
    if u and s == u:
        return True
    if u and len(u) >= 4 and s == ("Zalo " + u[-4:]):
        return True
    if u and s == ("Nhom " + u[:8]):
        return True
    if s.isdigit() and len(s) >= 8:
        return True
    return False


def list_unresolved_targets(account_id):
    """Liệt kê (uids, gids) trong inbox.json của account còn thiếu tên thật hoặc avatar."""
    account_id = str(account_id or "").strip()
    uids = []
    gids = []
    seen_u = set()
    seen_g = set()
    if not account_id:
        return uids, gids
    with _lock:
        db = _load_inbox()
        for conv in (db.get("conversations") or {}).values():
            if not isinstance(conv, dict) or str(conv.get("accountId") or "") != account_id:
                continue
            kind = str(conv.get("kind") or "")
            key = str(conv.get("key") or "").strip()
            if kind == "sms" and key:
                if _is_placeholder_name(conv.get("name"), key) or not str(conv.get("avatar") or "").strip():
                    if key not in seen_u:
                        seen_u.add(key)
                        uids.append(key)
            elif kind == "group" and key:
                if _is_placeholder_name(conv.get("name"), key) or not str(conv.get("avatar") or "").strip():
                    if key not in seen_g:
                        seen_g.add(key)
                        gids.append(key)
            for m in (conv.get("messages") or [])[-30:]:
                if not isinstance(m, dict):
                    continue
                if str(m.get("from") or "") in ("me", "staff"):
                    continue
                suid = str(m.get("senderUid") or "").strip()
                if suid and suid != "0" and suid not in seen_u:
                    if _is_placeholder_name(m.get("senderName"), suid) or not str(m.get("senderAvatar") or "").strip():
                        seen_u.add(suid)
                        uids.append(suid)
    return uids, gids


def patch_profiles(account_id, uid_profile_map=None, gid_group_map=None):
    """Cập nhật tên/avatar vào cả convs lẫn senderName/senderAvatar của tin nhắn cũ.
    Trả về danh sách convId đã thay đổi.
    """
    account_id = str(account_id or "").strip()
    uid_profile_map = uid_profile_map or {}
    gid_group_map = gid_group_map or {}
    if not account_id or (not uid_profile_map and not gid_group_map):
        return []
    changed_cids = []
    with _lock:
        db = _load_inbox()
        convs = db.get("conversations") or {}
        dirty = False
        for cid, conv in convs.items():
            if not isinstance(conv, dict) or str(conv.get("accountId") or "") != account_id:
                continue
            conv_changed = False
            kind = str(conv.get("kind") or "")
            key = str(conv.get("key") or "").strip()
            if kind == "sms" and key in uid_profile_map:
                nm, av = uid_profile_map[key]
                nm = str(nm or "").strip()
                av = str(av or "").strip()
                if nm and (_is_placeholder_name(conv.get("name"), key) or conv.get("name") != nm):
                    conv["name"] = nm
                    conv_changed = True
                if av and conv.get("avatar") != av:
                    conv["avatar"] = av
                    conv_changed = True
            elif kind == "group" and key in gid_group_map:
                nm, av, mc = gid_group_map[key]
                nm = str(nm or "").strip()
                av = str(av or "").strip()
                if nm and (_is_placeholder_name(conv.get("name"), key) or conv.get("name") != nm):
                    conv["name"] = nm
                    conv_changed = True
                if av and conv.get("avatar") != av:
                    conv["avatar"] = av
                    conv_changed = True
                if mc and conv.get("memberCount") != mc:
                    conv["memberCount"] = mc
                    conv_changed = True

            for m in conv.get("messages") or []:
                if not isinstance(m, dict) or str(m.get("from") or "") in ("me", "staff"):
                    continue
                suid = str(m.get("senderUid") or "").strip()
                if not suid and kind == "sms":
                    suid = key
                if suid in uid_profile_map:
                    nm, av = uid_profile_map[suid]
                    nm = str(nm or "").strip()
                    av = str(av or "").strip()
                    if nm and _is_placeholder_name(m.get("senderName"), suid):
                        m["senderName"] = nm
                        conv_changed = True
                    elif nm and m.get("senderName") != nm and kind == "sms":
                        m["senderName"] = nm
                        conv_changed = True
                    if av and m.get("senderAvatar") != av:
                        m["senderAvatar"] = av
                        conv_changed = True

            if conv_changed:
                conv["updatedAt"] = _now_ms()
                changed_cids.append(cid)
                dirty = True
        if dirty:
            _save_inbox(db)
    return changed_cids


def lookup_name_avatar(account_id, sender_uid):
    """Tra ten/avatar nguoi gui tu directory ban be (nhanh, khong goi API)."""
    try:
        d = get_directory(account_id)
        for f in d.get("friends") or []:
            if str(f.get("userId") or "") == str(sender_uid):
                return str(f.get("name") or ""), str(f.get("avatar") or "")
    except Exception:
        pass
    return "", ""


def _now_ms():
    return int(time.time() * 1000)


def staff_role(staff):
    nm = str(staff or "").strip()
    low = nm.lower()
    if low == "super admin":
        return "super_admin"
    if low == "admin" or low.startswith("admin "):
        return "admin" if nm == "admin" else "manager"
    try:
        from features.gateway import config as _gc
        prof = _gc.get_staff_profile(nm)
        if prof:
            return _gc.normalize_staff_role(prof.get("role"))
    except Exception:
        pass
    users = _read_json(USERS_PATH, {})
    if not isinstance(users, dict):
        users = {}
    u = users.get(nm)
    if isinstance(u, dict):
        r = str(u.get("role") or "sale").strip().lower()
        if r == "admin":
            return "admin"
        if r == "manager":
            return "manager"
        return "sale"
    if not users:
        return "admin" if nm == "admin" else "sale"
    return "sale"


def ensure_staff(staff, role=None):
    staff = (staff or "").strip() or "nv1"
    gw_role = ""
    try:
        from features.gateway import config as _gc
        prof = _gc.get_staff_profile(staff)
        if prof:
            gw_role = _gc.normalize_staff_role(prof.get("role"))
    except Exception:
        gw_role = ""
    with _lock:
        users = _read_json(USERS_PATH, {})
        if not isinstance(users, dict):
            users = {}
        if staff not in users:
            resolved_role = role or gw_role or ("admin" if (staff == "admin" or not users) else "sale")
            users[staff] = {"name": staff, "role": resolved_role, "createdAt": int(time.time())}
            _write_json(USERS_PATH, users)
        elif gw_role and isinstance(users.get(staff), dict) and users[staff].get("role") != gw_role:
            users[staff]["role"] = gw_role
            _write_json(USERS_PATH, users)
    return staff


def audit(action, staff, conv_id="", detail=""):
    try:
        with _lock:
            logs = _read_json(AUDIT_PATH, [])
            if not isinstance(logs, list):
                logs = []
            logs.append({"at": int(time.time() * 1000), "action": action, "staff": staff,
                         "convId": conv_id, "detail": str(detail or "")[:500]})
            logs = logs[-1000:]
            _write_json(AUDIT_PATH, logs)
    except Exception:
        pass


def list_audit(limit=100):
    logs = _read_json(AUDIT_PATH, [])
    if not isinstance(logs, list):
        return []
    return list(logs)[-int(limit or 100):]


def ingest_zalo(account_id, kind, key, zmsg, conv_name="", conv_avatar="", own_uid=""):
    """Luu tin tu Zalo poll vao inbox. zmsg: {msgId, senderUid, senderName, senderAvatar, text, thumb, href, msgType, ts}"""
    account_id = str(account_id or "").strip()
    key = str(key or "").strip()
    kind = "sms" if kind == "sms" else "group"
    own_uid = str(own_uid or "").strip()
    if not account_id or not key:
        return None
    msg_id = str((zmsg or {}).get("msgId") or (zmsg or {}).get("globalMsgId") or "").strip()
    if not msg_id:
        return None
    ts = int((zmsg or {}).get("ts") or (zmsg or {}).get("createTime") or 0)
    cid = _conv_id(account_id, kind, key)
    with _lock:
        db = _load_inbox()
        convs = db["conversations"]
        conv = convs.get(cid)
        if not isinstance(conv, dict):
            conv = {"convId": cid, "accountId": account_id, "kind": kind, "key": key,
                    "name": conv_name or key, "avatar": conv_avatar or "",
                    "messages": [], "lastMsgId": "", "lastTs": 0, "lastText": "",
                    "claim": None, "reads": {}, "updatedAt": _now_ms()}
            convs[cid] = conv
        if conv_name and (not _is_placeholder_name(conv_name, key) or _is_placeholder_name(conv.get("name"), key)):
            conv["name"] = conv_name
        if conv_avatar:
            conv["avatar"] = conv_avatar
        seen = {str(m.get("msgId") or "") for m in conv.get("messages", [])}
        if msg_id in seen or msg_id == str(conv.get("lastMsgId") or ""):
            return None
        ts_raw = int(ts or 0) or 0
        # Chuẩn hoá ts về ms (Zalo có lúc trả giây, lúc trả ms)
        if ts_raw and ts_raw < 1000000000000 and ts_raw > 1000000000:
            ts_raw = ts_raw * 1000
        ts_ms = ts_raw or _now_ms()
        sender_uid = str((zmsg or {}).get("senderUid") or "")
        mine = sender_uid in ("", "0") or (bool(own_uid) and sender_uid == own_uid)
        text_in = str((zmsg or {}).get("text") or (zmsg or {}).get("title") or "")
        if mine and text_in.strip():
            # Chống nhân đôi tin tự gửi: vừa gửi qua /api/inbox/send (msgId staff-...)
            # Zalo poll lại sẽ trả về msgId server cho cùng nội dung (senderUid có thể
            # là "", "0" hoặc chính uid của nick). Gộp vào bản staff thay vì thêm
            # 1 dòng mới (lỗi "2 cái chào" trên ảnh).
            msgs = conv.get("messages", []) or []
            for m in reversed(msgs[-12:]):
                if not isinstance(m, dict):
                    continue
                if str(m.get("from") or "") not in ("staff", "me"):
                    continue
                try:
                    mts = int(m.get("ts") or 0)
                    if mts and mts < 1000000000000 and mts > 1000000000:
                        mts = mts * 1000
                except Exception:
                    mts = 0
                same_text = str(m.get("text") or "").strip() == text_in.strip()
                same_photo = (
                    bool(m.get("mediaToken"))
                    and str((zmsg or {}).get("msgType") or "") in ("chat.photo", "share.file")
                    and abs((mts or ts_ms) - ts_ms) < 120000
                )
                # staff-xxx: luôn gộp theo nội dung hoặc ảnh vừa gửi
                if (same_text or same_photo) and abs((mts or ts_ms) - ts_ms) < 600000:
                    if str(m.get("from") or "") == "staff":
                        m["msgId"] = msg_id
                    cli_in = str((zmsg or {}).get("cliMsgId") or "").strip()
                    if cli_in and not m.get("cliMsgId"):
                        m["cliMsgId"] = cli_in
                    if same_photo and not m.get("thumb") and (zmsg or {}).get("thumb"):
                        m["thumb"] = str((zmsg or {}).get("thumb") or "")
                    m["ts"] = ts_ms
                    conv["lastMsgId"] = msg_id
                    conv["lastTs"] = ts_ms
                    conv["lastText"] = text_in[:200]
                    conv["updatedAt"] = _now_ms()
                    _save_inbox(db)
                    return {"convId": cid, "message": dict(m), "conv": public_conv(conv), "deduped": True}
        if mine and not text_in.strip():
            # Tin tự gửi không có nội dung hiển thị (thumb/href) -> bỏ qua để
            # khỏi tạo dòng trắng thứ 2.
            return None
        sender_name = str((zmsg or {}).get("senderName") or "").strip()
        sender_avatar = str((zmsg or {}).get("senderAvatar") or "").strip()
        if not mine and (not sender_name or sender_name in ("", "Khach")):
            try:
                nm, av = lookup_name_avatar(account_id, sender_uid)
                if nm:
                    sender_name = nm
                if av and not sender_avatar:
                    sender_avatar = av
            except Exception:
                pass
        if mine and not sender_name:
            sender_name = "Nhan vien"
        if not mine and not sender_name:
            sender_name = "Zalo " + (sender_uid[-4:] if len(sender_uid) >= 4 else sender_uid)
        rec = {"msgId": msg_id,
               "cliMsgId": str((zmsg or {}).get("cliMsgId") or "").strip(),
               "ts": ts or _now_ms(), "at": _now_ms(),
               "from": "me" if mine else "zalo",
               "senderUid": sender_uid,
               "senderName": sender_name,
               "senderAvatar": sender_avatar,
               "staff": "",
               "text": str((zmsg or {}).get("text") or (zmsg or {}).get("title") or ""),
               "thumb": str((zmsg or {}).get("thumb") or ""),
               "href": str((zmsg or {}).get("href") or ""),
               "msgType": str((zmsg or {}).get("msgType") or ""),
               "mediaToken": str((zmsg or {}).get("mediaToken") or "")}
        conv.setdefault("messages", []).append(rec)
        conv["messages"] = conv["messages"][-200:]
        conv["lastMsgId"] = msg_id
        conv["lastTs"] = rec["ts"]
        conv["lastText"] = rec["text"][:200]
        conv["updatedAt"] = _now_ms()
        db["seq"] = int(db.get("seq") or 0) + 1
        _save_inbox(db)
        return {"convId": cid, "message": rec, "conv": public_conv(conv)}


def add_outbound(account_id, kind, key, staff, text, meta=None):
    account_id = str(account_id or "").strip()
    key = str(key or "").strip()
    kind = "sms" if kind == "sms" else "group"
    staff = (staff or "").strip() or "nv1"
    cid = _conv_id(account_id, kind, key)
    with _lock:
        db = _load_inbox()
        conv = db["conversations"].get(cid)
        if not isinstance(conv, dict):
            conv = {"convId": cid, "accountId": account_id, "kind": kind, "key": key,
                    "name": key, "avatar": "", "messages": [], "lastMsgId": "",
                    "lastTs": 0, "lastText": "", "claim": None, "reads": {}, "updatedAt": _now_ms()}
            db["conversations"][cid] = conv
        db["seq"] = int(db.get("seq") or 0) + 1
        mid = str((meta or {}).get("zaloMsgId") or "").strip() or f"staff-{db['seq']}-{int(time.time()*1000)}"
        cli_id = str((meta or {}).get("cliMsgId") or "").strip()
        rec = {"msgId": mid, "cliMsgId": cli_id,
               "ts": _now_ms(), "at": _now_ms(), "from": "staff",
               "senderUid": "", "senderName": staff, "staff": staff,
               "text": str(text or ""), "thumb": str((meta or {}).get("thumb") or ""),
               "href": str((meta or {}).get("href") or ""),
               "msgType": str((meta or {}).get("msgType") or "staff.text"),
               "mediaToken": str((meta or {}).get("mediaToken") or ""),
               "fileName": str((meta or {}).get("fileName") or ""),
               "fileSize": int((meta or {}).get("fileSize") or 0)}
        conv.setdefault("messages", []).append(rec)
        conv["messages"] = conv["messages"][-200:]
        conv["lastMsgId"] = mid
        conv["lastTs"] = rec["ts"]
        default_prev = ""
        if rec["msgType"] == "share.file":
            default_prev = f"[Tệp] {rec['fileName']}" if rec["fileName"] else "[Tệp đính kèm]"
        elif rec.get("mediaToken") or rec.get("thumb"):
            default_prev = "[Hình ảnh]"
        conv["lastText"] = rec["text"][:200] or default_prev[:200]
        conv["updatedAt"] = _now_ms()
        _save_inbox(db)
        audit("send", staff, cid, rec["text"][:200])
        return {"convId": cid, "message": rec, "conv": public_conv(conv)}


def find_message(conv_id, msg_id):
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return None, None
        for m in conv.get("messages") or []:
            if isinstance(m, dict) and str(m.get("msgId") or "") == str(msg_id or ""):
                return dict(conv), dict(m)
    return dict(conv), None


def recall_message(conv_id, msg_id, staff=""):
    staff = (staff or "").strip() or "nv1"
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return {"ok": False, "error": "Không tìm thấy hội thoại."}
        target = None
        for m in conv.get("messages") or []:
            if isinstance(m, dict) and str(m.get("msgId") or "") == str(msg_id or ""):
                target = m
                break
        if target is None:
            return {"ok": False, "error": "Không tìm thấy tin nhắn."}
        target["recalled"] = True
        target["msgType"] = "chat.undo"
        target["text"] = "[Tin nhắn đã được thu hồi]"
        target["thumb"] = ""
        target["href"] = ""
        target["mediaToken"] = ""
        target["recalledBy"] = staff
        target["recalledAt"] = _now_ms()
        if str(conv.get("lastMsgId") or "") == str(msg_id or ""):
            conv["lastText"] = "[Tin nhắn đã được thu hồi]"
        conv["updatedAt"] = _now_ms()
        _save_inbox(db)
        audit("recall", staff, conv_id, str(msg_id))
        return {"ok": True, "convId": conv_id, "message": dict(target), "conv": public_conv(conv)}


def _claim_expired(claim):
    if not isinstance(claim, dict):
        return True
    return float(claim.get("until") or 0) < time.time()


def public_conv(conv):
    c = dict(conv or {})
    claim = c.get("claim")
    if _claim_expired(claim):
        c["claim"] = None
        c["claimedBy"] = ""
    else:
        c["claimedBy"] = str((claim or {}).get("staff") or "")
    msgs = c.get("messages") or []
    c["messages"] = msgs[-50:]
    if not c.get("lastRead") and isinstance(c.get("reads"), dict) and c["reads"]:
        best = None
        for st_name, r_info in c["reads"].items():
            if not isinstance(r_info, dict):
                continue
            if best is None or int(r_info.get("at") or 0) > int(best.get("at") or 0):
                best = {"msgId": str(r_info.get("msgId") or ""), "at": int(r_info.get("at") or 0), "by": str(st_name)}
        if best:
            c["lastRead"] = best
    return c


def list_conversations(account_id="", only_group_ids=None, only_friend_ids=None, hide_empty=False,
                       account_ids=None, account_meta=None):
    with _lock:
        db = _load_inbox()
        convs = (db.get("conversations") or {}).values()
        acc_set = {str(x).strip() for x in (account_ids or []) if str(x).strip()}
        if acc_set:
            convs = [v for v in convs if str((v or {}).get("accountId") or "") in acc_set]
        elif account_id:
            convs = [v for v in convs if str((v or {}).get("accountId") or "") == str(account_id)]
        out = [public_conv(v) for v in convs]
    if isinstance(account_meta, dict) and account_meta:
        for c in out:
            aid = str(c.get("accountId") or "")
            meta = account_meta.get(aid)
            if isinstance(meta, dict):
                c["accountName"] = str(meta.get("name") or aid)
                c["accountAvatar"] = str(meta.get("avatar") or "")
    # Lọc theo setup: chỉ hiện nhóm/bạn được tick. Để trống = hiện tất cả.
    try:
        gset = {str(x).strip() for x in (only_group_ids or []) if str(x).strip()}
        fset = {str(x).strip() for x in (only_friend_ids or []) if str(x).strip()}
    except Exception:
        gset, fset = set(), set()
    if gset or fset:
        keep = []
        for c in out:
            k = str(c.get("kind") or "")
            key = str(c.get("key") or "")
            if k == "group" and gset and key not in gset:
                continue
            if k == "sms" and fset and key not in fset:
                continue
            keep.append(c)
        out = keep
    if hide_empty:
        out = [c for c in out if (c.get("messages") or []) or str(c.get("lastText") or "").strip()]
    out.sort(key=lambda x: int(x.get("lastTs") or x.get("updatedAt") or 0), reverse=True)
    return out


def prune_stale_conversations(account_id, valid_group_ids=None, valid_friend_ids=None):
    """Xóa hội thoại của account KHÔNG còn trong danh bạ (đổi acc / rời nhóm / unfriend).

    Trả về số hội thoại đã xóa. Không đụng tới account khác."""
    account_id = str(account_id or "").strip()
    if not account_id:
        return 0
    try:
        gset = {str(x).strip() for x in (valid_group_ids or []) if str(x).strip()}
        fset = {str(x).strip() for x in (valid_friend_ids or []) if str(x).strip()}
    except Exception:
        gset, fset = set(), set()
    removed = 0
    with _lock:
        db = _load_inbox()
        convs = db.get("conversations") or {}
        for cid in list(convs.keys()):
            c = convs.get(cid) or {}
            if str(c.get("accountId") or "") != account_id:
                continue
            k = str(c.get("kind") or "")
            key = str(c.get("key") or "")
            if k == "group" and gset and key not in gset:
                del convs[cid]
                removed += 1
            elif k == "sms" and fset and key not in fset:
                del convs[cid]
                removed += 1
        if removed:
            _save_inbox(db)
    return removed


def get_messages(conv_id, limit=80):
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return None
        msgs = (conv.get("messages") or [])[-int(limit or 80):]
        return {"conv": public_conv(conv), "messages": msgs}


def claim(conv_id, staff, ttl_sec=600, force=False):
    staff = ensure_staff(staff)
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return {"ok": False, "error": "Khong tim thay hoi thoai."}
        cur = conv.get("claim")
        prev_owner = ""
        if isinstance(cur, dict) and not _claim_expired(cur) and str(cur.get("staff") or "") != staff:
            prev_owner = str(cur.get("staff") or "")
            if not (force and staff_role(staff) in ("super_admin", "admin", "manager")):
                return {"ok": False, "error": f"Dang do {prev_owner} xu ly.", "claimedBy": prev_owner}
        conv["claim"] = {"staff": staff, "since": int(time.time()), "until": time.time() + max(60, int(ttl_sec or 600))}
        _save_inbox(db)
        if prev_owner:
            audit("takeover_claim", staff, conv_id, f"tiếp quản từ {prev_owner}")
        else:
            audit("claim", staff, conv_id, f"giữ {ttl_sec}s")
        return {"ok": True, "conv": public_conv(conv)}


def release(conv_id, staff):
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return {"ok": False}
        cur = conv.get("claim")
        if (isinstance(cur, dict) and str(cur.get("staff") or "") != (staff or "")
                and staff_role(staff) not in ("super_admin", "admin", "manager") and not _claim_expired(cur)):
            return {"ok": False, "error": "Hoi thoai dang duoc nguoi khac xu ly."}
        conv["claim"] = None
        _save_inbox(db)
        audit("release", staff or "", conv_id, "")
        return {"ok": True, "conv": public_conv(conv)}


def mark_read(conv_id, staff, msg_id=""):
    staff = ensure_staff(staff)
    with _lock:
        db = _load_inbox()
        conv = (db.get("conversations") or {}).get(conv_id)
        if not isinstance(conv, dict):
            return {"ok": False}
        msgs = conv.get("messages") or []
        fallback_mid = str((msgs[-1] or {}).get("msgId") or "") if msgs else ""
        target_mid = str(msg_id or conv.get("lastMsgId") or fallback_mid or "")
        now_ms = _now_ms()
        reads = conv.setdefault("reads", {})
        reads[staff] = {"msgId": target_mid, "at": now_ms}
        conv["lastRead"] = {"msgId": target_mid, "at": now_ms, "by": staff}
        conv["unread"] = 0
        _save_inbox(db)
        return {"ok": True, "reads": reads, "lastRead": conv["lastRead"], "msgId": target_mid, "at": now_ms}


# --- presence ---
_presence = {}
_presence_lock = threading.RLock()


def heartbeat(staff, status=None, conv_id=None, conv_name=None, action=None):
    staff = ensure_staff(staff)
    now = time.time()
    with _presence_lock:
        prev = _presence.get(staff)
        if not isinstance(prev, dict):
            prev = {"at": float(prev or 0), "status": "online", "convId": "", "convName": "",
                    "lastActionAt": now, "lastAction": ""}
        new_status = str(status or prev.get("status") or "online").strip().lower()
        if new_status not in ("online", "away"):
            new_status = "online"
        new_cid = str(conv_id if conv_id is not None else prev.get("convId") or "").strip()
        new_cname = str(conv_name if conv_name is not None else prev.get("convName") or "").strip()
        last_action_at = float(prev.get("lastActionAt") or now)
        last_action = str(prev.get("lastAction") or "")
        if action or new_cid != prev.get("convId") or new_status != prev.get("status"):
            last_action_at = now
            if action:
                last_action = str(action).strip()
        _presence[staff] = {
            "at": now,
            "status": new_status,
            "convId": new_cid,
            "convName": new_cname,
            "lastActionAt": last_action_at,
            "lastAction": last_action,
        }
    return True


def _presence_entry(raw, now=None, timeout_sec=45):
    now = now or time.time()
    if isinstance(raw, dict):
        at = float(raw.get("at") or 0)
        status = str(raw.get("status") or "online").strip().lower()
        cid = str(raw.get("convId") or "").strip()
        cname = str(raw.get("convName") or "").strip()
        last_act = float(raw.get("lastActionAt") or at or 0)
        act_txt = str(raw.get("lastAction") or "")
    else:
        at = float(raw or 0)
        status = "online"
        cid = ""
        cname = ""
        last_act = at
        act_txt = ""
    is_live = at > 0 and (now - at < timeout_sec)
    idle_sec = int(max(0, now - last_act)) if last_act > 0 else 0
    if not is_live:
        state = "offline"
    elif status == "away":
        state = "away"
    elif idle_sec >= 180:
        state = "idle"
    else:
        state = "online"
    return {
        "online": is_live,
        "state": state,
        "status": status if is_live else "offline",
        "convId": cid if is_live else "",
        "convName": cname if is_live else "",
        "lastSeenAt": int(at * 1000) if at else 0,
        "idleSec": idle_sec,
        "lastAction": act_txt,
    }


def online_staff(timeout_sec=45):
    now = time.time()
    with _presence_lock:
        out = []
        for s, raw in _presence.items():
            at = float(raw.get("at") or 0) if isinstance(raw, dict) else float(raw or 0)
            if now - at < timeout_sec:
                out.append(s)
        return sorted(out)


def presence_details(timeout_sec=45):
    now = time.time()
    with _presence_lock:
        return {s: _presence_entry(raw, now=now, timeout_sec=timeout_sec) for s, raw in _presence.items()}


def supervisor_overview(profiles=None, accounts=None, timeout_sec=45):
    """Tổng hợp trạng thái real-time của tất cả Sale/Trưởng phòng để Giám đốc giám sát & vào nhắn thay."""
    now = time.time()
    lt = time.localtime(now)
    day_start_sec = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    day_start_ms = int(day_start_sec * 1000)

    acc_map = {}
    acc_list = []
    for a in (accounts or []):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if not aid:
            continue
        item = {
            "id": aid,
            "name": str(a.get("name") or aid),
            "avatar": str(a.get("avatar") or ""),
            "ready": bool(a.get("ready")),
            "running": bool(a.get("running")),
        }
        acc_map[aid] = item
        acc_list.append(item)

    sent_today = {}
    sent_total = {}
    msgs_by_staff = {}
    all_sent_events = []
    claims_by_staff = {}
    conv_names = {}
    total_unread = 0
    with _lock:
        db = _load_inbox()
        convs = (db.get("conversations") or {}).values()
        for c in convs:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("convId") or c.get("id") or "")
            cname = str(c.get("name") or cid)
            aid = str(c.get("accountId") or "")
            acc_name = (acc_map.get(aid) or {}).get("name") or aid
            if cid:
                conv_names[cid] = cname
            msgs = c.get("messages") or []
            if msgs:
                last_m = msgs[-1] if isinstance(msgs[-1], dict) else {}
                lr = c.get("lastRead") if isinstance(c.get("lastRead"), dict) else None
                reads_vals = [lr] if lr else []
                if isinstance(c.get("reads"), dict):
                    reads_vals.extend([v for v in c["reads"].values() if isinstance(v, dict)])
                best_idx = -1
                max_read_at = 0
                for rv in reads_vals:
                    rat = int(rv.get("at") or 0)
                    if rat > max_read_at:
                        max_read_at = rat
                    rmid = str(rv.get("msgId") or "")
                    if rmid:
                        for idx in range(len(msgs) - 1, -1, -1):
                            if str((msgs[idx] or {}).get("msgId") or "") == rmid:
                                if idx > best_idx:
                                    best_idx = idx
                                break
                if best_idx >= 0:
                    total_unread += max(0, len(msgs) - 1 - best_idx)
                elif max_read_at > 0 and max_read_at >= int(c.get("lastTs") or 0):
                    pass
                elif str(last_m.get("from") or "") in ("staff", "me"):
                    pass
                else:
                    total_unread += len(msgs)
            cl = c.get("claim")
            if isinstance(cl, dict) and not _claim_expired(cl):
                owner = str(cl.get("staff") or "").strip()
                if owner:
                    claims_by_staff.setdefault(owner.lower(), []).append({
                        "convId": cid,
                        "name": cname,
                        "accountId": aid,
                        "accountName": acc_name,
                        "since": int(cl.get("since") or 0) * 1000,
                    })
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if str(m.get("from") or "") == "staff":
                    stf = str(m.get("staff") or "").strip()
                    if stf:
                        low_s = stf.lower()
                        sent_total[low_s] = sent_total.get(low_s, 0) + 1
                        ts_m = int(m.get("ts") or 0)
                        if ts_m >= day_start_ms:
                            sent_today[low_s] = sent_today.get(low_s, 0) + 1
                        ev_m = {
                            "ts": ts_m,
                            "staff": stf,
                            "action": "send",
                            "convId": cid,
                            "convName": cname,
                            "accountId": aid,
                            "accountName": acc_name,
                            "detail": str(m.get("text") or ("[File/Media]" if m.get("mediaUrl") else "")),
                            "msgId": str(m.get("msgId") or ""),
                        }
                        msgs_by_staff.setdefault(low_s, []).append(ev_m)
                        all_sent_events.append(ev_m)

    with _presence_lock:
        pres_snap = {s: _presence_entry(raw, now=now, timeout_sec=timeout_sec) for s, raw in _presence.items()}

    # Build ordered roster from Gateway profiles + any live non-admin staff
    roster = []
    seen_low = set()
    for p in (profiles or []):
        if not isinstance(p, dict):
            continue
        nm = str(p.get("name") or "").strip()
        if not nm or nm.lower() in seen_low:
            continue
        seen_low.add(nm.lower())
        roster.append({
            "name": nm,
            "role": "manager" if str(p.get("role") or "").lower() == "manager" else "sale",
            "accountIds": [str(x).strip() for x in (p.get("accountIds") or []) if str(x).strip()],
        })
    for s in sorted(pres_snap.keys()):
        if s.lower() in ("admin", "super admin") or s.lower() in seen_low:
            continue
        seen_low.add(s.lower())
        roster.append({"name": s, "role": staff_role(s), "accountIds": []})

    users_db = users_list()
    staff_rows = []
    online_cnt = away_cnt = offline_cnt = locked_cnt = banned_cnt = sales_cnt = total_claims = total_sent = 0
    for item in roster:
        nm = item["name"]
        low = nm.lower()
        role = item["role"]
        u_rec = users_db.get(nm) or next((v for k, v in users_db.items() if str(k).lower() == low and isinstance(v, dict)), {}) or {}
        is_locked = bool(u_rec.get("locked"))
        is_banned = bool(u_rec.get("banned"))
        p_info = pres_snap.get(nm) or next((v for k, v in pres_snap.items() if k.lower() == low), None) \
            or _presence_entry(None, now=now, timeout_sec=timeout_sec)
        cur_cid = p_info.get("convId") or ""
        cur_cname = (p_info.get("convName") or conv_names.get(cur_cid) or "") if cur_cid else ""
        assigned_accs = []
        for aid in item["accountIds"]:
            assigned_accs.append({
                "id": aid,
                "name": (acc_map.get(aid) or {}).get("name") or aid,
                "avatar": (acc_map.get(aid) or {}).get("avatar") or "",
            })
        my_claims = claims_by_staff.get(low, [])
        my_sent = sent_today.get(low, 0)
        my_sent_total = sent_total.get(low, 0)
        my_msgs = sorted(msgs_by_staff.get(low, []), key=lambda x: int(x.get("ts") or 0), reverse=True)[:80]
        state = "banned" if is_banned else ("locked" if is_locked else p_info["state"])
        if role != "manager":
            sales_cnt += 1
            if is_banned:
                banned_cnt += 1
            elif is_locked:
                locked_cnt += 1
            elif state == "away":
                away_cnt += 1
            elif state in ("online", "idle"):
                online_cnt += 1
            else:
                offline_cnt += 1
        total_claims += len(my_claims)
        total_sent += my_sent
        staff_rows.append({
            "name": nm,
            "role": role,
            "locked": is_locked,
            "banned": is_banned,
            "accountIds": item["accountIds"],
            "accounts": assigned_accs,
            "state": state,
            "online": p_info["online"] and not is_locked and not is_banned,
            "convId": cur_cid,
            "convName": cur_cname,
            "lastSeenAt": p_info["lastSeenAt"],
            "idleSec": p_info["idleSec"],
            "claims": my_claims,
            "claimCount": len(my_claims),
            "sentToday": my_sent,
            "sentTotal": my_sent_total,
            "messages": my_msgs,
        })

    recent_audit = []
    for ev in list_audit(limit=120):
        e = dict(ev)
        cid = str(e.get("convId") or "")
        e["convName"] = conv_names.get(cid) or cid
        recent_audit.append(e)

    # Combine sent messages + non-duplicate audit actions into full chronological log
    seen_send_keys = {(int(x.get("ts") or 0) // 1000, str(x.get("staff") or "").lower(), str(x.get("convId") or "")) for x in all_sent_events}
    combined_log = list(all_sent_events)
    for ev in recent_audit:
        act = str(ev.get("action") or "")
        if act == "send":
            k = (int(ev.get("ts") or 0) // 1000, str(ev.get("staff") or "").lower(), str(ev.get("convId") or ""))
            if k in seen_send_keys:
                continue
        combined_log.append(ev)
    combined_log.sort(key=lambda x: int(x.get("ts") or 0), reverse=True)

    return {
        "staffList": staff_rows,
        "accounts": acc_list,
        "summary": {
            "totalSales": sales_cnt,
            "onlineCount": online_cnt,
            "awayCount": away_cnt,
            "offlineCount": offline_cnt,
            "lockedCount": locked_cnt,
            "bannedCount": banned_cnt,
            "totalClaims": total_claims,
            "totalSentToday": total_sent,
            "totalUnread": total_unread,
        },
        "recentActivity": recent_audit[:40],
        "allActivity": combined_log[:200],
        "ts": int(now * 1000),
    }


def staff_moderation_status(staff):
    nm = str(staff or "").strip()
    if not nm or nm.lower() == "super admin":
        return {"locked": False, "banned": False}
    users = _read_json(USERS_PATH, {})
    if not isinstance(users, dict):
        return {"locked": False, "banned": False}
    rec = users.get(nm) or next((v for k, v in users.items() if str(k).lower() == nm.lower() and isinstance(v, dict)), None)
    if not isinstance(rec, dict):
        return {"locked": False, "banned": False}
    return {"locked": bool(rec.get("locked")), "banned": bool(rec.get("banned"))}


def release_all_claims(staff, actor=""):
    target = str(staff or "").strip().lower()
    if not target:
        return 0
    released = 0
    with _lock:
        db = _load_inbox()
        convs = db.get("conversations") or {}
        for c in convs.values():
            if not isinstance(c, dict):
                continue
            cl = c.get("claim")
            if isinstance(cl, dict) and str(cl.get("staff") or "").strip().lower() == target:
                c["claim"] = None
                released += 1
        if released:
            _save_inbox(db)
            audit("release_all_claims", actor or staff, "", f"thu hồi {released} khách của {staff}")
    return released


def moderate_staff(target, action, actor=""):
    nm = str(target or "").strip()
    act = str(action or "").strip().lower()
    if not nm:
        return {"ok": False, "error": "Thiếu tên nhân sự."}
    ensure_staff(nm)
    released = 0
    with _lock:
        users = _read_json(USERS_PATH, {})
        if not isinstance(users, dict):
            users = {}
        key = next((k for k in users.keys() if str(k).lower() == nm.lower()), nm)
        rec = users.get(key) if isinstance(users.get(key), dict) else {"name": nm, "role": staff_role(nm), "createdAt": int(time.time())}
        if act == "lock":
            rec["locked"] = True
            with _presence_lock:
                for pk in list(_presence.keys()):
                    if str(pk).lower() == nm.lower():
                        _presence.pop(pk, None)
            audit("lock_staff", actor, "", f"tạm khóa {nm}")
        elif act == "unlock":
            rec["locked"] = False
            audit("unlock_staff", actor, "", f"mở khóa {nm}")
        elif act == "ban":
            rec["banned"] = True
            rec["locked"] = True
            with _presence_lock:
                for pk in list(_presence.keys()):
                    if str(pk).lower() == nm.lower():
                        _presence.pop(pk, None)
            released = release_all_claims(nm, actor=actor)
            audit("ban_staff", actor, "", f"cấm truy cập {nm}")
        elif act == "unban":
            rec["banned"] = False
            rec["locked"] = False
            audit("unban_staff", actor, "", f"bỏ cấm {nm}")
        elif act == "release_claims":
            released = release_all_claims(nm, actor=actor)
        else:
            return {"ok": False, "error": "Thao tác không hợp lệ."}
        users[key] = rec
        _write_json(USERS_PATH, users)
    return {"ok": True, "target": nm, "action": act, "releasedClaims": released,
            "locked": bool(rec.get("locked")), "banned": bool(rec.get("banned"))}


def users_list():
    users = _read_json(USERS_PATH, {})
    if not isinstance(users, dict):
        users = {}
    return users


def get_config():
    cfg = _read_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("accountId", "")
    cfg.setdefault("onlyGroupIds", [])
    cfg.setdefault("onlyFriendIds", [])
    cfg.setdefault("hideEmpty", False)
    cfg.setdefault("updatedAt", 0)
    return cfg


def save_config(patch):
    cfg = get_config()
    if not isinstance(patch, dict):
        patch = {}
    if "accountId" in patch:
        cfg["accountId"] = str(patch.get("accountId") or "").strip()
    for k in ("onlyGroupIds", "onlyFriendIds"):
        if k in patch:
            v = patch.get(k)
            if isinstance(v, str):
                v = [x.strip() for x in v.split(",")]
            cfg[k] = [str(x).strip() for x in (v or []) if str(x).strip()]
    if "hideEmpty" in patch:
        cfg["hideEmpty"] = bool(patch.get("hideEmpty"))
    cfg["updatedAt"] = _now_ms()
    _write_json(CONFIG_PATH, cfg)
    return cfg
