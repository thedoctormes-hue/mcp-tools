#!/usr/bin/env python3
"""
heartbeat-pull.py — thin CLI wrapper that hits the heartbeat MCP server over
HTTP and prints ONLY the agent's summary_text.

Why this exists: OpenClaw isolated cron sessions in this deployment do NOT
execute MCP tools (the gateway does not route heartbeat__pull there), but they
DO allow `exec`. So the "dumb heartbeat" runs THIS script via exec; the script
calls the server, the server logs the pull in audit.log (proof), and prints the
summary for the agent to relay to Telegram.

Usage:  heartbeat-pull.py <agent>
Prints: summary_text (the "Собор сердца" line + ✅/🔴 checklist)
On error: prints a short "heartbeat unavailable" note (non-zero exit).
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8088/mcp"
AGENTS = {
    "kotolizator", "mangust", "raven", "owl",
    "bestia", "streikbrecher", "dominika", "antcat",
}


def _rpc(method, params=None, sid=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(BASE, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    resp = urllib.request.urlopen(req, timeout=10)
    sid = resp.headers.get("Mcp-Session-Id") or sid
    raw = resp.read().decode()
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip()), sid
            except Exception:
                pass
    return None, sid


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in AGENTS:
        print("heartbeat unavailable: unknown agent")
        sys.exit(1)
    agent = sys.argv[1]
    try:
        _, sid = _rpc("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "hb-pull", "version": "1.0"}})
        res, _ = _rpc("tools/call", {"name": "pull", "arguments": {"agent": agent}}, sid)
        payload = json.loads(res["result"]["content"][0]["text"])
        print(payload["summary_text"])
    except Exception as e:  # noqa
        # Graceful degradation (ЗавЛаб requirement): when the server is down,
        # do NOT crash, do NOT auto-restart it, do NOT re-call. Print clear
        # recovery instructions addressed to the HUMAN OPERATOR, not the agent.
        print("⚠️ heartbeat-сервер недоступен (127.0.0.1:8088) — ДЛЯ ОПЕРАТОРА:")
        print("Поднять: systemctl start mcp-heartbeat.service")
        print("Перепроверить: python3 /root/LabDoctorM/projects/mcp-tools/bin/heartbeat-pull.py " + agent)
        sys.exit(1)


if __name__ == "__main__":
    main()
