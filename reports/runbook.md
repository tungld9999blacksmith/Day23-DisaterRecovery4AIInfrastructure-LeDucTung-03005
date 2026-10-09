# Runbook 1 trang — Region chính (A) down → failover sang Region B

Dành cho on-call lúc 3h sáng, không cần đọc code. Mọi lệnh chạy trong **PowerShell tại thư mục
gốc repo**. (Linux/WSL: thay `.\lab.ps1 py` bằng `python3`, `.\lab.ps1 status` bằng
`python3 chaos/kill_region.py status`, `curl.exe` bằng `curl`.)

**Đường nhanh (khuyến nghị):** chạy cả 7 bước tự động, có hỏi xác nhận trước khi failover:

```powershell
.\lab.ps1 py dr/runbook.py --primary a --target b --backend fs
```

Nhật ký từng bước nằm ở `reports/runbook-run.jsonl` (timeline), 5 bước con của failover ở
`reports/failover-events.jsonl`. Nếu script lỗi giữa chừng → làm tay theo bảng dưới, **từ bước
đang dở**.

## Điều kiện trước khi bắt đầu (cả 3 phải đúng, nếu không → DỪNG, gọi Incident Commander)

| Kiểm tra | Lệnh | Phải thấy |
|---|---|---|
| Region B còn sống | `.\lab.ps1 status` | `b.alive = true` (nếu B cũng chết = double outage, failover vô nghĩa) |
| Đã có snapshot | `Test-Path state\_replica\dr-artifacts\MANIFEST.json` | `True` |
| Độ trễ snapshot trong ngưỡng RPO | `.\lab.ps1 py state/snapshot.py lag --backend fs` | `rpo_seconds` < 300 (bình thường ≈ 30s = chu kỳ replicate) |

## 7 bước

| # | Bước | Lệnh (copy-paste) | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `.\lab.ps1 status` — chạy **3 lần, cách nhau 5s**. Đối chiếu health checker: `Select-String reports\health-events.jsonl -Pattern '"region": "a".*"to": "UNHEALTHY"'` | `a.ready = false` cả 3 lần liên tiếp **hoặc** health log có dòng `a → UNHEALTHY` với `consecutive_fails ≥ 3`. Chỉ 1 lần fail → **chưa phải outage**, tiếp tục theo dõi | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `Get-Content chaos\chaos-events.jsonl -Tail 1` (lấy `iso` = t_outage) → mở kênh incident, page Incident Commander (IC), ghi SEV1 | Có t_outage và t_notified; `reports/runbook-run.jsonl` có dòng `step 2` với `notify_delay_s` | On-call SRE |
| — | **Cổng quyết định** | Báo IC: "A down từ {t_outage}, B sống, snapshot lag {N}s. Failover?" | IC trả lời **YES** (script: gõ `y`). Không có YES → không đi tiếp | Incident Commander |
| 3 | Restore state ở region phụ | `.\lab.ps1 py state/snapshot.py get --region b --backend fs` rồi `curl.exe -s http://127.0.0.1:8002/v1/state` | Output `get` có `embed_model_version` **trùng** với A (`embed-model=vi-e5-base@v3`); `/v1/state` của B: `weights: true`, `count > 0` | ML Platform on-call |
| 4 | Scale pool warm→full | `Set-Content -NoNewline state\region-b\pool_state full` rồi lặp `curl.exe -s http://127.0.0.1:8002/readyz` mỗi 2s | `/readyz` của B trả `"ready": true` (HTTP 200). Bình thường ≈ 7s (GPU warm-up 6s). **Quá 60s → ABORT, KHÔNG làm bước 5**, báo IC | ML Platform on-call |
| 5 | DNS/LB cutover | **Chỉ sau khi bước 4 xong:** `Set-Content -NoNewline edge\active_region b` | Sau ≤ 5s (TTL edge): `curl.exe -s http://127.0.0.1:8080/edge/state` → `"active_region":"b"`; `curl.exe -s http://127.0.0.1:8080/v1/infer` → `"edge_region":"b"`, `"region":"b"` | On-call SRE |
| 6 | Verify golden signals | `1..10 \| % { curl.exe -s -o NUL -w "%{http_code} %{time_total}\n" http://127.0.0.1:8080/v1/infer }` | 10/10 trả `200` (error rate = 0%, ngưỡng < 1%); p95 `time_total` < 1.0s qua edge (baseline drill: trung bình 0.41s, max 0.60s). Không đạt → báo IC, **không tự rollback** | On-call SRE |
| 7 | Đo RTO + postmortem | `.\lab.ps1 rto` (= `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`) | `"valid": true`, `"rto_verdict": "PASS"`, `"warnings": []`. Ghi RTO, RPO (`rpo_at_restore_s` + `docs_lost`) vào `reports/postmortem.md` trong 48h | IC (giao người viết postmortem) |

Tham chiếu drill 2: detect ở +21.2s, cutover +29.0s, RTO đo được 31.5s / mục tiêu 300s; RPO 12.0s,
6 document bị mất. Bước 4 mất 6.83s (`reports/failover-events.jsonl:4`).

## Rollback — trả traffic về Region A

**Không bao giờ tự động.** A "sống lại" không có nghĩa là A an toàn: tự động failback khi A vừa
lên là đúng kịch bản flapping A→B→A→B (§4 Anti-Patterns), mỗi lần chuyển lại thêm một outage nhỏ
(restore + warm-up + TTL).

**Ai quyết định:** chỉ **Incident Commander**. On-call SRE thực hiện, ML Platform on-call xác nhận
dữ liệu.

**Điều kiện failback (phải đủ TẤT CẢ):**

1. Đã biết và đã sửa nguyên nhân gốc khiến A down (không failback "để thử").
2. A trả `/readyz` 200 **liên tục ≥ 15 phút** (`.\lab.ps1 status` hoặc health log không có
   chuyển trạng thái nào của A trong 15 phút).
3. Dữ liệu ghi vào B trong lúc incident đã được đồng bộ ngược sang A — nếu không, failback làm mất
   chính những document đó (RPO lần hai).
4. B vẫn đang khoẻ (golden signals đạt) — failback là việc có kế hoạch, không phải chữa cháy.
5. Thực hiện trong khung giờ ít traffic, đã thông báo trước trong kênh incident.

**Cách làm:** chụp snapshot từ B rồi chạy failover theo chiều ngược lại (cùng 5 bước, cùng quy tắc
"không cutover khi đích chưa ready"):

```powershell
.\lab.ps1 py state/snapshot.py put --region b --backend fs
.\lab.ps1 py dr/failover.py --target a --backend fs
```

Biết là xong khi `curl.exe -s http://127.0.0.1:8080/v1/infer` trả `"region":"a"` và bước 6 (golden
signals) đạt trên A.

**Rollback ngay giữa failover (B hỏng sau cutover):** nếu bước 6 thất bại **và** A đã khoẻ trở lại
theo điều kiện 2, IC có thể quyết định trả `edge/active_region` về `a`
(`Set-Content -NoNewline edge\active_region a`). Nếu cả A lẫn B đều không đạt → giữ nguyên B,
escalate, không chuyển qua lại.

**Chống flapping:** sau một lần failover, **không failover tiếp trong 30 phút** trừ khi IC quyết
định (cooldown).
