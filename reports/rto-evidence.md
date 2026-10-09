# RTO/RPO Evidence — Lab 23

Tất cả số liệu dưới đây được lấy từ drill chạy ngày 2026-10-09 trên bare mode với
`netblock --mock`; không sử dụng số tham khảo trong guide.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T05:13:49Z | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.0s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | Không có | 16 request sau outage đều thất bại | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | dòng cuối cửa sổ vẫn lỗi, không có recovery | `reports/drill-1-nodr.jsonl:32` |

Drill 1 có 16 request thất bại sau outage và không có request thành công trở lại.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | `action:kill`, Region A | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.4s | dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:32` |
| Health check phát hiện A | 16.7s | `to:UNHEALTHY`, interval 5s, threshold 3 | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 21.5s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | 28.0s | `step:4_wait_ready`, waited 6.485s | `reports/failover-events.jsonl:4` |
| DNS cutover | 28.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **30.8s** | request `ok:true` đầu tiên do B phục vụ | `reports/drill-2-withdr.jsonl:47` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | 30.8s | 300s | PASS |
| RPO — Vector DB | 18.0s / 9 document | 300s | PASS |

RPO và model version được ghi cùng thời điểm restore tại
`reports/failover-events.jsonl:2`; replication dùng chu kỳ 30 giây được chứng minh tại
`reports/replication.jsonl:2`. Khi chạy lại công cụ đo trên các log này, kết quả tổng
hợp là `valid:true` và `warnings:[]`.

## 3. Phân rã RTO

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detection | 16.7s | outage → `UNHEALTHY`; detect floor là 5s × 3 = 15.0s tại `reports/health-events.jsonl:2` | giảm interval nhưng giữ threshold và theo dõi false positive |
| Snapshot restore và điều phối | 4.8s | detection → restore hoàn tất tại `reports/failover-events.jsonl:2` | snapshot nhỏ hơn, restore song song phần độc lập |
| GPU pool warm-up | 6.5s | scale → ready, `waited_s:6.485` tại `reports/failover-events.jsonl:4` | giữ warm capacity hoặc pre-warm có kiểm soát |
| DNS/LB TTL cache | 2.8s | cutover → request đầu tiên thành công ở `reports/drill-2-withdr.jsonl:47` | giảm TTL, chấp nhận tăng tải DNS |
| **Tổng** | **30.8s** | bằng RTO đo từ timestamp | đạt mục tiêu 300s |

Các giá trị được làm tròn một chữ số thập phân; tổng bốn thành phần bằng RTO 30.8s.
