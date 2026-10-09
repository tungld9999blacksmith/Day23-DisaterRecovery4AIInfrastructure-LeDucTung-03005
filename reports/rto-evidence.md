# RTO/RPO Evidence — Lab 23 (TEMPLATE — sinh viên điền bằng SỐ CỦA MÌNH)

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

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:__` |
| User thấy lỗi đầu tiên | | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:__` |
| Health check phát hiện | | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:__` |
| Snapshot restore xong | | `step:2_restore_snapshot` | `reports/failover-events.jsonl:__` |
| Region phụ ready | | `step:4_wait_ready` | `reports/failover-events.jsonl:__` |
| DNS cutover | | `step:5_dns_cutover` | `reports/failover-events.jsonl:__` |
| **RTO đo được** | | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:__` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `__s` | 300s (5 phút) | |
| RPO — Vector DB | `__s` / `__` doc | 300s (5 phút) | |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | | `interval_s × threshold` trong `reports/health-events.jsonl:__` | |
| Snapshot restore | | 2_restore → 3_scale | |
| GPU pool warm-up | | `waited_s` ở `4_wait_ready` | |
| DNS/LB TTL cache | | t_recovered − t_cutover | |
