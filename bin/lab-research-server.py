#!/usr/bin/env python3
"""Lab Research MCP — search & deep-research front-door for OpenClaw agents.

FastMCP server exposing two tools:
  - web_search:    raw search via SearXNG (Unified Search Gateway, :8889)
  - deep_research: full deep research via the /research orchestrator
                   (verify + merge + synthesis + cache + freshness)

Transport is selected via MCP_TRANSPORT (http | stdio). Conventions follow
the other mcp-tools servers (FastMCP, read-only, env-driven config).

PAT-005: no facts invented; we only forward to the gateway/orchestrator and
return their output. We never claim a result is verified — that is the
orchestrator's `verify` job.
"""
import os
import json
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP

# --- Configuration (env-driven, sane defaults) ---
SEARXNG = os.environ.get("SEARXNG_URL", "http://localhost:8889/search")
ORCHESTRATOR = os.environ.get(
    "LABSEARCH_ORCHESTRATOR",
    "/root/LabDoctorM/projects/free-api-hunter/scripts/search-orchestrator.sh",
)
# Optional engine/category filter for web_search. Empty => gateway default
# (the `general` category, i.e. all active engines incl. paid pools + free).
ENGINES = os.environ.get("LABSEARCH_ENGINES", "")

mcp = FastMCP(
    "lab-research",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8089")),
)


def _search(query: str, max_results: int, engines: str) -> List[Dict[str, Any]]:
    """Call the SearXNG gateway and return normalized result dicts."""
    params = {"q": query, "format": "json"}
    if engines:
        params["engines"] = engines
    url = f"{SEARXNG}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    results = data.get("results", [])[:max_results]
    return [
        {
            "title": res.get("title", ""),
            "url": res.get("url", ""),
            "content": (res.get("content") or "")[:400],
        }
        for res in results
    ]


@mcp.tool()
def web_search(query: str, max_results: int = 10, engines: str = "") -> Dict[str, Any]:
    """Web search via the Unified Search Gateway (SearXNG).

    Args:
        query: search query
        max_results: maximum number of results to return (default 10)
        engines: optional engine/category filter, e.g. "general",
                 "exa,tavily" (default: gateway default = all active engines)

    Returns:
        count: number of results
        results: list of {title, url, content}
        error: present only on failure
    """
    try:
        eff_engines = engines or ENGINES
        out = _search(query, max_results, eff_engines)
        return {"count": len(out), "results": out}
    except Exception as e:  # noqa: BLE001 - surface gateway errors to caller
        return {"error": str(e), "count": 0, "results": []}


@mcp.tool()
def deep_research(query: str, count: int = 10) -> Dict[str, Any]:
    """Deep research via the /research orchestrator.

    Runs `search-orchestrator.sh <query> deep_research <count>` which fans out
    across all providers (Tavily/Firecrawl/TinyFish/SearXNG), merges + dedups,
    applies freshness scoring and contradiction detection, and returns the
    synthesized answer with metadata.

    Args:
        query: research question
        count: number of results per provider (default 10)

    Returns:
        answer: synthesized research output (string)
        error: present only on failure
    """
    try:
        proc = subprocess.run(
            [ORCHESTRATOR, "deep_research", query, str(count)],
            capture_output=True,
            text=True,
            timeout=240,
        )
        out = proc.stdout.strip() or proc.stderr.strip()
        return {"answer": out or "No research output."}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
