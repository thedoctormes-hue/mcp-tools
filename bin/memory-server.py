#!/usr/bin/env python3
"""
MCP Memory Server — read-only wrapper over the lab semantic search (labsearch).

PAT-004: does NOT reimplement search. It shells out to the canonical
`lab_search.py` as a subprocess and returns its JSON output to the agent.
Only read access — never modifies the FAISS index or its metadata.

Uses official MCP SDK (FastMCP) with streamable-http transport when
MCP_TRANSPORT=http, otherwise stdio.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Any
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
LAB_SEARCH_SCRIPT = Path(
    "/root/LabDoctorM/projects/lab-memory/scripts/lab_search.py"
)

mcp = FastMCP(
    "memory-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8087")),
)


@mcp.tool()
def search(query: str, top_k: int = 5) -> Dict[str, Any]:
    """
    Semantic search over the lab's collective memory (FAISS index).

    Calls the canonical lab_search.py as a read-only subprocess — this server
    never touches the index itself.

    Args:
        query: Natural-language search query (e.g. "как настроить MCP сервер").
        top_k: Maximum number of results to return (default 5).

    Returns:
        query: Echoed query.
        count: Number of results returned.
        results: List of {score, id, file_path, agent, project, source, text,
                 last_modified, chunk_type}. Empty list if nothing found.
        error: Present only on failure (e.g. search script unavailable).
    """
    top_k = max(1, int(top_k))

    if not LAB_SEARCH_SCRIPT.exists():
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": f"lab_search.py not found at {LAB_SEARCH_SCRIPT}",
        }

    try:
        proc = subprocess.run(
            [
                "/usr/bin/python3",
                str(LAB_SEARCH_SCRIPT),
                "search",
                query,
                "--limit",
                str(top_k),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": "lab_search.py timed out after 60s",
        }
    except Exception as e:  # pragma: no cover - defensive
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": f"failed to run lab_search.py: {e}",
        }

    if proc.returncode != 0:
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": f"lab_search.py exited {proc.returncode}: "
            f"{proc.stderr.strip()[:300]}",
        }

    # Parse JSON from stdout; ignore any non-JSON log lines on stderr.
    try:
        results = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as e:
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": f"could not parse lab_search.py output: {e} "
            f"(stdout: {proc.stdout.strip()[:200]})",
        }

    if not isinstance(results, list):
        return {
            "query": query,
            "count": 0,
            "results": [],
            "error": "unexpected lab_search.py output shape (not a list)",
        }

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
