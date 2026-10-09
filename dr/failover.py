"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


POLL_S = 0.5


def emit(**kw) -> dict:
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    """/v1/state của region: pool_state, weights, count. Ném httpx.HTTPError nếu không gọi được."""
    r = httpx.get(f"{URL[region]}/v1/state", timeout=3)
    r.raise_for_status()
    return r.json()


def wait_ready(region: str, wait: float) -> tuple[bool, float, dict]:
    """Poll /readyz tới khi 200 hoặc hết `wait` giây. Trả (ready, waited_s, body cuối cùng)."""
    t0 = time.time()
    body: dict = {}
    while True:
        try:
            r = httpx.get(f"{URL[region]}/readyz", timeout=2)
            body = r.json()
            if r.status_code == 200:
                return True, round(time.time() - t0, 2), body
        except (httpx.HTTPError, ValueError) as e:
            body = {"error": type(e).__name__}
        if time.time() - t0 >= wait:
            return False, round(time.time() - t0, 2), body
        time.sleep(POLL_S)


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước verify -> restore -> scale -> wait_ready -> cutover, đúng thứ tự.

    Bất kỳ bước nào trước 5_dns_cutover thất bại -> ABORT, edge/active_region giữ nguyên.
    """
    primary = "a" if target == "b" else "b"
    t_start = time.time()
    result = {"ok": False, "target": target, "primary": primary, "backend": backend}

    def abort(step: str, **kw) -> dict:
        emit(step=step, ok=False, aborted=True, **kw)
        result.update(aborted_at=step, elapsed_s=round(time.time() - t_start, 2), **kw)
        return result

    # 1. Region đích hiện có gì? (trước failover: kỳ vọng count=0, weights=false, pool=warm)
    try:
        before = state_of(target)
    except httpx.HTTPError as e:
        return abort("1_verify_target", error=type(e).__name__)
    emit(step="1_verify_target", ok=True, target=target, pool_state=before.get("pool_state"),
         weights=before.get("weights"), count=before.get("count"))

    # 2. Restore vector DB + weights từ snapshot, đo RPO thật (primary có mà bản restore không có)
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:  # snapshot.get raise SystemExit khi chưa từng `put`
        return abort("2_restore_snapshot", error=str(e))
    rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True, backend=backend,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"), snapshot_latest_doc_ts=meta.get("latest_doc_ts"))
    result.update(rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))

    # 3. warm -> full. serving/app.py bắt đầu đếm WARMUP_SECONDS từ lúc thấy thay đổi này
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.write_text("full")
    emit(step="3_scale_pool", ok=True, target=target, pool_state="full")

    # 4. Chỉ đi tiếp khi /readyz trả 200 — cutover sớm = 503 từ CẢ HAI region
    ready, waited_s, body = wait_ready(target, wait)
    if not ready:
        return abort("4_wait_ready", waited_s=waited_s, wait_limit_s=wait, last_readyz=body)
    vectors = (body.get("vectors") or {}).get("count")
    emit(step="4_wait_ready", ok=True, waited_s=waited_s, vectors=vectors)
    result.update(waited_s=waited_s, vectors=vectors, weights=True)

    # 5. DNS cutover: edge đọc lại file này sau tối đa EDGE_TTL_SECONDS
    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", ok=True, active_region=target, previous_region=primary)
    result.update(ok=True, active_region=target, elapsed_s=round(time.time() - t_start, 2))
    return result



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
