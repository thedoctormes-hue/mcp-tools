#!/usr/bin/env python3
"""P5-2: gateway_health alert.

Дёргает ``memory_gateway.server.gateway_health()`` и при аномалии пишет
алерт в лог + файл и выходит с кодом 1 (systemd зафиксирует failed,
а timer отметит проблему). Без аномалий — печатает OK и выходит 0.

Пороги:
  - ``ok`` должен быть True (токен жив, lexical.db читается, ALM отвечает);
  - p95 latency ALM не выше LATENCY_P95_THRESHOLD_MS.
"""
import json
import os
import sys
import time

SYS_PATH = "/root/LabDoctorM/projects/mcp-tools/memory-gateway"
sys.path.insert(0, SYS_PATH)

from memory_gateway import server  # noqa: E402

ALERT_LOG = "/root/LabDoctorM/.ops/shared/memory-gateway-health/alerts.log"
LATENCY_P95_THRESHOLD_MS = 2000.0  # порог p95 латентности ALM
VECTOR_COUNT_MIN = 100  # минимум векторов (если меньше — индекс пуст/деградировал)
VECTOR_COUNT_DROP_PCT = 30  # падение >30% от baseline — alert
VECTOR_BASELINE_FILE = "/root/LabDoctorM/.ops/shared/memory-gateway-health/vector_baseline.json"


def _alert(msg, detail):
    line = (
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ALERT: {msg} | "
        f"{json.dumps(detail, ensure_ascii=False)[:500]}\n"
    )
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        f.write(line)
    sys.stderr.write(line)


def main():
    try:
        h = server.gateway_health()
    except Exception as e:  # noqa: BLE001
        _alert("gateway_health raised exception", {"error": str(e)})
        return 1

    problems = []
    if not h.get("ok"):
        problems.append("ok=False")
    lat = h.get("latency") or {}
    p95 = lat.get("p95_ms")
    if p95 is not None and p95 > LATENCY_P95_THRESHOLD_MS:
        problems.append(f"ALM p95 latency {p95}ms > {LATENCY_P95_THRESHOLD_MS}ms")
    # P: vector_count check
    vl = h.get("vector_layer", {})
    vc = vl.get("vector_count")
    if vc is not None:
        if vc < VECTOR_COUNT_MIN:
            problems.append(f"vector_count={vc} < {VECTOR_COUNT_MIN} (индекс пуст/деградировал)")
        # baseline drop check
        baseline = None
        try:
            if os.path.exists(VECTOR_BASELINE_FILE):
                with open(VECTOR_BASELINE_FILE) as f:
                    baseline = json.load(f).get("vector_count")
        except Exception:
            pass
        if baseline and baseline > VECTOR_COUNT_MIN:
            drop_pct = (baseline - vc) / baseline * 100
            if drop_pct > VECTOR_COUNT_DROP_PCT:
                problems.append(f"vector_count упал на {drop_pct:.0f}% ({baseline} → {vc})")
    # save baseline if healthy
    if not problems and vc is not None:
        try:
            os.makedirs(os.path.dirname(VECTOR_BASELINE_FILE), exist_ok=True)
            with open(VECTOR_BASELINE_FILE, "w") as f:
                json.dump({"vector_count": vc, "ts": time.time()}, f)
        except Exception:
            pass

    if problems:
        _alert("; ".join(problems), h)
        return 1

    print("OK", json.dumps(h, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
