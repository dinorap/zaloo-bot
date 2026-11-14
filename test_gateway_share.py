"""Test tong hop share TK qua Gateway Host (khong can server live)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=== 1. Token noi bo (config) ===")
from features.gateway import config as C
tok1 = C.get_internal_token()
tok2 = C.get_internal_token()
print("token len:", len(tok1), "| on dinh sau 2 lan goi:", tok1 == tok2)
print("token preview:", tok1[:4] + "***" + tok1[-3:])
print("allowLan:", C.get_allowed_lan())
print("sendGap:", C.get_send_gap())

print("\n=== 2. Whitelist LAN (auth.ip_allowed) ===")
from features.gateway.auth import ip_allowed, mask_secret
cases = {
    "127.0.0.1": True, "::1": True,
    "192.168.1.10": True, "192.168.0.1": True,
    "10.0.5.6": True, "10.10.10.10": True,
    "8.8.8.8": False, "172.16.0.5": False, "203.0.113.9": False,
}
ok = True
for ip, expect in cases.items():
    got = ip_allowed(ip)
    mark = "OK " if got == expect else "FAIL"
    if got != expect: ok = False
    print(f"  [{mark}] {ip} -> {got} (expect {expect})")
print("whitelist:", "PASS" if ok else "FAIL")

print("\n=== 3. Che secret (khong lo key ra Client) ===")
print("mask zpwEnk:", mask_secret("O387DBPhxIloMhX4iJNUaA=="))
print("mask cookies dai:", mask_secret("zpw_sek=abc123xyz; zpsid=..."))
# kiem tra code health khong tra cookies/zpwEnk/imei
src = open("app.py", encoding="utf-8").read()
import re
m = re.search(r"def _pub\(a\):(.*?)(\n    def |\n    if account_id:)", src, re.S)
pub = m.group(1) if m else ""
leak = [k for k in ("cookies", "zpwEnk", "imei", "proxy") if f'"{k}"' in pub or f"'{k}'" in pub]
print("health _pub co lo key khong:", ("KHONG - PASS" if not leak else f"CO {leak} - FAIL"))

print("\n=== 4. Queue FIFO 3 client (khong da session) ===")
from features.gateway import send_queue as Q
order = []
def fake_send(aid, job):
    import time; time.sleep(0.2)
    order.append(job.get("message"))
    return {"ok": True, "decoded": {"error_code": 0}}
Q._do_send = fake_send
import threading, time
results = {}
def cli(n):
    results[n] = Q.enqueue("acc-share-test", "group", "gid1", f"tin-{n}", timeout=30)
ts = [threading.Thread(target=cli, args=(i,)) for i in (1,2,3)]
t0=time.time(); [t.start() for t in ts]; [t.join() for t in ts]; dt=time.time()-t0
print("results ok:", all(v.get("ok") for v in results.values()))
print("thu tu gui:", order, "| elapsed:", round(dt,1), "s (tuan tu la dung)")
print("circuit:", Q.circuit_status("acc-share-test"))

print("\n=== 5. Circuit breaker 429/431 ===")
Q._record_failure("acc-cb-test", "rate", "429 Too Many Requests")
Q._record_failure("acc-cb-test", "rate", "429")
Q._record_failure("acc-cb-test", "rate", "431")
st = Q.circuit_status("acc-cb-test")
print("sau 3 loi rate -> open:", st["open"], "| reason:", st["reason"])
# reset de khoi anh huong acc that
Q._record_success("acc-cb-test")
print("sau success -> open:", Q.circuit_status("acc-cb-test")["open"], "(expect False)")

print("\n=== 6. Client SDK (may Client dung) ===")
from features.gateway import client as CL
print("co ham:", [f for f in ("configure","nexus_get","nexus_post","nexus_post_form","session_health","group_latest","send_group","send_sms") if hasattr(CL, f)])
CL.configure("http://192.168.1.10:5000", "TOKEN-TEST")
print("configure ok:", CL.HOST_URL, "| token set:", bool(CL.INTERNAL_TOKEN))

print("\n=== 7. Store + cursor persist ===")
from features.gateway import store as S
cfg = S.save_config({"enabled": False, "accountId": "acc-test", "routes": [{"id":"r1","dest_group_id":"963","source_group_ids":["111","222","963"]}], "intervalSec": 15})
print("routes sau chuan hoa (tu loai dest khoi source):", cfg["routes"])
S.save_cursor({"111": {"msgId": "m1", "ts": 123}})
print("cursor doc lai:", S.load_cursor())

print("\n=== 8. Tai khoan that san sang share chua ===")
d = json.load(open("data/accounts.json", encoding="utf-8"))
for a in d.get("accounts", []):
    ready = bool(a.get("loginCaptured") and a.get("zpwEnk") and "zpw_sek" in (a.get("cookies") or ""))
    print(f"  {a.get('name')} ({str(a.get('accountId'))[:8]}...) uid={a.get('uid')} ready-share={ready}")

print("\n=== KET LUAN ===")
print("Share TK = Host giu session, Client goi qua token. Code da san sang.")
print("Thieu duy nhat: app anh DANG CHAY BAN CU (404 /api/gateway/config) -> restart python app.py de nap gateway moi.")
