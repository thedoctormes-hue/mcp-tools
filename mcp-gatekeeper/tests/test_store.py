"""
Unit tests for the durable lease store (ADR-0058 step 1).

Targets ``gatekeeper.store.LeaseStore`` directly — no Gatekeeper / policy
needed. Paths use pytest's tmp_path so nothing touches /var/lib or the
live filesystem.
"""

import json
import time


from gatekeeper.store import Lease, LeaseStore


def _make_lease(rid="r1", agent="raven", port=8080, timeout=86400.0):
    now = time.time()
    return Lease(
        request_id=rid,
        agent=agent,
        project_id="projA",
        kind="port",
        port=port,
        timer_action=None,
        timer_schedule=None,
        what_for="unit-test lease",
        run_as="mcp-gatekeeper",
        issued_user="mcp-gatekeeper",
        acquired_at=now,
        last_heartbeat=now,
        lease_timeout=timeout,
        bypass=None,
        unit=None,
    )


def test_put_get_delete(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    store = LeaseStore(db, snap)
    lease = _make_lease()
    store.put(lease)
    assert store.get("r1") is not None
    assert store.get("r1").port == 8080
    assert store.get("missing") is None
    store.delete("r1")
    assert store.get("r1") is None
    store.close()


def test_persists_across_reopen(tmp_path):
    # Эмуляция restart: новый LeaseStore на тот же db_path.
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    s1 = LeaseStore(db, snap)
    s1.put(_make_lease(rid="r1", port=8080))
    s1.put(_make_lease(rid="r2", agent="antcat", port=8081))
    s1.close()

    s2 = LeaseStore(db, snap)
    s2.load()
    all_ = s2.all_as_dict()
    assert set(all_.keys()) == {"r1", "r2"}
    assert all_["r1"].port == 8080
    assert all_["r2"].agent == "antcat"
    s2.close()


def test_snapshot_writes_json(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    store = LeaseStore(db, snap)
    store.put(_make_lease(rid="r1", port=8080))
    assert snap.exists()
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert any(l["request_id"] == "r1" for l in data["leases"])
    store.close()


def test_load_grace_resets_heartbeat(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    s1 = LeaseStore(db, snap)
    old = _make_lease(rid="r1", port=8080)
    old.last_heartbeat = 1000.0  # давно
    s1.put(old)
    s1.close()

    s2 = LeaseStore(db, snap)
    s2.load()
    loaded = s2.get("r1")
    assert loaded is not None
    # grace: last_heartbeat сброшен ближе к now, а не 1000.0
    assert loaded.last_heartbeat > 1000.0
    assert abs(time.time() - loaded.last_heartbeat) < 60
    s2.close()


def test_migrates_from_legacy_json(tmp_path):
    db = tmp_path / "leases.db"
    snap = tmp_path / "leases.json"
    # Legacy snapshot (формат старого _save_state) рядом с пустой БД.
    legacy = {
        "version": 1,
        "saved_at": "2026-07-19T00:00:00+00:00",
        "leases": [
            {
                "request_id": "legacy-sh-1",
                "agent": "sh",
                "project_id": "infra",
                "kind": "port",
                "port": 8100,
                "timer_action": None,
                "timer_schedule": None,
                "what_for": "infra service on 8100",
                "run_as": "mcp-gatekeeper",
                "issued_user": "mcp-gatekeeper",
                "acquired_at": 1700000000.0,
                "last_heartbeat": 1700000000.0,
                "lease_timeout": 300.0,
                "bypass": None,
                "unit": None,
            }
        ],
    }
    snap.write_text(json.dumps(legacy), encoding="utf-8")
    store = LeaseStore(db, snap)
    store.load()
    all_ = store.all_as_dict()
    # Существующий lease sh->8100 сохранён при миграции.
    assert "legacy-sh-1" in all_
    assert all_["legacy-sh-1"].port == 8100
    assert all_["legacy-sh-1"].agent == "sh"
    store.close()
