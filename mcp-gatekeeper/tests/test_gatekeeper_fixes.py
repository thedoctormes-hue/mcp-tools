"""Tests for the two PDP/store fixes:

Fix A (lease reaper): ``LeaseStore.reap_expired_leases`` releases leases whose
heartbeat deadline passed, and the thin ``bin/reap-leases.py`` CLI drives it.

Fix B (v2 dedup fail-closed): ``Gatekeeper.check_justification`` in v2 mode must
NOT fail-open when semantic dedup errors — it must log a WARNING and fall back
to the strict v1_exact check (reject cross-agent duplicates).
"""

import time

import pytest

from gatekeeper.store import Lease, LeaseStore

from conftest import load_module, load_policy  # reuse the suite's helpers


def _make_lease(rid, agent="raven", port=8080, timeout=86400.0, heartbeat=None):
    now = time.time() if heartbeat is None else heartbeat
    return Lease(
        request_id=rid,
        agent=agent,
        project_id="projA",
        kind="port",
        port=port,
        timer_action=None,
        timer_schedule=None,
        what_for=f"lease {rid}",
        run_as="mcp-gatekeeper",
        issued_user="mcp-gatekeeper",
        acquired_at=now,
        last_heartbeat=now,
        lease_timeout=timeout,
        bypass=None,
        unit=None,
    )


# --------------------------------------------------------------------------- #
# Fix A — store-level reaper
# --------------------------------------------------------------------------- #
def test_reap_expired_leases_releases_only_expired(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    store = LeaseStore(db, snap)

    now = time.time()
    live = _make_lease("live", heartbeat=now, timeout=3600.0)
    dead = _make_lease("dead", heartbeat=now - 1000.0, timeout=60.0)  # expired
    store.put(live)
    store.put(dead)

    # Re-load with grace=False so the TRUE last_heartbeat is used (mirrors the
    # reaper CLI, which must not apply a restart grace).
    store2 = LeaseStore(db, snap)
    store2.load(grace=False)

    released = store2.reap_expired_leases()
    assert released == 1
    assert store2.get("dead") is None
    assert store2.get("live") is not None
    store.close()
    store2.close()


def test_reap_expired_leases_none_when_all_live(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    store = LeaseStore(db, snap)
    store.put(_make_lease("a", heartbeat=time.time(), timeout=3600.0))
    store.put(_make_lease("b", heartbeat=time.time(), timeout=3600.0))
    assert store.reap_expired_leases() == 0
    store.close()


def test_reap_expired_leases_persists_to_db(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    s1 = LeaseStore(db, snap)
    now = time.time()
    s1.put(_make_lease("keep", heartbeat=now, timeout=3600.0))
    s1.put(_make_lease("gone", heartbeat=now - 9999.0, timeout=10.0))
    s1.close()

    s2 = LeaseStore(db, snap)
    s2.load(grace=False)
    assert s2.reap_expired_leases() == 1
    s2.close()

    s3 = LeaseStore(db, snap)
    s3.load(grace=False)
    assert s3.get("gone") is None
    assert s3.get("keep") is not None
    s3.close()


def test_load_grace_false_keeps_stored_heartbeat(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    s1 = LeaseStore(db, snap)
    old = _make_lease("o", heartbeat=1000.0, timeout=10.0)
    s1.put(old)
    s1.close()

    s2 = LeaseStore(db, snap)
    s2.load(grace=False)
    assert s2.get("o").last_heartbeat == 1000.0  # untouched (no restart grace)
    s2.close()


# --------------------------------------------------------------------------- #
# Fix B — v2 dedup fail-closed
# --------------------------------------------------------------------------- #
@pytest.fixture
def gk_v2(tmp_path):
    mod = load_module()
    Gatekeeper = getattr(mod, "Gatekeeper")
    gk = Gatekeeper(load_policy(), tmp_path, fail_fast=False)
    gk.justification_mode = "v2_exact"  # force the v2 path
    return gk


def test_v2_fail_closed_on_semantic_error(gk_v2):
    # Seed a cross-agent duplicate justification (exact) in the store.
    gk_v2.register_port("raven", "lab", 8085, "prometheus exporter")

    # Force semantic dedup to error out.
    def _boom(agent, what_for, port):
        raise RuntimeError("embedder down")

    gk_v2._semantic_dedup = _boom

    # Cross-agent reuse must now be REJECTED (fail-closed -> v1_exact fallback),
    # not silently allowed as the old fail-open behaviour did.
    allow, reason = gk_v2.check_justification("owl", "prometheus exporter", 8120)
    assert allow is False
    assert "дубликат" in reason.lower() or "justification" in reason.lower()


def test_v2_same_agent_allowed_when_semantic_errors(gk_v2):
    gk_v2.register_port("raven", "lab", 8085, "prometheus exporter")

    def _boom(agent, what_for, port):
        raise RuntimeError("embedder down")

    gk_v2._semantic_dedup = _boom

    # Same agent reusing the text is legitimately allowed (v1_exact allows it).
    allow, reason = gk_v2.check_justification("raven", "prometheus exporter", 8081)
    assert allow is True
