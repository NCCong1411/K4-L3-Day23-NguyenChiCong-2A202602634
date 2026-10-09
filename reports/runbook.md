# Runbook 1 trang — Region chính down

Áp dụng cho bare mode, primary `a`, secondary `b`, snapshot backend `fs`. Chạy mọi lệnh
từ thư mục gốc repository với `.venv` đã kích hoạt. Không dùng
`--i-really-want-both` và không sửa `edge/active_region` bằng tay.

Lệnh ở bước 2 chạy toàn bộ automation từ xác nhận đến golden signals. Các lệnh tại
bước 3–6 là checkpoint độc lập, copy-paste được để người trực ca xác minh từng kết quả;
không lặp lại restore, scale hoặc cutover bằng tay.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `for i in 1 2 3; do python chaos/kill_region.py status --region a; sleep 5; done` | A không ready ba lần liên tiếp, B vẫn trả lời `/healthz`; health checker ghi `UNHEALTHY` | on-call SRE |
| 2 | Mở incident và bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` | Người vận hành nhập `y`; bước `thong_bao_incident` xuất hiện trong `reports/runbook-run.jsonl` | Incident Commander |
| 3 | Restore state ở B | `python state/snapshot.py lag --backend fs` | `2_restore_snapshot` có `ok:true`, `rpo_seconds`, `docs_lost` và model version trong `reports/failover-events.jsonl` | data-platform on-call |
| 4 | Scale pool warm→full | `curl -sf localhost:8002/readyz` | HTTP 200, `pool_state:"full"`, weights có mặt và vector count > 0 | ML-platform on-call |
| 5 | DNS/LB cutover | `curl -s localhost:8080/edge/state` | `active_region:"b"`; chỉ được xảy ra sau bước 4 | network/SRE on-call |
| 6 | Verify golden signals | `tail -n 2 reports/runbook-run.jsonl` | `verify_golden_signals` có 10 request, error rate 0 và p95 dưới 200 ms | service owner |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:"PASS"`, recovery do B phục vụ | Incident Commander |

## Rollback về Region A

Chỉ rollback khi A đã được `restore --backend bare`, qua ít nhất ba lần readiness liên
tiếp, dữ liệu mới ở B đã được snapshot, và golden signals của A đạt ngưỡng. Incident
Commander là người duy nhất phê duyệt rollback; tuyệt đối không full-auto để tránh hai
region flap qua lại.

```bash
python chaos/kill_region.py restore --region a --backend bare
python state/snapshot.py put --region b --backend fs
python dr/failover.py --target a --backend fs
curl -s localhost:8080/edge/state
```

Rollback phải dừng ngay nếu A không ready trong thời hạn của `failover.py`; hàm sẽ không
cutover khi target chưa sẵn sàng.
