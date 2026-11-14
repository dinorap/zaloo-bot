"""Inbox sync worker: poll Zalo get-last-msgs -> store -> broadcast.
Dong bo ten + avatar that tu personalGroups + friend list (Nexus directory).
Tao vo hoi thoai cho TAT CA nhom + ban be de Inbox hien nhu Zalo that.
"""
import threading
import time

_lock = threading.RLock()
_thread = None
_stop = threading.Event()
_last_sync = {}
_dir_cache_by_acc: dict = {}
_DIR_TTL = 300.0  # cache ten/avatar nhom 5 phut (nhom doi ten hiem)

# Danh ba ban be (API getfriends) rat de bi Zalo 429 -> cache rieng 30 phut
# + backoff 10 phut khi vua dinh 429. Khong goi lien tuc nhu truoc.
_FRIEND_TTL = 1800.0
_friend_cache_by_acc: dict = {}
_friend_429_until_by_acc: dict = {}
_FRIEND_BACKOFF_SEC = 600.0

# Cache profile nguoi la/thanh vien nhom (getprofiles/v2) + backoff khi bi gioi han
_PROFILE_TTL = 1800.0
_profile_cache_by_acc: dict = {}
_profile_429_until_by_acc: dict = {}
_PROFILE_BACKOFF_SEC = 600.0


def _short_err(e, n=200) -> str:
    """Cat ngan log loi (URL zalo ma hoa rat dai, chi giu 200 ky tu dau)."""
    try:
        s = str(e or "")
    except Exception:
        s = "error"
    # URL params ma hoa co the dai vai KB -> cat bot de khoi spam terminal
    if len(s) > n:
        s = s[:n] + "..."
    # 1 dong duy nhat
    return " ".join(s.split())


def _broadcast(conv_id, kind, message=None, conv=None, extra=None):
    try:
        import json as _json
        import sys as _sys
        _main_mod = _sys.modules.get("__main__")
        _b = getattr(_main_mod, "inbox_broadcast", None)
        if not callable(_b):
            from app import inbox_broadcast as _b
        payload = {"convId": conv_id, "kind": kind}
        if message is not None:
            payload["message"] = message
        if conv is not None:
            payload["conv"] = conv
        if extra:
            payload.update(extra)
        _b(_json.dumps(payload, ensure_ascii=False), "inbox")
    except Exception:
        pass


def _pick_account():
    # Ưu tiên 1: tài khoản đã chọn trong Inbox setup (inbox_config.json).
    # Ưu tiên 2: acc đầu tiên trong danh sách share của Gateway (nếu Host đã tick).
    # Ưu tiên 3: tài khoản gateway đang chạy. Ưu tiên 4: acc loginCaptured.
    try:
        from features.inbox import store as _st
        cfg = _st.get_config()
        aid = str(cfg.get("accountId") or "").strip()
        if aid:
            return aid
    except Exception:
        pass
    try:
        from features.gateway import config as _gc
        shared = _gc.get_shared_account_ids() or []
        if shared:
            return str(shared[0])
    except Exception:
        pass
    try:
        from features.gateway import store as _gs
        cfg = _gs.load_config()
        aid = str(cfg.get("accountId") or "").strip()
        if aid:
            return aid
    except Exception:
        pass
    try:
        from features.accounts.account_manager import load_accounts
        for a in load_accounts() or []:
            _cks = str(a.get("cookies") or "")
            _enk = str(a.get("zpwEnk") or "").strip()
            if a.get("loginCaptured") and _enk and "zpw_sek" in _cks:
                return str(a.get("accountId") or "")
        accs = load_accounts() or []
        if accs:
            return str(accs[0].get("accountId") or "")
    except Exception:
        pass
    return ""


