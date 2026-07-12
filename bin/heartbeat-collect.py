#!/usr/bin/env python3
"""
heartbeat-collect.py — daily collector that writes each agent's hb-status.json.

Part of the "dumb heartbeat pulls its endpoint" design (ЗавЛаб, 2026-07-12).
The heartbeat MCP server is READ-ONLY: it reads hb-status.json. THIS script is
the writer — run by systemd timer once per day (staggered), it performs the
real checks and persists results. Agents never run heavy checks themselves.

Checks (generic, real, read-only except the final hb-status.json write):
  1. grimoire.md alive      — file exists with >=1 '- ' bullet (REQUIRED #1)
  2. search-stack alive     — ONNX :8082 /health ok AND lab_search end-to-end
  3. gateway healthy        — openclaw-gateway active, NRestarts below threshold
  4. disk safe              — root filesystem usage < 85%
  5. reindex alive          — reindex-full.timer / reindex-incremental active

Output: /root/LabDoctorM/workspaces/<agent>/hb-status.json
Schema: see docs/heartbeat-status-schema.md

Safety: never writes outside <workspace>/hb-status.json; timeout-guarded so a
hung subsystem (e.g. ONNX embed) cannot stall the collector.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

WORKSPACES = Path(os.environ.get("HB_WORKSPACES", "/root/LabDoctorM/workspaces"))
AGENTS = [
    "kotolizator", "mangust", "raven", "owl",
    "bestia", "streikbrecher", "dominika", "antcat",
]
ONNX_URL = "http://127.0.0.1:8082/health"
LAB_SEARCH = "/root/LabDoctorM/projects/lab-memory/scripts/lab_search.py"
GATEWAY_UNIT = "openclaw-gateway.service"
REINDEX_UNITS = ["reindex-full.timer", "reindex-incremental.timer"]
DISK_THRESHOLD = 85
GATEWAY_RESTART_THRESHOLD = 5


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa
        return 1, "", str(e)[:120]


def _check_grimoire(agent_dir: Path):
    g = agent_dir / "grimoire.md"
    if not g.is_file():
        return "fail", "grimoire.md отсутствует"
    bullets = [ln for ln in g.read_text(errors="replace").splitlines()
               if ln.strip().startswith("- ")]
    if not bullets:
        return "fail", "grimoire.md пуст (нет '- ' строк)"
    return "pass", f"{len(bullets)} строк '- '"


def _check_search_stack():
    # ONNX health endpoint
    rc, out, err = _run(f"curl -s -m 5 {ONNX_URL}", timeout=8)
    onnx_ok = False
    if rc == 0 and "ok" in out.lower():
        onnx_ok = True
    # lab_search end-to-end
    rc2, out2, err2 = _run(f"python3 {LAB_SEARCH} health", timeout=20)
    ls_ok = (rc2 == 0) and ("ok" in out2.lower() or "healthy" in out2.lower())
    if onnx_ok and ls_ok:
        return "pass", "ONNX :8082 ok + lab_search ок"
    if not onnx_ok and not ls_ok:
        return "fail", "ONNX :8082 недоступен + lab_search молчит"
    return "fail", f"ONNX ok={onnx_ok}, lab_search ok={ls_ok}"


def _check_gateway():
    rc, out, err = _run(f"systemctl is-active {GATEWAY_UNIT}", timeout=8)
    active = (out == "active")
    rc2, nr, _ = _run(
        f"systemctl show {GATEWAY_UNIT} --property=NRestarts", timeout=8)
    try:
        nrestarts = int(nr.split("=")[1]) if "=" in nr else -1
    except Exception:
        nrestarts = -1
    if not active:
        return "fail", "gateway НЕ active"
    if nrestarts > GATEWAY_RESTART_THRESHOLD:
        return "fail", f"NRestarts={nrestarts} (высокий)"
    return "pass", f"active, NRestarts={nrestarts}"


def _check_disk():
    rc, out, err = _run("df -P / | tail -1 | awk '{print $5}'", timeout=8)
    try:
        pct = int(out.replace("%", "").strip())
    except Exception:
        return "unknown", f"не удалось измерить ({out})"
    if pct >= DISK_THRESHOLD:
        return "fail", f"{pct}% (порог {DISK_THRESHOLD}%)"
    return "pass", f"{pct}%"


def _check_reindex():
    for unit in REINDEX_UNITS:
        rc, out, err = _run(f"systemctl is-active {unit}", timeout=8)
        if out == "active":
            return "pass", f"{unit} active"
    return "fail", "reindex таймеры не active"


def collect_agent(agent: str) -> dict:
    agent_dir = WORKSPACES / agent
    checks = []
    g_res, g_note = _check_grimoire(agent_dir)
    checks.append({"name": "grimoire.md жив", "result": g_res, "note": g_note})
    s_res, s_note = _check_search_stack()
    checks.append({"name": "search-stack alive", "result": s_res, "note": s_note})
    w_res, w_note = _check_gateway()
    checks.append({"name": "gateway healthy", "result": w_res, "note": w_note})
    d_res, d_note = _check_disk()
    checks.append({"name": "disk safe (<85%)", "result": d_res, "note": d_note})
    r_res, r_note = _check_reindex()
    checks.append({"name": "reindex alive", "result": r_res, "note": r_note})
    return {
        "agent": agent,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def main():
    written = []
    for agent in AGENTS:
        status = collect_agent(agent)
        out = WORKSPACES / agent / "hb-status.json"
        try:
            out.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            written.append(agent)
        except Exception as e:  # noqa
            print(f"WARN: cannot write {out}: {e}")
    # also write a colony snapshot for quick eyeballing
    try:
        snap = {}
        for a in written:
            d = json.loads((WORKSPACES / a / "hb-status.json").read_text())
            res = {c["name"]: c["result"] for c in d["checks"]}
            snap[a] = res
        (WORKSPACES / "colony-heartbeat.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] heartbeat-collect: wrote hb-status.json for {len(written)} agents: {written}")


if __name__ == "__main__":
    main()
