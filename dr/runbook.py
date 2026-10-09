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
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"ts": now,
              "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Tự xác nhận trong CI; chế độ thường luôn hỏi y/N."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def _latest_outage(primary: str) -> dict | None:
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("action") == "kill" and event.get("region") == primary:
                events.append(event)
    return events[-1] if events else None


def _target_alive(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=1.5).status_code == 200
    except Exception:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực hiện và ghi bằng chứng cho đủ bảy bước runbook."""
    started = time.time()

    failures = 0
    reasons = []
    for attempt in range(3):
        ready, reason = health_checker.probe(primary, timeout=2.0)
        reasons.append(reason)
        failures = failures + 1 if not ready else 0
        if attempt < 2:
            time.sleep(5.0)
    target_alive = _target_alive(target)
    outage_confirmed = failures >= 3 and target_alive
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary, target=target,
         consecutive_failures=failures, reasons=reasons, target_alive=target_alive)
    if not outage_confirmed:
        return {"ok": False, "error": "outage_not_confirmed", "primary": primary,
                "target": target}

    outage = _latest_outage(primary)
    incident_ts = time.time()
    approved = confirm(auto, f"Region {primary} da loi. Failover sang region {target}?")
    step(2, "thong_bao_incident", ok=approved, primary=primary, target=target,
         outage_ts=outage.get("ts") if outage else None,
         outage_iso=outage.get("iso") if outage else None,
         operator_notified_ts=incident_ts,
         notification_delay_s=(round(incident_ts - outage["ts"], 3) if outage else None),
         auto=auto)
    if not approved:
        return {"ok": False, "error": "operator_cancelled", "primary": primary,
                "target": target}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=failover_result.get("ok", False),
         failover_result=failover_result)

    target_state = failover_result.get("target_state") or {}
    restore = failover_result.get("restore") or {}
    replica_ok = bool(target_state.get("weights") and target_state.get("count", 0) > 0)
    step(4, "verify_state_replica", ok=replica_ok, target=target,
         vector_count=target_state.get("count"), weights=target_state.get("weights"),
         rpo_seconds=restore.get("rpo_seconds"), docs_lost=restore.get("docs_lost"),
         embed_model_version=restore.get("embed_model_version"))

    cutover_ok = bool(failover_result.get("ok") and
                      failover_result.get("active_region") == target)
    step(5, "dns_cutover", ok=cutover_ok, active_region=failover_result.get("active_region"),
         target=target)

    latencies = []
    errors = 0
    if cutover_ok:
        for _ in range(10):
            request_started = time.monotonic()
            try:
                response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
                body = response.json()
                if response.status_code != 200 or body.get("error"):
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.monotonic() - request_started) * 1000)
    else:
        errors = 10
    ordered = sorted(latencies)
    p95 = (round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 2)
           if ordered else None)
    error_rate = round(errors / 10, 3)
    golden_ok = cutover_ok and errors == 0
    step(6, "verify_golden_signals", ok=golden_ok, requests=10,
         errors=errors, error_rate=error_rate, p95_latency_ms=p95)

    elapsed = round(time.time() - started, 3)
    overall_ok = bool(failover_result.get("ok") and replica_ok and golden_ok)
    measure_command = ("python tools/measure_rto.py --loadgen "
                       "reports/drill-2-withdr.jsonl --target-rto 300")
    step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed,
         measure_command=measure_command)
    return {"ok": overall_ok, "primary": primary, "target": target,
            "elapsed_s": elapsed, "failover": failover_result,
            "golden_signals": {"requests": 10, "errors": errors,
                               "error_rate": error_rate, "p95_latency_ms": p95}}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