def _pick_accounts() -> list:
    """Trả về tất cả accountId cần đồng bộ (các acc được share / giao cho Sale / acc sẵn sàng)."""
    ordered = []
    seen = set()

    def _add(aid):
        s = str(aid or "").strip()
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)

    try:
        from features.gateway import config as _gc
        for aid in (_gc.get_shared_account_ids() or []):
            _add(aid)
        for prof in (_gc.get_staff_profiles() or []):
            for aid in (prof.get("accountIds") or []):
                _add(aid)
    except Exception:
        pass
    try:
        from features.inbox import store as _st
        _add(_st.get_config().get("accountId"))
    except Exception:
        pass
    try:
        from features.accounts.account_manager import load_accounts
        all_accs = load_accounts() or []
        ready_map = {}
        for a in all_accs:
            aid = str(a.get("accountId") or "").strip()
            if not aid:
                continue
            _cks = str(a.get("cookies") or "")
            _enk = str(a.get("zpwEnk") or "").strip()
            if a.get("loginCaptured") and _enk and "zpw_sek" in _cks:
                ready_map[aid] = True
                if not ordered:
                    _add(aid)
        # Lọc các acc đã cấu hình theo acc tồn tại & sẵn sàng (nếu có acc ready),
        # đồng thời thêm mọi acc ready khi Host chưa giới hạn riêng hoặc khi Giám đốc cần xem toàn bộ.
        for aid in ready_map:
            _add(aid)
        if ordered:
            valid = [aid for aid in ordered if any(str(a.get("accountId") or "").strip() == aid for a in all_accs)]
            if valid:
                return valid
    except Exception:
        pass
    if ordered:
        return ordered
    one = _pick_account()
    return [one] if one else []


def _normalize_groups(acc):
    groups = acc.get("personalGroups") or []
    if isinstance(groups, dict):
        groups = list(groups.values())
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("groupId") or g.get("gridId") or g.get("id") or "").strip()
        if not gid:
            continue
        out.append({
            "groupId": gid,
            "name": str(g.get("name") or g.get("grid_name") or g.get("groupName") or ("Nhom " + gid[:8])).strip(),
            "avatar": str(g.get("avatar") or g.get("fullAvt") or g.get("avt") or g.get("grid_avatar") or "").strip(),
            "memberCount": int(g.get("memberCount") or g.get("grid_totalMember") or g.get("totalMember") or 0),
        })
    return out


def _load_friends_safe(acc, force=False):
    """Lay ban be that, cache rieng 30 phut + backoff 10 phut khi dinh 429.

    API getfriends cua Zalo gioi han rat gat — goi lien tuc la an 429 ngay.
    """
    aid = str((acc or {}).get("accountId") or "").strip()
    now = time.time()
    cached = _friend_cache_by_acc.get(aid) if aid else None
    # Vua dinh 429 -> nghi 10 phut, dung cache cu, khong goi lai
    if aid and now < float(_friend_429_until_by_acc.get(aid) or 0):
        if cached:
            return [dict(f) for f in (cached.get("friends") or [])]
        return None
    # Cache con tuoi 30 phut -> dung luon, khong goi API
    if not force and cached and (now - float(cached.get("at") or 0) < _FRIEND_TTL):
        return [dict(f) for f in (cached.get("friends") or [])]
    try:
        from features.friends.friend_service import get_friend_list
        friends = get_friend_list(
            acc.get("zpwEnk", ""), acc.get("cookies", ""), imei=acc.get("imei", ""))
        out = []
        for f in friends or []:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("userId") or f.get("uid") or "").strip()
            if not uid:
                continue
            out.append({
                "userId": uid,
                "name": str(f.get("zaloName") or f.get("displayName") or uid).strip(),
                "avatar": str(f.get("avatar") or "").strip(),
            })
        if aid:
            _friend_cache_by_acc[aid] = {"at": now, "friends": [dict(f) for f in out]}
            _friend_429_until_by_acc.pop(aid, None)
        return out
    except Exception as e:
        msg = _short_err(e)
        # Dinh 429 -> backoff 10 phut de Zalo ha nhiet
        if "429" in msg:
            if aid:
                _friend_429_until_by_acc[aid] = now + _FRIEND_BACKOFF_SEC
            print(f"[inbox-sync] friend list 429 -> nghi 10 phut, dung cache cu ({len((cached or {}).get('friends') or [])} ban)", flush=True)
        else:
            print(f"[inbox-sync] friend list loi (dung cache): {msg}", flush=True)
        if cached:
            return [dict(f) for f in (cached.get("friends") or [])]
        return None


