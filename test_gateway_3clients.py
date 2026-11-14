"""Test 3 client gui dong thoi qua Gateway Host: FIFO per-account, khong da session."""
import threading, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features.gateway import send_queue as Q

order = []
lock = threading.Lock()

def fake_do_send(account_id, job):
    time.sleep(0.3)  # gia lap Zalo API
    with lock:
        order.append((job.get("message"), threading.current_thread().name))
    return {"ok": True, "decoded": {"error_code": 0}}

Q._do_send = fake_do_send

results = {}
def client(name, msg):
    out = Q.enqueue("acc-test-1", "group", "gid-1", msg, timeout=30)
    results[name] = out

ts = [threading.Thread(target=client, args=(f"C{i}", f"msg-{i}"), name=f"C{i}") for i in range(1, 4)]
t0 = time.time()
[t.start() for t in ts]
[t.join() for t in ts]
dt = time.time() - t0
print("RESULTS:", results)
print("ORDER:", order)
print(f"ELAPSED: {dt:.1f}s (expect >= ~1.9s neu FIFO + gap 1.2s, 3 tin tuan tu)")
assert all(v.get("ok") for v in results.values()), "co client that bai"
assert len(order) == 3, "thieu tin"
# FIFO: thu tu hoan thanh phai tuan tu, khong chen ngang (elapsed chung minh serialize)
assert dt >= 1.5, f"gui song song khong qua queue? elapsed={dt:.1f}s"
print("PASS: 3 client tuan tu qua 1 queue, khong mo 3 session song song -> khong da session.")
print("CIRCUIT:", Q.circuit_status("acc-test-1"))
print("DEPTHS:", Q.queue_depths())
