"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")

# Cùng cấu hình với health checker của drill -> tự probe cũng không detect nhanh hơn floor
PROBE_INTERVAL_S, PROBE_TIMEOUT_S, PROBE_THRESHOLD = 5.0, 2.0, 3
DETECT_WAIT_S = 90.0
GOLDEN_REQUESTS = 10


def step(n, name, **kw) -> dict:
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG — đây là timeline của postmortem."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    ans = input(f"{msg} (y/N)? ").strip().lower()
    return ans == "y"


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def last_outage(region: str) -> dict | None:
    """Sự kiện kill gần nhất của `region` trong chaos log (mốc t_outage)."""
    kills = [e for e in _jsonl(CHAOS_LOG) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def wait_health_event(region: str, since: float, timeout: float) -> dict | None:
    """Chờ health checker ghi `region -> UNHEALTHY` sau mốc `since`. Đọc lại file mỗi 1s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in _jsonl(HEALTH_LOG):
            if (e.get("event") == "state_change" and e.get("region") == region
                    and e.get("to") == "UNHEALTHY" and e["ts"] >= since):
                return e
        time.sleep(1)
    return None


def self_probe(region: str) -> tuple[bool, int, str]:
    """Không có health checker: tự probe tới khi đủ PROBE_THRESHOLD lần fail LIÊN TIẾP.

    Một lần probe OK = region còn sống -> KHÔNG xác nhận outage (tránh failover vì 1 lần chập chờn).
    """
    fails, reason = 0, ""
    while fails < PROBE_THRESHOLD:
        ok, reason = hc.probe(region, PROBE_TIMEOUT_S)
        if ok:
            return False, 0, reason
        fails += 1
        if fails < PROBE_THRESHOLD:
            time.sleep(PROBE_INTERVAL_S)
    return True, fails, reason


def confirm_outage(region: str, t_outage: float | None, since: float) -> bool:
    """Bước 1. Ưu tiên health checker (nguồn sự thật về t_detect); chỉ tự probe khi
    không có health log. Trả True nếu outage được xác nhận."""
    ev = wait_health_event(region, since, DETECT_WAIT_S) if HEALTH_LOG.exists() else None
    if ev:
        step(1, "xac_nhan_outage", confirmed=True, source="health_log", t_detect=ev["ts"],
             reason=ev.get("reason"), consecutive_fails=ev.get("consecutive_fails"),
             detect_after_outage_s=round(ev["ts"] - t_outage, 2) if t_outage else None)
        return True
    down, fails, reason = self_probe(region)
    step(1, "xac_nhan_outage", confirmed=down, source="self_probe", t_detect=time.time(),
         reason=reason, consecutive_fails=fails)
    return down


def golden_signals(region: str, n: int) -> dict:
    """n request thật vào /v1/infer của region: p95 latency + error rate."""
    lat, errors = [], 0
    # 1 client dùng chung: đo latency của region, không đo chi phí tạo client/SSL context
    with httpx.Client(timeout=3) as c:
        for i in range(n):
            t0 = time.time()
            try:
                r = c.get(f"{URL[region]}/v1/infer", params={"q": f"golden {i}"})
                ok = r.status_code == 200 and r.json().get("region") == region
            except (httpx.HTTPError, ValueError):
                ok = False
            lat.append((time.time() - t0) * 1000)
            errors += not ok
    lat.sort()
    p95 = lat[max(0, -(-95 * n // 100) - 1)]  # nearest-rank: phần tử thứ ceil(0.95*n)
    return {"requests": n, "p95_ms": round(p95, 1), "max_ms": round(lat[-1], 1),
            "error_rate": round(errors / n, 3), "errors": errors}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước §4 "Runbook: Region Chính Down". failover() được gọi đúng 1 lần ở bước 3."""
    t_start = time.time()
    kill = last_outage(primary)
    t_outage = kill["ts"] if kill else None
    summary = {"ok": False, "primary": primary, "target": target, "t_outage": t_outage}

    def finish(**kw) -> dict:
        elapsed = round(time.time() - t_start, 2)
        since_outage = round(time.time() - t_outage, 2) if t_outage else None
        step(7, "post_incident", elapsed_s=elapsed, since_outage_s=since_outage,
             measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                         "--target-rto 300", **kw)
        summary.update(elapsed_s=elapsed, since_outage_s=since_outage, **kw)
        return summary

    # 1. Xác nhận outage — KHÔNG được failover trước khi detect (t_cutover < t_detect)
    if not confirm_outage(primary, t_outage, since=t_outage or t_start):
        return finish(aborted_at="xac_nhan_outage",
                      note=f"region-{primary} van tra loi /readyz -> KHONG failover")

    # 2. Mở incident. t_notified luôn SAU t_outage: operator không thể biết ngay giây đầu.
    t_notified = time.time()
    step(2, "thong_bao_incident", severity="SEV1", t_outage=t_outage,
         t_outage_iso=kill.get("iso") if kill else None, t_notified=t_notified,
         notify_delay_s=round(t_notified - t_outage, 2) if t_outage else None,
         message=f"region-{primary} down -> chuan bi failover sang region-{target}")
    summary.update(t_notified=t_notified)

    # Bán tự động: người vận hành phải bấm confirm (§4 — chống failover 2 chiều liên tục)
    if not confirm(auto, f"Failover {primary} -> {target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator_declined")
        return finish(aborted_at="confirm")

    # 3. Gọi failover() ĐÚNG MỘT LẦN — nó tự làm verify/restore/scale/wait/cutover
    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", confirmed_by="auto" if auto else "operator",
         ok=res.get("ok"), failover_elapsed_s=res.get("elapsed_s"),
         aborted_at=res.get("aborted_at"))
    summary["failover"] = res

    # 4. Chỉ ĐỌC kết quả bước 3 — không gọi lại failover
    step(4, "verify_state_replica", vectors=res.get("vectors"), weights=res.get("weights"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))

    # 5. Cutover có thành công không — cũng chỉ đọc lại
    step(5, "dns_cutover", ok=res.get("ok"), active_region=res.get("active_region"),
         waited_ready_s=res.get("waited_s"))
    if not res.get("ok"):
        return finish(aborted_at=f"failover:{res.get('aborted_at')}")

    # 6. Golden signals trên region mới
    gs = golden_signals(target, GOLDEN_REQUESTS)
    step(6, "verify_golden_signals", region=target, **gs)
    summary["golden_signals"] = gs

    return finish(ok=gs["error_rate"] == 0)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
