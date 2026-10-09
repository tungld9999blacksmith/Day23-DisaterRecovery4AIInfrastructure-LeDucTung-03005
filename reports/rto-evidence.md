# RTO/RPO Evidence — Lab 23 (Lê Đức Tùng)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T11:13:32` | chaos kill (`--mode netblock`, region a) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.1s` | dòng `ok:false` đầu tiên sau t_outage (`ReadTimeout`, 2451ms) | `reports/drill-1-nodr.jsonl:21` |
| Request lỗi sau t_outage | 12/12 | đếm dòng có `ts >= t_outage` trong loadgen log | `reports/measure-drill-1.json` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

Ghi chú:

- Request cuối cùng thành công là `seq=18` (`reports/drill-1-nodr.jsonl:19`), được Region A
  phục vụ 0.89s trước t_outage.
- `reports/drill-1-nodr.jsonl:20` (`seq=19`) cũng lỗi `ReadTimeout`, nhưng được gửi 0.39s
  **trước** t_outage: nó đang chờ A trả lời thì A bị đóng băng. `tools/measure_rto.py` chỉ tính
  request gửi sau t_outage, nên mốc "user thấy lỗi đầu tiên" là dòng 21. Tính cả dòng 20 thì
  toàn bộ drill có 13 request lỗi.
- Mỗi request lỗi treo khoảng 2.4s (timeout 2s của edge + overhead), đúng triệu chứng
  netblock: kernel vẫn nhận kết nối TCP nhưng process không trả lời.
- Từ t_outage tới request cuối của loadgen (+28.5s, `reports/drill-1-nodr.jsonl:32`), không
  có thành phần nào phát hiện outage hay chuyển traffic sang B → hệ thống không tự hồi phục.

## 2. Drill 2 — có DR

t_outage = `2026-10-09T13:30:28` (`--mode netblock`, region a, `other_alive: true`).
Cấu hình drill: health checker `interval 5s × threshold 3`, probe timeout 2s, `EDGE_TTL_SECONDS` 5s,
`WARMUP_SECONDS` 6s, replicate mỗi 30s, ingest 0.5 doc/s, loadgen 2 rps.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi đầu tiên | +2.5s | dòng `ok:false` đầu (`ReadTimeout`) | `reports/drill-2-withdr.jsonl:30` |
| Health check phát hiện | +21.2s | `to:UNHEALTHY, region:a`, `consecutive_fails: 3` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +22.1s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +29.0s | `step:4_wait_ready`, `waited_s: 6.83` | `reports/failover-events.jsonl:4` |
| DNS cutover | +29.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+31.5s** | dòng `ok:true` đầu sau lỗi, `served_by: b` | `reports/drill-2-withdr.jsonl:42` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `31.5s` | 300s (5 phút) | **PASS** (dư 268.5s) |
| RPO — Vector DB | `12.0s` / `6` doc | 300s (5 phút) | **PASS** (dư 288.0s) |

Ghi chú:

- Lệnh đo: `tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` →
  `valid: true`, `warnings: []`, `recovered_by_region: b`, `rto_measured_s: 31.5`,
  `rpo_at_restore_s: 12.0`, `docs_lost: 6`, 12 request lỗi sau t_outage (toàn bộ `ReadTimeout`).
- RPO 12.0s / 6 doc ghi ở `reports/failover-events.jsonl:2`: bản restore lấy snapshot của
  `reports/replication.jsonl:2`, còn primary vẫn nhận ingest thêm 12s sau đó (0.5 doc/s × 12s = 6 doc).
- Runbook (`reports/runbook-run.jsonl`) chỉ gọi failover sau khi đọc được dòng UNHEALTHY của health
  checker → t_cutover (+29.0s) > t_detect (+21.2s), không có cảnh báo cutover sớm.
- Golden signals trên B sau cutover: 10/10 request OK, p95 14ms, error rate 0.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

Bốn khoảng nối tiếp nhau, cộng lại đúng bằng RTO: 21.2 + 0.9 + 6.8 + 2.6 = **31.5s**.

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 21.2s (floor 15s) | `interval_s × threshold` = 5 × 3 = 15s trong `reports/health-events.jsonl:2`. Thêm 6.2s vì netblock làm **mỗi** lần probe A treo hết timeout 2s (3 lần fail ≈ 3 × (5 + 2)s, trừ phần lệch pha với lúc kill) | Hạ probe timeout 2s → 0.5s (−4.5s, vì `/readyz` bình thường chỉ ~15ms); hạ interval 5s → 2s (−9s). Đánh đổi: dễ flapping hơn, nên giữ threshold 3 |
| Snapshot restore | 0.9s | t(`3_scale_pool`) − t_detect: runbook đọc lại health log (poll 1s) + verify + copy snapshot (`reports/failover-events.jsonl:1`–`:3`) | Runbook dùng tín hiệu push (webhook/alert) thay vì poll file (−0.5s). Với vector DB lớn thật, restore sẽ là phút chứ không phải giây → giữ sẵn bản replica ở region phụ (warm standby có dữ liệu) |
| GPU pool warm-up | 6.8s | `waited_s` 6.83 ở `reports/failover-events.jsonl:4` — `WARMUP_SECONDS` 6s tính từ lúc `pool_state` đổi `warm → full` + 1 vòng poll 0.5s | Giữ B ở mức `full` một phần (hot standby: tốn chi phí GPU 24/7, giảm gần hết 6.8s); hoặc nạp sẵn weights vào GPU khi health checker mới thấy 1–2 lần fail (pre-warm, không cutover) |
| DNS/LB TTL cache | 2.6s | t_recovered − t_cutover. Request gửi sau cutover ở +29.1s vẫn bị edge route vào A (`edge_region: a`, `reports/drill-2-withdr.jsonl:41`); request kế tiếp mới tới B (`reports/drill-2-withdr.jsonl:42`) | Hạ `EDGE_TTL_SECONDS` 5s → 1s (trường hợp xấu nhất −4s); hoặc cho edge tự loại upstream lỗi (passive health check) thay vì chờ TTL |
