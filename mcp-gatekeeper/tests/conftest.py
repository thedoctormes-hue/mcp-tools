"""
Shared fixtures/helpers for mcp-gatekeeper tests.

Tests target the real server implementation in bin/mcp-gatekeeper-server.py:
  * class Gatekeeper(policy: dict, data_dir: Path, fail_fast=False)
      - .pdp(req) -> (allow: bool, reason: str)
      - .register_port / .register_timer / .register_service / .release
      - .transfer / .heartbeat / .reaper_tick / .journal(event)
  * policy loaded from policies/policy_v1.yaml (agent ids: raven, antcat, owl, ...)

Set GATEKEEPER_SERVER_PATH to point the suite at an alternate server file
(used to validate the suite against a patched copy).
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Allow tests to import the gatekeeper package directly (e.g. gatekeeper.store).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_SERVER_ENV = os.environ.get("GATEKEEPER_SERVER_PATH")
SERVER_PATH = Path(_SERVER_ENV) if _SERVER_ENV else (PROJECT_ROOT / "bin" / "mcp-gatekeeper-server.py")
POLICY_PATH = PROJECT_ROOT / "policies" / "policy_v1.yaml"

UNIT_CANDIDATES = [
    PROJECT_ROOT / "systemd" / "mcp-gatekeeper.service",
    PROJECT_ROOT / "deploy" / "systemd" / "mcp-gatekeeper.service",
]


def load_module():
    """Import the server module, or return ('error', exc) / None."""
    if not SERVER_PATH.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("mcp_gatekeeper_server", str(SERVER_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        return ("error", exc)


def load_policy():
    if yaml is None:
        pytest.skip("pyyaml not available")
    if not POLICY_PATH.exists():
        pytest.fail(f"policy file missing: {POLICY_PATH}")
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def find_unit_path():
    for cand in UNIT_CANDIDATES:
        if cand.exists():
            return cand
    return None


def parse_unit(path):
    sections, cur = {}, None
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = m.group(1)
            sections.setdefault(cur, {})
            continue
        if "=" in line and cur is not None:
            k, v = line.split("=", 1)
            sections[cur][k.strip()] = v.strip()
    return sections


@pytest.fixture
def gk(tmp_path):
    """Fresh Gatekeeper with real policy and an isolated temp data dir."""
    mod = load_module()
    if mod is None:
        pytest.fail(f"server not implemented — BLOCKER: missing {SERVER_PATH}")
    if isinstance(mod, tuple) and mod[0] == "error":
        pytest.fail(f"server import failed — BLOCKER: {mod[1]!r}")
    Gatekeeper = getattr(mod, "Gatekeeper", None)
    assert Gatekeeper is not None, "server module has no Gatekeeper class"
    return Gatekeeper(load_policy(), tmp_path, fail_fast=False)


@pytest.fixture
def unit_path():
    p = find_unit_path()
    if p is None:
        pytest.fail(
            "systemd unit not found — BLOCKER: expected "
            + " or ".join(str(c) for c in UNIT_CANDIDATES)
        )
    return p
