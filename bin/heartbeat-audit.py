#!/usr/bin/env python3
"""
heartbeat-audit.py — attendance report from the heartbeat server audit journal.

Reads the append-only audit.log written by heartbeat-server.py and answers:
  * who pulled their heartbeat endpoint and WHEN (last seen)
  * who is MISSING (no pull in the last N hours) — the "proof" gap
  * per-agent call counts over a window

READ-ONLY: only reads audit.log(+rotations). No writes, no service calls.

Usage:
  heartbeat-audit.py                 # default: last 24h attendance
  heartbeat-audit.py --hours 48      # window = 48h
  heartbeat-audit.py --agent raven   # focus one agent
  heartbeat-audit.py --json          # machine-readable
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone

AUDIT_DIR = os.environ.get("HB_AUDIT_DIR", "/root/LabDoctorM/.ops/mcp-heartbeat")
AUDIT_GLOB = os.path.join(AUDIT_DIR, "audit.log*")
AGENTS = [
    "kotolizator", "mangust", "raven", "owl",
    "bestia", "streikbrecher", "dominika", "antcat",
]
# Tools that count as an agent "showing up" for its own heartbeat.
PULL_TOOLS = {"pull", "resource"}


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_records():
    recs = []
    for path in sorted(glob.glob(AUDIT_GLOB)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    return recs


def build_report(hours=24, focus=None):
    now = datetime.now(timezone.utc)
    recs = _load_records()
    per_agent = {a: {"count": 0, "last": None, "last_overall": None}
                 for a in AGENTS}
    total_calls = len(recs)
    for r in recs:
        tool = r.get("tool")
        agent = r.get("agent")
        ts = _parse_ts(r.get("ts"))
        if tool in PULL_TOOLS and agent in per_agent:
            age_h = (now - ts).total_seconds() / 3600.0 if ts else None
            if age_h is not None and age_h <= hours:
                per_agent[agent]["count"] += 1
            # last-seen is all-time (not windowed) so we can report "N days ago"
            prev = per_agent[agent]["last"]
            if ts and (prev is None or ts > prev):
                per_agent[agent]["last"] = ts
                per_agent[agent]["last_overall"] = r.get("overall")
    present, missing = [], []
    rows = {}
    agents = [focus] if focus else AGENTS
    for a in agents:
        if a not in per_agent:
            continue
        info = per_agent[a]
        last = info["last"]
        age_h = (now - last).total_seconds() / 3600.0 if last else None
        seen_in_window = age_h is not None and age_h <= hours
        rows[a] = {
            "count_in_window": info["count"],
            "last_seen": last.isoformat() if last else None,
            "last_age_hours": round(age_h, 1) if age_h is not None else None,
            "last_overall": info["last_overall"],
            "present": seen_in_window,
        }
        (present if seen_in_window else missing).append(a)
    return {
        "generated_at": now.isoformat(),
        "window_hours": hours,
        "audit_records_total": total_calls,
        "present": present,
        "missing": missing,
        "agents": rows,
    }


def _fmt_age(h):
    if h is None:
        return "никогда"
    if h < 1:
        return f"{int(h*60)}м назад"
    if h < 48:
        return f"{h:.1f}ч назад"
    return f"{h/24:.1f}д назад"


def render(report):
    lines = []
    w = report["window_hours"]
    lines.append(f"🐾 Явки heartbeat — окно {w}ч "
                 f"(записей в журнале: {report['audit_records_total']})")
    lines.append(f"✅ пришли: {len(report['present'])}/"
                 f"{len(report['agents'])}  🔴 пропали: {len(report['missing'])}")
    lines.append("")
    for a, info in report["agents"].items():
        mark = "✅" if info["present"] else "🔴"
        age = _fmt_age(info["last_age_hours"])
        ov = info["last_overall"] or "—"
        lines.append(
            f"{mark} {a}: last {age} | вердикт {ov} | "
            f"вызовов за окно {info['count_in_window']}")
    if report["missing"]:
        lines.append("")
        lines.append("🔴 НЕ приходили в окне: " + ", ".join(report["missing"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = build_report(hours=args.hours, focus=args.agent)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))


if __name__ == "__main__":
    main()
