#!/usr/bin/env python3
"""shell-server.py — FULL (unrestricted) MCP command-execution server.

This is a FULL RCE surface — equivalent to the agent's native `exec` tool,
but routed through a single audited MCP channel with systemd resource limits.

- NO command whitelist/deny: the agent can run anything (pipes, chains, rm, etc.)
- EVERY call is audit-logged to /var/log/mcp-shell-audit.log
- systemd unit caps memory (MemoryMax) and blocks privilege escalation
  (NoNewPrivileges), but allows network + filesystem writes (parity with exec)

Use with the understanding that this is as powerful as handing the agent a shell.
"""
import os
import subprocess
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

AUDIT_LOG = "/var/log/mcp-shell-audit.log"


def _audit(command: str, cwd: str, rc=None, out_len=0, err_len=0, exc=None):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} cmd={command!r} cwd={cwd} rc={rc} out={out_len} err={err_len} exc={exc}\n"
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


mcp = FastMCP(
    "shell-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8089")),
)


@mcp.tool()
def info() -> dict:
    """Describe this server's mode (full/unrestricted)."""
    return {
        "mode": "full RCE (unrestricted)",
        "audit_log": AUDIT_LOG,
        "warning": "Equivalent to the native exec tool. No command restrictions.",
    }


@mcp.tool()
def execute(command: str, timeout: int = 60, cwd: str = "/root/LabDoctorM") -> dict:
    """Execute ANY shell command (full RCE, like the native exec tool).

    Pipes, chains, redirects allowed. Audited. Runs with the server's privileges.
    DANGER: this is as powerful as a shell — no restrictions are applied.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        _audit(command, cwd, proc.returncode, len(proc.stdout), len(proc.stderr))
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[:16000],
            "stderr": proc.stderr[:4000],
        }
    except subprocess.TimeoutExpired:
        _audit(command, cwd, None, exc="timeout")
        return {"timeout": True, "reason": f"exceeded {timeout}s"}
    except Exception as e:  # noqa: BLE001
        _audit(command, cwd, exc=str(e))
        return {"error": str(e)}


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