def _sync_directory(account_id, force=False, force_friends=False):
    """Nap ten/avatar that vao inbox_directory.json + tao vo hoi thoai. Tra ve (groups, friends)."""
    aid = str(account_id or "").strip()
    now = time.time()
    cached = _dir_cache_by_acc.get(aid) or {}
    if not force and cached and (now - float(cached.get("at") or 0) < _DIR_TTL):
        return cached.get("groups") or [], cached.get("friends") or []
    try:
        from features.accounts.account_manager import get_account
        from features.inbox import store as _st
        acc = get_account(aid)
        if not acc:
            return [], []
        groups = _normalize_groups(acc)
        # Ban be co TTL + backoff rieng 30p/10p — auto loop KHONG force de tranh 429.
        friends = _load_friends_safe(acc, force=bool(force_friends))
        if friends is None:
            # giu cache cu neu Zalo rate-limit
            old = _st.get_directory(aid)
            friends = [{"userId": f.get("userId"), "name": f.get("name"),
                        "avatar": f.get("avatar")} for f in (old.get("friends") or [])]
        slim_groups = [{"groupId": g["groupId"], "name": g["name"],
                        "avatar": g["avatar"], "memberCount": g["memberCount"]} for g in groups]
        slim_friends = [{"userId": f["userId"], "name": f["name"],
                         "avatar": f["avatar"]} for f in friends]
        _st.rebuild_directory(aid, slim_groups, slim_friends)
        created, updated = _st.ensure_conversations(aid, slim_groups, slim_friends)
        if created or updated:
            print(f"[inbox-sync] directory ({aid[:8]}): {len(slim_groups)} nhom + {len(slim_friends)} ban "
                  f"(new={created} upd={updated})", flush=True)
        _dir_cache_by_acc[aid] = {"at": now, "accountId": aid,
                                  "groups": slim_groups, "friends": slim_friends}
        try:
            _broadcast("", "directory",
                       extra={"accountId": aid, "totalGroups": len(slim_groups), "totalFriends": len(slim_friends)})
        except Exception:
            pass
        return slim_groups, slim_friends
    except Exception as e:
        print(f"[inbox-sync] directory loi ({aid[:8]}): {e}", flush=True)
        return [], []


