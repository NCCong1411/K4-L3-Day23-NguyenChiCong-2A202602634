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


def emit(**kw):
    """Append một dòng JSONL có ts + iso vào LOG và in ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"ts": now,
              "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def state_of(region: str) -> dict:
    """Đọc state phục vụ của một region, trả lỗi có cấu trúc nếu không truy cập được."""
    try:
        response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"region": region, "error": f"{type(exc).__name__}: {exc}"}


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện năm bước failover theo đúng thứ tự an toàn."""
    primary = "b" if target == "a" else "a"
    result = {"ok": False, "primary": primary, "target": target, "backend": backend}

    target_before = state_of(target)
    emit(step="1_verify_target", ok="error" not in target_before, target=target,
         target_state=target_before)
    result["target_before"] = target_before

    restore_started = time.monotonic()
    try:
        restored = snapshot.get(target, backend)
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    except (Exception, SystemExit) as exc:
        emit(step="2_restore_snapshot", ok=False, target=target,
             error=f"{type(exc).__name__}: {exc}")
        result["error"] = f"restore_failed: {exc}"
        return result

    restore_info = {**restored, **rpo,
                    "restore_duration_s": round(time.monotonic() - restore_started, 3)}
    emit(step="2_restore_snapshot", ok=True, target=target,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=restored.get("embed_model_version"),
         restore_duration_s=restore_info["restore_duration_s"])
    result["restore"] = restore_info

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full", encoding="utf-8")
    emit(step="3_scale_pool", ok=True, target=target, pool_state="full")

    ready_started = time.monotonic()
    deadline = ready_started + wait
    last_reason = "not_probed"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if response.status_code == 200:
                waited = round(time.monotonic() - ready_started, 3)
                ready_body = response.json()
                emit(step="4_wait_ready", ok=True, target=target, waited_s=waited,
                     ready=ready_body)
                result["ready"] = ready_body
                result["waited_s"] = waited
                break
            try:
                last_reason = ",".join(response.json().get("reasons") or [])
            except Exception:
                last_reason = f"http_{response.status_code}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    else:
        waited = round(time.monotonic() - ready_started, 3)
        emit(step="4_wait_ready", ok=False, target=target, waited_s=waited,
             reason=last_reason)
        result.update(error="target_not_ready", waited_s=waited, reason=last_reason)
        return result

    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", ok=True, target=target, active_region=target)

    result.update(ok=True, active_region=target, target_state=state_of(target))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
