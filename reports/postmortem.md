# Postmortem — DR Drill Lab 23: Region A down (netblock) → failover sang Region B

Tác giả: Lê Đức Tùng · Ngày drill: 2026-10-09 · Mức độ: SEV1 (giả lập) · Trạng thái: resolved

Theo template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** lúc 13:30:28 UTC Region A bị netblock (process bị đóng băng, cổng vẫn nhận kết nối
nhưng không trả lời). Health checker phát hiện sau 21.2s, runbook tự động failover sang Region B,
user nhận lại câu trả lời đúng sau **31.5s** (RTO mục tiêu 300s). Mất **6 document** (RPO 12.0s).
12 request của user bị lỗi `ReadTimeout` trong thời gian outage. So với drill 1 không có DR
(`reports/drill-1-nodr.jsonl`): `NO_RECOVERY`, 100% request lỗi tới hết drill.

## 1. Timeline (mọi dòng phải có evidence path:line)

Giờ UTC, mốc 0 = t_outage.

| ISO time | +giây | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T13:30:28 | −0.5 | Request OK cuối cùng do Region A phục vụ | `reports/drill-2-withdr.jsonl:28` |
| 2026-10-09T13:30:28 | 0 | **Outage bắt đầu** — netblock region a, region b còn sống | `chaos/chaos-events.jsonl:7` |
| 2026-10-09T13:30:31 | 2.5 | **User đầu tiên bị ảnh hưởng** — `ReadTimeout` sau 2.4s | `reports/drill-2-withdr.jsonl:30` |
| 2026-10-09T13:30:50 | 21.2 | **Health check alert** — `a → UNHEALTHY`, 3 lần fail liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T13:30:50 | 21.5 | Runbook xác nhận outage + mở incident (`notify_delay_s` 21.52) | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| 2026-10-09T13:30:50 | 21.5 | **Operator confirm cutover** (`--auto`) → gọi failover. `confirmed_by: auto` được ghi ở dòng 3 lúc 13:30:57, sau khi failover trả kết quả | `reports/runbook-run.jsonl:3` |
| 2026-10-09T13:30:51 | 22.1 | Restore snapshot vào B xong — RPO 12.0s, 6 doc mất | `reports/failover-events.jsonl:2` |
| 2026-10-09T13:30:57 | 29.0 | B ready sau 6.83s warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-09T13:30:57 | 29.0 | DNS cutover → `active_region = b` | `reports/failover-events.jsonl:5` |
| 2026-10-09T13:30:57 | 29.1 | Request vẫn bị edge route vào A (cache TTL chưa hết) → lỗi | `reports/drill-2-withdr.jsonl:41` |
| 2026-10-09T13:30:58 | 29.5 | Golden signals trên B: 10/10 OK, p95 14ms | `reports/runbook-run.jsonl:6` |
| 2026-10-09T13:31:00 | **31.5** | **Resolved** — request OK đầu tiên, `served_by: b` | `reports/drill-2-withdr.jsonl:42` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `31.5s` · gap: `−268.5s` (đạt, còn dư 89.5% ngân sách)
- RPO mục tiêu: 300s · đo được: `12.0s` (`6` doc bị mất) · gap: `−288.0s` (đạt)
- Nguồn số: `tools/measure_rto.py` → `valid: true`, `warnings: []`, `rto_verdict: PASS`
  (chi tiết ở `reports/rto-evidence.md`).

| Thành phần RTO | Giây | % RTO |
|---|---:|---:|
| Health-check detect | 21.2 | 67% |
| Snapshot restore (gồm runbook đọc health log) | 0.9 | 3% |
| GPU pool warm-up | 6.8 | 22% |
| DNS/LB TTL cache | 2.6 | 8% |
| **Tổng** | **31.5** | 100% |

- **Bước tốn nhiều giây nhất:** `health-check detect` — 21.2s, 2/3 RTO. Vì sao: (1) floor
  thiết kế `interval × threshold` = 5s × 3 = 15s là cố ý để chống flapping; (2) netblock làm
  **mỗi** lần probe A treo hết timeout 2s, nên một chu kỳ probe thực tế là ~7s chứ không phải 5s
  → thêm 6.2s so với floor. Đây là cái giá của kiểu lỗi "treo" so với "chết hẳn" (`--mode stop`
  fail ngay bằng `ConnectError`).

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

**Vì sao user bị lỗi 31.5s?**

1. Vì edge tiếp tục gửi traffic vào A cho tới lúc cutover (+29.0s) và thêm một chu kỳ TTL sau đó.
2. Vì không thành phần nào được phép đổi route trước khi outage được **xác nhận** — xác nhận cần
   3 lần probe fail liên tiếp (chống flapping), mỗi lần tốn 5s chờ + 2s timeout.
3. Vì khi đã xác nhận, Region B chưa thể nhận traffic ngay: B được giữ ở dạng *pilot light*
   (`pool_state: warm`, không dữ liệu, không weights — xem `reports/failover-events.jsonl:1`),
   phải restore + warm-up GPU 6.8s.
4. Vì kiến trúc chọn active-passive với replication **bất đồng bộ mỗi 30s** để tiết kiệm chi phí
   GPU/lưu trữ ở region phụ.
5. → Gốc rễ: RTO/RPO hiện tại là hệ quả trực tiếp của 3 tham số đã chọn (chu kỳ health check,
   mức độ "ấm" của standby, chu kỳ replication) — không phải lỗi ngẫu nhiên. Chúng đạt mục tiêu
   300s với biên rất rộng, nghĩa là có thể đánh đổi một phần biên đó lấy chi phí thấp hơn, hoặc
   ngược lại.

