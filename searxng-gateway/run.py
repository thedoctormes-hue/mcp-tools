#!/usr/bin/env python3
"""Точка входа для systemd (streamable-http режим)."""
import os
os.environ.setdefault("SEARXNG_TRANSPORT", "streamable-http")

from searxng_gateway.server import mcp  # noqa: E402

if __name__ == "__main__":
    transport = os.getenv("SEARXNG_TRANSPORT", "streamable-http")
    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
