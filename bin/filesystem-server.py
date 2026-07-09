#!/usr/bin/env python3
"""
Filesystem MCP Server — read-only access to whitelisted directories.

Uses official MCP SDK (FastMCP) with stdio transport.
"""

import os
import glob
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
ALLOWED_ROOTS = [
    Path("/root/LabDoctorM/workspaces/").resolve(),
    Path("/root/LabDoctorM/projects/").resolve(),
]
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB

mcp = FastMCP(
    "filesystem-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8083")),
)


def _resolve_safe(path: str) -> Path:
    """Resolve and validate path against whitelist. Block path traversal."""
    # Block obvious traversal attempts in raw input
    if ".." in path:
        raise ValueError(f"Path traversal blocked: {path}")

    resolved = Path(path).resolve()

    # Ensure path is under one of the allowed roots
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    raise PermissionError(
        f"Access denied: {resolved} is outside allowed directories. "
        f"Allowed: {[str(r) for r in ALLOWED_ROOTS]}"
    )


@mcp.tool()
def read_file(path: str) -> str:
    """Read a file's contents. Read-only. Max 1MB. Only whitelisted dirs."""
    safe_path = _resolve_safe(path)

    if not safe_path.is_file():
        raise FileNotFoundError(f"Not a file: {safe_path}")

    size = safe_path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(
            f"File too large ({size} bytes). Max allowed: {MAX_FILE_SIZE} bytes (1MB)."
        )

    return safe_path.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def list_dir(path: str) -> str:
    """List directory contents (names only). Only whitelisted dirs."""
    safe_path = _resolve_safe(path)

    if not safe_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {safe_path}")

    entries = sorted(os.listdir(safe_path))
    if not entries:
        return "(empty directory)"

    return "\n".join(entries)


@mcp.tool()
def search_files(pattern: str, path: str) -> str:
    """Search files by glob pattern under a given path. Only whitelisted dirs."""
    safe_path = _resolve_safe(path)

    if not safe_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {safe_path}")

    # Block traversal inside glob pattern too
    if ".." in pattern:
        raise ValueError(f"Path traversal blocked in pattern: {pattern}")

    matches = sorted(glob.glob(str(safe_path / pattern), recursive=True))

    if not matches:
        return "(no matches)"

    # Return paths relative to search root for readability
    return "\n".join(
        str(Path(m).relative_to(safe_path)) for m in matches
    )


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