def _resolve_uids_profile_map(account_id, uids, force=False):
    """Lấy {uid: (name, avatar)} từ cache hoặc getprofiles/v2 cho các UID chưa có tên/avatar."""
    aid = str(account_id or "").strip()
    if not aid or not uids:
        return {}
    now = time.time()
    acc_cache = _profile_cache_by_acc.setdefault(aid, {})
    result = {}
    to_fetch = []
    seen = set()
    for raw in uids:
        uid = str(raw or "").strip()
        if not uid or uid in ("0", "") or uid in seen:
            continue
        seen.add(uid)
        cached = acc_cache.get(uid)
        if not force and cached and (now - float(cached.get("at") or 0) < _PROFILE_TTL):
            nm, av = cached.get("name", ""), cached.get("avatar", "")
            if nm or av:
                result[uid] = (nm, av)
            continue
        to_fetch.append(uid)

    if not to_fetch:
        return result

    backoff_until = float(_profile_429_until_by_acc.get(aid) or 0)
    if backoff_until > now:
        for uid in to_fetch:
            cached = acc_cache.get(uid)
            if cached and (cached.get("name") or cached.get("avatar")):
                result[uid] = (cached.get("name", ""), cached.get("avatar", ""))
        return result

    try:
        from app import _get_account_credentials
        from core.zalo.zalo_config import get_zpw_ver
        from features.profiles.get_single_profile import ProfileRateLimitError, get_single_profile

        _, cookies, zpw_enk, imei = _get_account_credentials(aid, require_imei=False)
        profiles = get_single_profile(
            to_fetch[:30],
            zpw_enk,
            cookies,
            imei=imei,
            zpw_ver=get_zpw_ver(),
            raise_on_rate_limit=True,
        ) or {}
        _profile_429_until_by_acc.pop(aid, None)
        for uid in to_fetch:
            prof = profiles.get(uid)
            if isinstance(prof, dict):
                nm = str(prof.get("displayName") or prof.get("zaloName") or prof.get("dName") or "").strip()
                av = str(prof.get("avatar") or prof.get("avt") or "").strip()
                acc_cache[uid] = {"name": nm, "avatar": av, "at": now}
                if nm or av:
                    result[uid] = (nm, av)
            else:
                # Cache trống ngắn hạn (nhớ thời điểm) tránh gọi lại ngay nếu UID không tồn tại
                acc_cache[uid] = {"name": "", "avatar": "", "at": now - (_PROFILE_TTL - 300.0)}
    except ProfileRateLimitError as e:
        _profile_429_until_by_acc[aid] = now + _PROFILE_BACKOFF_SEC
        print(f"[inbox-sync] getprofiles bi gioi han ({_short_err(e)}) -> tam nghi 10p", flush=True)
    except Exception as e:
        print(f"[inbox-sync] getprofiles loi: {_short_err(e)}", flush=True)
    return result


def _resolve_missing_profiles(account_id, acc=None, force=False):
    """Tự bù tên/avatar thật cho các hội thoại & tin nhắn cũ còn hiện UID số hoặc thiếu avatar."""
    from features.inbox import store as _st
    aid = str(account_id or "").strip()
    if not aid:
        return 0
    unresolved_uids, unresolved_gids = _st.list_unresolved_targets(aid)
    if not unresolved_uids and not unresolved_gids:
        return 0

    uid_profile_map = {}
    # Ưu tiên lấy từ directory friends trước nếu đã có
    d = _st.get_directory(aid) or {}
    dir_friends = {str(f.get("userId") or ""): (str(f.get("name") or ""), str(f.get("avatar") or ""))
                   for f in (d.get("friends") or []) if isinstance(f, dict)}
    remain_uids = []
    for uid in unresolved_uids:
        nm, av = dir_friends.get(uid, ("", ""))
        if nm and not _st._is_placeholder_name(nm, uid) and av:
            uid_profile_map[uid] = (nm, av)
        else:
            remain_uids.append(uid)

    if remain_uids:
        fetched = _resolve_uids_profile_map(aid, remain_uids, force=force)
        for uid, pair in fetched.items():
            uid_profile_map[uid] = pair

    gid_group_map = {}
    if unresolved_gids:
        try:
            if acc is None:
                from features.accounts.account_manager import get_account
                acc = get_account(aid)
            if acc:
                for g in _normalize_groups(acc):
                    gid = g["groupId"]
                    if gid in unresolved_gids:
                        gid_group_map[gid] = (g["name"], g["avatar"], g["memberCount"])
        except Exception:
            pass

    changed_cids = _st.patch_profiles(aid, uid_profile_map=uid_profile_map, gid_group_map=gid_group_map)
    if changed_cids:
        print(f"[inbox-sync] da cap nhat ten/avatar cho {len(changed_cids)} hoi thoai", flush=True)
        _broadcast("", "directory", extra={"resolvedCount": len(changed_cids)})
    return len(changed_cids)


