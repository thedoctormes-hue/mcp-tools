"""Real unit tests for mcp-tools servers.

Scope: unit-level logic only. Live MCP servers are NOT started in tests
(they are slow and require the systemd environment). We exercise:

- ``apikeys-server._mask_key`` — masking of long keys and safe handling of
  short/empty keys (no crash).
- Import smoke test — both server modules import without errors and their
  ``FastMCP`` objects are created.
- Transport selection — ``MCP_TRANSPORT`` env var drives ``mcp.run`` to
  ``streamable-http`` (HTTP) or ``stdio`` (default). Verified by mocking
  ``FastMCP.run`` so the server never actually binds a port.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APIKEYS = REPO_ROOT / "bin" / "apikeys-server.py"
FILESYSTEM = REPO_ROOT / "bin" / "filesystem-server.py"


def _load_module(path: Path, name: str):
    """Import a server file as a normal module (no __main__ side effects)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_as_main(path: Path, name: str):
    """Execute a server file with __name__ == '__main__' while mocking
    ``FastMCP.run`` so the server never actually starts. Returns the mock
    so callers can assert which transport was selected."""
    src = path.read_text()
    code = compile(src, str(path), "exec")
    mod = type(sys)(f"main_{name}")
    mod.__name__ = "__main__"
    with mock.patch("mcp.server.fastmcp.FastMCP.run") as run_mock:
        exec(code, mod.__dict__)
    return run_mock


# --------------------------------------------------------------------------- #
# _mask_key
# --------------------------------------------------------------------------- #

def test_mask_key_masks_long_key():
    apikeys = _load_module(APIKEYS, "apikeys_server")
    mask = apikeys._mask_key
    key = "csk-" + "A" * 30 + "xyz"  # 37 chars, head/tail of 4 each
    out = mask(key)
    assert isinstance(out, str)
    assert "*" in out
    assert out.startswith(key[:4])      # head preserved
    assert out.endswith(key[-4:])        # tail preserved
    assert len(out) == len(key)          # overall length preserved


def test_mask_key_short_keys_no_crash():
    apikeys = _load_module(APIKEYS, "apikeys_server")
    mask = apikeys._mask_key
    for key in ["", "abc", "abcdef", "abcdefgh", "x" * 4]:
        out = mask(key)                  # must not raise
        assert isinstance(out, str)


# --------------------------------------------------------------------------- #
# Import smoke test
# --------------------------------------------------------------------------- #

def test_import_servers_and_fastmcp_objects():
    apikeys = _load_module(APIKEYS, "apikeys_server")
    filesystem = _load_module(FILESYSTEM, "filesystem_server")
    assert hasattr(apikeys, "mcp")
    assert hasattr(filesystem, "mcp")
    # FastMCP exposes the server name used at construction.
    assert getattr(apikeys.mcp, "name", None) == "apikeys-server"
    assert getattr(filesystem.mcp, "name", None) == "filesystem-server"


# --------------------------------------------------------------------------- #
# Transport selection
# --------------------------------------------------------------------------- #

def test_apikeys_http_transport_selects_streamable(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    run_mock = _load_as_main(APIKEYS, "apikeys")
    run_mock.assert_called_once_with(transport="streamable-http")


def test_filesystem_stdio_transport_default(monkeypatch):
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    run_mock = _load_as_main(FILESYSTEM, "filesystem")
    run_mock.assert_called_once_with(transport="stdio")
