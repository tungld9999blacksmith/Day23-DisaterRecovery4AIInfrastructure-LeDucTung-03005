"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


HEALTHY, UNHEALTHY = "HEALTHY", "UNHEALTHY"


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Không bao giờ ném exception: region chết = probe fail.

    netblock -> ReadTimeout sau `timeout` giây, stop -> ConnectError ngay.
    """
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.HTTPError as e:
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons")
    except ValueError:
        reasons = r.text[:200]
    return False, f"http_{r.status_code}: {reasons}"


def next_state(current: str, fails: int, ready: bool, threshold: int) -> str:
    """Máy trạng thái chống flapping: UNHEALTHY chỉ khi fail >= threshold lần LIÊN TIẾP,
    HEALTHY lại ngay khi probe thành công."""
    if ready:
        return HEALTHY
    if fails >= threshold:
        return UNHEALTHY
    return current


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll /readyz của mọi region mỗi `interval` giây, ghi 1 dòng JSONL mỗi lần đổi trạng thái."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # Giả định ban đầu HEALTHY và KHÔNG log -> dòng đầu tiên trong log luôn là một sự cố thật
    state = dict.fromkeys(URL, HEALTHY)
    fails = dict.fromkeys(URL, 0)
    end = time.time() + duration

    with out.open("a", encoding="utf-8") as f:
        def emit(region: str, to: str, reason: str):
            rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                   "event": "state_change", "region": region, "from": state[region], "to": to,
                   "reason": reason, "consecutive_fails": fails[region],
                   "interval_s": interval, "threshold": threshold}
            f.write(json.dumps(rec) + "\n")
            f.flush()  # runbook.py đọc file này trong lúc checker vẫn chạy
            print("HEALTH", json.dumps(rec), flush=True)

        while time.time() < end:
            for region in URL:
                ready, reason = probe(region, timeout)
                fails[region] = 0 if ready else fails[region] + 1
                to = next_state(state[region], fails[region], ready, threshold)
                if to != state[region]:
                    emit(region, to, reason)
                    state[region] = to
            # sleep cố định, không bù thời gian probe: detect không thể sớm hơn interval*threshold
            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
