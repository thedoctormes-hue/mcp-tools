"""Unit tests for memory-server revalidation logic.

Scope: unit-level only. The live MCP server is NOT started. We exercise the
lazy check-on-request machinery that replaced the old background poll-watchdog:

- ``_disk_index_changed`` — pure mtime comparison (disk vs in-memory).
- ``_ensure_fresh_index`` — reloads only when disk is newer, under a lock with
  a double-check so two concurrent requests don't reload twice.

The real ``load_index`` (which reads FAISS from disk) is mocked; disk mtime is
controlled via ``os.path.getmtime`` monkeypatching.
"""

import importlib.util
import os
import threading
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY = REPO_ROOT / "bin" / "memory-server.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    """Fresh import of memory-server with no __main__ side effects."""
    m = _load_module(MEMORY, "memory_server_under_test")
    # Reset in-memory state to a known baseline.
    with m._state_lock:
        m._state["index_mtime"] = 100.0
        m._state["meta_mtime"] = 100.0
        m._state["index"] = object()  # non-None so "have" is True
        m._state["ready"] = True
    return m


def test_disk_index_changed_false_when_disk_same_or_older(mod):
    # disk mtime <= in-memory -> no change
    with mock.patch("os.path.getmtime", return_value=100.0):
        assert mod._disk_index_changed() is False
    with mock.patch("os.path.getmtime", return_value=50.0):
        assert mod._disk_index_changed() is False


def test_disk_index_changed_true_when_disk_newer(mod):
    with mock.patch("os.path.getmtime", return_value=200.0):
        assert mod._disk_index_changed() is True


def test_ensure_fresh_skips_reload_when_unchanged(mod):
    calls = []
    mod.load_index = lambda: calls.append(1) or True
    with mock.patch("os.path.getmtime", return_value=100.0):
        result = mod._ensure_fresh_index()
    assert result is False
    assert calls == []  # reload NOT triggered


def test_ensure_fresh_reloads_once_when_disk_newer(mod):
    calls = []
    # Simulate real load_index: update in-memory mtime to the (new) disk value.
    def fake_load():
        calls.append(1)
        with mod._state_lock:
            mod._state["index_mtime"] = 200.0
            mod._state["meta_mtime"] = 200.0
        return True

    mod.load_index = fake_load
    with mock.patch("os.path.getmtime", return_value=200.0):
        result = mod._ensure_fresh_index()
    assert result is True
    assert calls == [1]  # exactly one reload


def test_ensure_fresh_concurrent_no_double_reload(mod):
    """Two threads both see 'changed' before the lock; only one reloads."""
    calls = []
    barrier = threading.Barrier(2)

    def fake_load():
        calls.append(1)
        # Update in-memory mtime so the double-check under lock sees "fresh".
        with mod._state_lock:
            mod._state["index_mtime"] = 200.0
            mod._state["meta_mtime"] = 200.0
        return True

    mod.load_index = fake_load

    def worker():
        barrier.wait()  # both threads enter together
        mod._ensure_fresh_index()

    with mock.patch("os.path.getmtime", return_value=200.0):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert calls == [1], f"expected exactly 1 reload, got {len(calls)}"