def _sync_one_account(account_id, loop_n=1):
    from features.inbox import store as _st
    import sys
    sys.path.insert(0, ".")
    from app import _get_account_credentials
    from core.zalo.zalo_config import get_zpw_ver
    from features.messaging.last_messages import fetch_last_messages
    from features.accounts.account_manager import get_account

    acc = get_account(account_id)
    if not acc:
        return False
    try:
        _, cookies, zpw_enk, imei = _get_account_credentials(account_id, require_imei=False)
    except Exception as e:
        if loop_n == 1:
            print(f"[inbox-sync] session chua san sang ({str(account_id)[:8]}): {e}", flush=True)
        return False
    acc_name = str(acc.get("name") or "Nhan vien").strip()
    own_uid = str(acc.get("uid") or acc.get("userId") or acc.get("user_id") or "").strip()

    if loop_n == 1:
        try:
            _old0 = _st.get_directory(account_id) or {}
            _need_f = not (_old0.get("friends") or [])
        except Exception:
            _need_f = True
        groups, friends = _sync_directory(account_id, force=True, force_friends=_need_f)
        _resolve_missing_profiles(account_id, acc=acc, force=False)
    elif loop_n % 86 == 0:
        groups, friends = _sync_directory(account_id, force=True, force_friends=False)
    else:
        groups, friends = _sync_directory(account_id, force=False)
    if loop_n > 1 and loop_n % 20 == 0:
        _resolve_missing_profiles(account_id, acc=acc, force=False)

    name_by_gid = {g["groupId"]: g["name"] for g in groups}
    avt_by_gid = {g["groupId"]: g["avatar"] for g in groups}
    friend_map = {f["userId"]: (f["name"], f["avatar"]) for f in friends}
    name_by_uid = {uid: nm for uid, (nm, _av) in friend_map.items()}
    avt_by_uid = {uid: av for uid, (_nm, av) in friend_map.items()}

    thread_map = {}
    for g in groups:
        gid = g["groupId"]
        prev = (_last_sync.get(f"{account_id}:group:{gid}") or "0")
        if prev in ("", None):
            prev = "0"
        thread_map[f"{gid}_1"] = str(prev)
    for f in friends[:100]:
        fid = f["userId"]
        prev = (_last_sync.get(f"{account_id}:sms:{fid}") or "0")
        thread_map[f"{fid}_0"] = str(prev or "0")
    if not thread_map:
        return True
    res = fetch_last_messages(thread_map, cookies, zpw_enk, imei, zpw_ver=get_zpw_ver())
    if not res.get("ok"):
        print(f"[inbox-sync] get-last-msgs loi ({str(account_id)[:8]}): {res.get('message')}", flush=True)
        return False

    group_msgs = res.get("groupMsgs") or []
    user_msgs = res.get("userMsgs") or []

    unknown_uids = []
    for m in group_msgs:
        suid = str(m.get("senderUid") or "").strip()
        if suid and suid != "0" and suid != own_uid and (not name_by_uid.get(suid) or not avt_by_uid.get(suid)):
            unknown_uids.append(suid)
    for m in user_msgs:
        tid = str(m.get("threadId") or "").strip()
        suid = str(m.get("senderUid") or "").strip()
        if tid and (not name_by_uid.get(tid) or not avt_by_uid.get(tid)):
            unknown_uids.append(tid)
        if suid and suid != "0" and suid != own_uid and (not name_by_uid.get(suid) or not avt_by_uid.get(suid)):
            unknown_uids.append(suid)
    if unknown_uids:
        resolved = _resolve_uids_profile_map(account_id, unknown_uids, force=False)
        for ruid, (rnm, rav) in resolved.items():
            if rnm:
                name_by_uid[ruid] = rnm
            if rav:
                avt_by_uid[ruid] = rav

    for m in group_msgs:
        gid = str(m.get("groupId") or "").strip()
        if not gid:
            continue
        _last_sync[f"{account_id}:group:{gid}"] = str(m.get("msgId") or "0")
        suid = str(m.get("senderUid") or "")
        sname = name_by_uid.get(suid, "")
        savt = avt_by_uid.get(suid, "")
        if suid in ("", "0"):
            sname, savt = acc_name, str(acc.get("avatarUrl") or "")
        out = _st.ingest_zalo(account_id, "group", gid, {
            "msgId": m.get("msgId"), "cliMsgId": m.get("cliMsgId"),
            "senderUid": suid,
            "senderName": sname, "senderAvatar": savt,
            "text": m.get("text"),
            "thumb": m.get("thumb"), "href": m.get("href"),
            "msgType": m.get("msgType"), "ts": m.get("ts"),
        }, conv_name=name_by_gid.get(gid, gid),
            conv_avatar=avt_by_gid.get(gid, ""), own_uid=own_uid)
        if out:
            _broadcast(out["convId"], "new_message", out["message"], out["conv"])
    for m in user_msgs:
        tid = str(m.get("threadId") or "").strip()
        if not tid:
            continue
        _last_sync[f"{account_id}:sms:{tid}"] = str(m.get("msgId") or "0")
        suid = str(m.get("senderUid") or "")
        sname = name_by_uid.get(tid, tid)
        savt = avt_by_uid.get(tid, "")
        if suid in ("", "0"):
            sname, savt = acc_name, str(acc.get("avatarUrl") or "")
        else:
            sname = name_by_uid.get(suid, sname)
            savt = avt_by_uid.get(suid, savt)
        out = _st.ingest_zalo(account_id, "sms", tid, {
            "msgId": m.get("msgId"), "cliMsgId": m.get("cliMsgId"),
            "senderUid": suid,
            "senderName": sname, "senderAvatar": savt,
            "text": m.get("text"),
            "thumb": m.get("thumb"), "href": m.get("href"),
            "msgType": m.get("msgType"), "ts": m.get("ts"),
        }, conv_name=name_by_uid.get(tid, tid), conv_avatar=avt_by_uid.get(tid, ""), own_uid=own_uid)
        if out:
            _broadcast(out["convId"], "new_message", out["message"], out["conv"])
    return True


