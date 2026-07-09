#!/usr/bin/env python3
"""shell-server.py — MCP server for CONTROLLED command execution (hardened, whitelist-only).

WARNING: this is an RCE surface by design. It is locked down with:
- a strict command whitelist (only specific binaries + arg patterns)
- a forbidden-pattern deny list (rm, dd, pipes, vault paths, sudo, etc.)
- no shell chaining (; | && || $() `), subprocess uses shlex.split (no shell=True)
- per-call audit log to /var/log/mcp-shell-audit.log
- systemd hardening (MemoryMax, NoNewPrivileges, ProtectSystem, IPAddressDeny)

Agents can: read status, restart/start/stop only mcp-* units, read project/workspace
files, read logs, check disk/free/docker. They CANNOT: delete, touch vault, change
passwords, reach network, or touch critical units (openclaw-gateway, dockerd).
"""
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

AUDIT_LOG = "/var/log/mcp-shell-audit.log"

# Allowed binary -> regex the FULL command must match.
# Write actions (restart/start/stop/reload) are limited to mcp-* units only.
ALLOWED = {
    "systemctl": r"^systemctl (status|is-active|is-enabled) [A-Za-z0-9_@.\-]+$",
    "journalctl": r"^journalctl( (--no-pager|-u [A-Za-z0-9_@.\-]+|-n [0-9]{1,5}|-p (err|warning|info)))*$",
    "df": r"^df -h$",
    "free": r"^free -h$",
    "docker": r"^docker ps(--format [^|;&]+)?$",
    "systemctl-write": r"^systemctl (restart|start|stop|reload) mcp-[A-Za-z0-9_@.\-]+$",
    "cat": r"^cat /root/LabDoctorM/(projects|workspaces)/[A-Za-z0-9_/.\-]+$",
    "head": r"^head -n [0-9]{1,5} /root/LabDoctorM/(projects|workspaces)/[A-Za-z0-9_/.\-]+$",
    "tail": r"^tail(-n [0-9]{1,5})? /root/LabDoctorM/(projects|workspaces)/[A-Za-z0-9_/.\-]+$",
}

# Forbidden substrings / patterns — deny takes precedence over allow.
DENY = [
    r";", r"\|", r"&&", r"\|\|", r"\$\(", r"`",
    r"\brm\b", r"\bdd\b", r"mkfs", r"shutdown", r"\breboot\b",
    r"\bsudo\b", r"\bsu\b", r"passwd", r"chpasswd",
    r"curl", r"wget", r"\bnc\b", r"ncat",
    r"/root/LabDoctorM/vault", r"\.env\b", r"openclaw\.json",
    r"/etc/shadow", r"/etc/passwd",
    r"openclaw-gateway", r"dockerd",
]


def _audit(command: str, allowed: bool, reason: str, rc=None, out_len=0, err_len=0):
    ts = datetime.now(timezone.utc).isoformat()
    line = (
        f"{ts} allowed={allowed} reason={reason} rc={rc} "
        f"out={out_len} err={err_len} cmd={command!r}\n"
    )
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


def check(command: str):
    if not command or not command.strip():
        return False, "empty"
    for p in DENY:
        if re.search(p, command):
            return False, f"forbidden pattern: {p}"
    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "invalid quoting"
    if not parts:
        return False, "empty"
    base = parts[0]
    # write actions use the "systemctl-write" key
    key = "systemctl-write" if (base == "systemctl" and len(parts) >= 2 and parts[1] in ("restart", "start", "stop", "reload")) else base
    if key in ALLOWED and re.match(ALLOWED[key], command):
        return True, "whitelisted"
    return False, f"command not whitelisted: {base}"


mcp = FastMCP(
    "shell-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8089")),
)


@mcp.tool()
def list_allowed() -> dict:
    """Return the whitelist of allowed command patterns (agents self-limit)."""
    return {
        "allowed_patterns": {k: v for k, v in ALLOWED.items()},
        "denied_substrings": DENY,
        "note": "Only whitelisted binary+arg patterns run. Everything else is denied and audit-logged.",
    }


@mcp.tool()
def execute(command: str, timeout: int = 30) -> dict:
    """Execute a whitelisted shell command. Returns stdout/stderr/returncode.

    DANGER: RCE surface — locked by whitelist + deny patterns + systemd hardening.
    No pipes/chains, no rm/dd/sudo, no vault/env paths, no network, no critical units.
    """
    allowed, reason = check(command)
    if not allowed:
        _audit(command, False, reason)
        return {"allowed": False, "reason": reason, "stdout": "", "stderr": "", "returncode": None}
    try:
        proc = subprocess.run(
            shlex.split(command),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        _audit(command, True, reason, proc.returncode, len(proc.stdout), len(proc.stderr))
        return {
            "allowed": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout[:8000],
            "stderr": proc.stderr[:2000],
        }
    except subprocess.TimeoutExpired:
        _audit(command, True, "timeout", None)
        return {"allowed": True, "timeout": True, "reason": f"exceeded {timeout}s", "stdout": "", "stderr": ""}
    except Exception as e:  # noqa: BLE001
        return {"allowed": True, "error": str(e)}


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