**Nếu đây là outage thật, những bước sau của runbook sẽ thất bại hoặc cho số sai:**

| Bước | Trong drill | Ngoài thực tế |
|---|---|---|
| Replication | `reports/replication.jsonl:2` vẫn chụp được snapshot **10.1s sau** khi A chết, vì `state/replicate.py` đọc thẳng file trên đĩa | Region chết → không còn snapshot mới. RPO thật = khoảng cách tới snapshot cuối **trước** outage, tối đa 30s (~15 doc ở 0.5 doc/s), không phải 12.0s |
| Đo `docs_lost` | `state/snapshot.py` tính bằng cách đọc DB của A sau outage | Không đọc được DB của region đã mất → chỉ ước lượng được (tốc độ ingest × tuổi snapshot), con số chính xác chỉ có khi A sống lại |
| Mốc t_outage | Runbook đọc `chaos/chaos-events.jsonl` | Không có file này. t_outage phải suy ra từ lỗi đầu tiên trong metric/log của edge → `notify_delay_s` thật sẽ kém chính xác hơn |
| Replication & failover cùng "máy" | Snapshot nằm ở `state/_replica/` cùng máy | Bucket replica phải ở region khác; nếu nó cùng region với A thì bước restore thất bại hoàn toàn |

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ probe timeout 2s → 0.5s (`/readyz` bình thường ~15ms), giữ `interval 5s × threshold 3` | SRE on-call | 2026-10-16 | RTO −4.5s (detect 21.2s → ~16.7s), không tăng rủi ro flapping |
| 2 | Edge loại upstream lỗi ngay (passive health check) thay vì chờ hết `EDGE_TTL_SECONDS` 5s | Network/Edge | 2026-10-23 | RTO −2.6s (đo được), tối đa −5s |
| 3 | Pre-warm: khi health checker thấy 1–2 lần fail liên tiếp, restore snapshot + scale B lên `full` **nhưng chưa cutover** | ML Platform | 2026-10-30 | RTO −6.8s (warm-up chạy song song với detect) |
| 4 | Replication liên tục (stream/WAL) thay cho snapshot mỗi 30s; snapshot đặt ở bucket khác region | Data Platform | 2026-11-06 | RPO tối đa 30s → vài giây (−~25s); hết rủi ro mất bucket cùng region |
| 5 | Runbook nhận alert dạng push (webhook) thay vì poll file 1s; t_outage lấy từ metric lỗi của edge | SRE on-call | 2026-10-23 | RTO −0.5s; `notify_delay_s` đo được ngoài drill |

Tổng tiềm năng nếu làm cả 1–3: RTO ~31.5s → ~17s, chủ yếu do detect.

## 5. Ba câu hỏi bắt buộc trả lời

**1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**

5s × 3 = **15s**, bằng **47.6%** RTO 31.5s. Nhưng thời gian detect thực tế là 21.2s
(`reports/health-events.jsonl:2`), tức **67%** RTO — vì với netblock mỗi lần probe còn treo thêm
2s timeout. Floor là giới hạn dưới, không phải con số thực tế. Với RTO 300s, detect chỉ nên chiếm
tối đa ~1/3 (100s), nên `interval × threshold` ≤ 100s → với threshold 3, interval có thể lên tới
~30s mà vẫn đạt; chọn 5s là còn rất nhiều biên.

**2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**

Floor còn 1s × 3 = 3s. Probe vẫn treo 2s mỗi lần nên detect ≈ 3 × (1 + 2) = ~9s thay vì 21.2s
→ **RTO giảm ~12s** (31.5s → ~19.5s). Cái giá:

- **Flapping:** cả cửa sổ xác nhận chỉ còn ~9s. Một GC pause, một lần mạng nghẽn, hay một lần
  deploy rolling ~3–9s đủ để kích hoạt failover thật — mỗi lần failover lại tốn restore +
  warm-up + TTL (~10s outage) và RPO, tức là "chữa" một sự cố 5s bằng một sự cố 10s.
- **Tải:** số probe tăng 5 lần (×2 region), bản thân health checker thành nguồn tải lên `/readyz`.
- Nếu muốn RTO nhỏ hơn, hạ **timeout** (action item 1) có lợi hơn hạ interval: −4.5s mà không
  thu hẹp cửa sổ chống flapping.

**3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa
gì với khách hàng?**

`docs_lost = 6` nghĩa là 6 ticket của khách đã được hệ thống nhận ("ghi thành công") nhưng **không
tồn tại** ở Region B. Nếu A mất vĩnh viễn, 6 ticket đó **mất hẳn**, không có cách khôi phục: B sẽ
trả lời như thể khách chưa từng hỏi. Ngoài thực tế con số còn có thể lớn hơn — tới toàn bộ dữ liệu
kể từ snapshot cuối trước outage (tối đa 30s ≈ 15 doc ở tốc độ drill; tỷ lệ thuận với traffic
thật). Thời gian outage 6 giờ **không** làm `docs_lost` tăng — sau cutover, dữ liệu mới ghi vào B —
nhưng làm tăng rủi ro ở bước failback (dữ liệu 6 giờ ở B phải đồng bộ ngược nếu A được dựng lại).
Hành động với khách hàng: xác định danh sách ticket trong cửa sổ RPO (từ log của edge/ingest), thông
báo chủ động và đề nghị gửi lại; về dài hạn, action item 4 (replication liên tục) để thu hẹp cửa sổ.