def _loop():
    loop_n = 0
    seen_accs = set()
    while not _stop.is_set():
        try:
            account_ids = _pick_accounts()
            if not account_ids:
                time.sleep(5.0)
                continue
            loop_n += 1
            for aid in account_ids:
                if _stop.is_set():
                    break
                # Tài khoản mới xuất hiện lần đầu được force directory/friends nếu chưa có cache
                acc_loop = 1 if aid not in seen_accs else loop_n
                try:
                    ok = _sync_one_account(aid, loop_n=acc_loop)
                    if ok:
                        seen_accs.add(aid)
                except Exception as e:
                    print(f"[inbox-sync] loi ({str(aid)[:8]}): {e}", flush=True)
        except Exception as e:
            print(f"[inbox-sync] loop loi: {e}", flush=True)
        _stop.wait(3.0)


def ensure_started():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="inbox-sync")
        _thread.start()
        return True


def is_running():
    with _lock:
        return bool(_thread and _thread.is_alive())


def force_sync(account_id=""):
    raw_aid = (account_id or "").strip()
    if raw_aid and raw_aid != "all":
        aids = [raw_aid]
    else:
        aids = _pick_accounts()
    if not aids:
        return {"ok": False, "error": "Chua co account."}
    total_groups = 0
    total_friends = 0
    total_resolved = 0
    for aid in aids:
        # Nut "Lam moi" thu cong -> duoc phep goi lai API ban be + tu dong lay ten/avatar nguoi la
        groups, friends = _sync_directory(aid, force=True, force_friends=True)
        resolved_count = _resolve_missing_profiles(aid, force=True)
        total_groups += len(groups)
        total_friends += len(friends)
        total_resolved += resolved_count
    return {"ok": True, "accountId": aids[0], "accountIds": aids,
            "totalGroups": total_groups, "totalFriends": total_friends,
            "resolvedProfiles": total_resolved}
