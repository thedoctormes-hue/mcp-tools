#!/usr/bin/env python3
"""
MCP Status Server — "приборный щит" лаборатории (lab health aggregator).

Read-only aggregator of lab status: systemd units, docker containers,
disk usage, recent incidents. All tools are strictly read-only; no
system state is modified.

Uses official MCP SDK (FastMCP). Transport selected via MCP_TRANSPORT env:
  http  -> streamable-http (systemd)
  stdio -> stdio (default / local)
"""

import os
import subprocess
from typing import Dict, Any, List
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
INCIDENTS_DIR = "/root/LabDoctorM/projects/DoctorM_and_Ai/incidents"

mcp = FastMCP(
    "status-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8088")),
)


def _run(cmd: List[str], timeout: int = 15) -> str:
    """Run a system command via subprocess and return stdout (best-effort).

    PAT-004: delegate to system commands; never infer state ourselves.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.stdout
    except FileNotFoundError:
        return f"COMMAND_NOT_FOUND: {' '.join(cmd)}"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001 - best-effort aggregation
        return f"ERROR: {e}"


def _parse_unit_names(raw: str) -> List[str]:
    """Extract systemd unit names (the .service token) from `systemctl list-units` output.

    Does NOT determine state itself — systemctl is the authoritative source (PAT-004).
    """
    names: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # skip header + legend lines produced by systemctl
        if s.startswith(("UNIT", "LOAD", "ACTIVE", "SUB", "To ", "Spare", "lines")):
            continue
        for tok in s.split():
            if tok.endswith(".service"):
                names.append(tok.replace("\u25cf", "").strip())
                break
    return names


@mcp.tool()
def get_systemd_status() -> Dict[str, Any]:
    """
    Get failed and running systemd service units.

    Returns:
        failed: list of failed .service unit names
        running: list of running .service unit names
        failed_count / running_count: number of units in each list
        raw_failed / raw_running: raw `systemctl list-units` output (for fact-check)

    Read-only. Uses `systemctl list-units`; does not parse process state itself (PAT-004).
    """
    failed_raw = _run(
        ["systemctl", "list-units", "--type=service", "--state=failed", "--no-pager"]
    )
    running_raw = _run(
        ["systemctl", "list-units", "--type=service", "--state=running", "--no-pager"]
    )

    failed = _parse_unit_names(failed_raw)
    running = _parse_unit_names(running_raw)

    return {
        "failed": failed,
        "failed_count": len(failed),
        "running": running,
        "running_count": len(running),
        "raw_failed": failed_raw,
        "raw_running": running_raw,
    }


@mcp.tool()
def get_docker_status() -> Dict[str, Any]:
    """
    Get running docker containers.

    Returns:
        containers: list of "Name: Status" strings (from `docker ps`)
        count: number of running containers
        raw: raw `docker ps --format` output (for fact-check)

    Read-only. Uses `docker ps`; no container state inference.
    """
    raw = _run(["docker", "ps", "--format", "{{.Names}}: {{.Status}}"])
    containers = [ln for ln in raw.splitlines() if ln.strip()]
    return {
        "containers": containers,
        "count": len(containers),
        "raw": raw,
    }


@mcp.tool()
def get_disk_usage() -> Dict[str, Any]:
    """
    Get disk usage of root filesystem.

    Returns:
        filesystem: mount target ("/")
        output: raw `df -h /` output (parsed as-is)
        lines: list of output lines

    Read-only. Uses `df -h /`; no filesystem scanning.
    """
    raw = _run(["df", "-h", "/"])
    return {
        "filesystem": "/",
        "output": raw,
        "lines": [ln for ln in raw.splitlines() if ln.strip()],
    }


@mcp.tool()
def get_incidents() -> Dict[str, Any]:
    """
    Get 10 most recent incident files (excluding README/template).

    Returns:
        incidents: list of incident filenames
        count: number of incidents
        directory: incidents directory path
        raw: raw command output (for fact-check)

    Read-only. Uses `ls` + `grep` + `tail`; no file modification.
    """
    raw = _run(
        [
            "bash",
            "-c",
            f"ls -1 {INCIDENTS_DIR}/ | grep -v -E 'README|template' | tail -10",
        ]
    )
    incidents = [ln for ln in raw.splitlines() if ln.strip()]
    return {
        "directory": INCIDENTS_DIR,
        "incidents": incidents,
        "count": len(incidents),
        "raw": raw,
    }


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
