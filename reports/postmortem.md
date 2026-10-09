# Postmortem — DR Drill Lab 23

## 1. Timeline

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:15:30Z | Outage Region A bắt đầu | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T05:15:31Z | User đầu tiên nhận 503/ReadTimeout | `reports/drill-2-withdr.jsonl:32` |
| 2026-10-09T05:15:47Z | Health checker đánh dấu A `UNHEALTHY` | `reports/health-events.jsonl:2` |
| 2026-10-09T05:15:51Z | Operator đã cho phép failover; bước verify target bắt đầu | `reports/failover-events.jsonl:1` |
| 2026-10-09T05:15:58Z | B ready và DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T05:16:01Z | Request đầu tiên thành công từ B | `reports/drill-2-withdr.jsonl:47` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: 300s; đo được: **30.8s**; gap là **269.2s headroom**.
- RPO mục tiêu: 300s; đo được: **18.0s và 9 document**; gap là **282.0s headroom**.
- Bước tốn nhiều thời gian nhất là health-check detection: 16.7s, tương đương 54.2%
  RTO. Trong đó detect floor cấu hình là 15.0s.
- Snapshot/điều phối tốn 4.8s, GPU warm-up 6.5s và DNS TTL còn 2.8s. Tổng là 30.8s.

## 3. Root cause — 5 Whys

1. Vì sao user gặp lỗi? Edge vẫn cache và gửi request tới Region A đang bị network block.
2. Vì sao Edge chưa chuyển ngay? Cutover bị khóa cho tới khi outage được xác nhận và B ready.
3. Vì sao xác nhận mất phần lớn RTO? Chính sách chống flapping yêu cầu ba probe lỗi liên tiếp,
   mỗi probe cách nhau 5 giây và request bị block phải chờ timeout.
4. Vì sao B chưa thể nhận traffic ngay sau detection? B khởi đầu ở warm state, chưa có vector,
   weights và cần 6.485 giây warm-up sau restore.
5. Vì sao vẫn giữ thiết kế này? Lab ưu tiên failover an toàn, có RPO đo được và không cutover
   sang target chưa sẵn sàng hơn là tối ưu một con số RTO bằng thao tác tay.

Root cause hệ thống là tổng của detection policy, asynchronous replication, cold/warm capacity
và DNS cache; chaos script chỉ kích hoạt tình huống để đo các khoảng trễ này.

## 4. Action items

| # | Action | Owner | Deadline | Giảm RTO/RPO dự kiến |
|---|---|---|---|---|
| 1 | Thử interval 2s, vẫn giữ threshold 3 và theo dõi false-positive qua 5 game day | SRE lead | 2026-10-16 | detect floor 15s → 6s, giảm RTO khoảng 9s |
| 2 | Tăng replication từ mỗi 30s lên mỗi 15s và theo dõi I/O | Data Platform | 2026-10-23 | giảm worst-case RPO thêm tối đa 15s |
| 3 | Duy trì một worker B pre-warm và đo chi phí | ML Platform | 2026-10-30 | giảm khoảng 6.5s warm-up |

## 5. Ba câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15.0s`, chiếm **48.7%** RTO 30.8s. Detection
   thực tế là 16.7s do lịch poll và timeout, chiếm 54.2%.
2. Nếu interval là 1s và vẫn giữ threshold 3, detect floor giảm từ 15s xuống 3s;
   RTO lý thuyết giảm khoảng 12s còn 18.8s. Đổi lại hệ thống tạo nhiều probe hơn,
   tăng tải và nhạy hơn với lỗi ngắn hạn; threshold vẫn phải giữ để hạn chế flapping.
3. Nếu primary mất vĩnh viễn sau outage sáu giờ, `docs_lost` là số ticket/document đã
   được khách hàng ghi nhận ở primary nhưng không tồn tại trong snapshot phục hồi. Những
   bản ghi đó cần tái tạo từ nguồn khác hoặc yêu cầu khách hàng nhập lại, nên đây là tác
   động dữ liệu thực chứ không chỉ là một chỉ số kỹ thuật.
