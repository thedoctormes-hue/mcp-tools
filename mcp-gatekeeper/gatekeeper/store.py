"""Durable lease store for mcp-gatekeeper (ADR-0058, step 1).

Backing: SQLite (durable source of truth) + a JSON snapshot mirror. An
in-memory cache (`_cache`) is the working set, so existing callers that
mutate a :class:`Lease` object in place (e.g. tests doing
``gk.leases[rid].last_heartbeat = 0.0``) keep working.

On every mutation both the DB row and the snapshot are refreshed, so a
crash loses at most the in-flight request (not committed leases).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Lease:
    request_id: str
    agent: str
    project_id: str
    kind: str  # "port" | "timer" | "service"
    port: Optional[int]
    timer_action: Optional[str]
    timer_schedule: Optional[str]
    what_for: str
    run_as: str
    issued_user: str
    acquired_at: float
    last_heartbeat: float
    lease_timeout: float
    bypass: Optional[str] = None  # "root" или None
    unit: Optional[str] = None    # имя systemd-юнита

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LeaseStore:
    def __init__(self, db_path: Path, snapshot_path: Path):
        self.db_path = Path(db_path)
        self.snapshot_path = Path(snapshot_path)
        # Lazy-create the backing directory (mirrors the old _save_state mkdir).
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Lease] = {}
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    # ---- schema ----
    def init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                request_id   TEXT PRIMARY KEY,
                agent        TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                kind         TEXT NOT NULL,
                port         INTEGER,
                timer_action TEXT,
                timer_schedule TEXT,
                what_for    TEXT,
                run_as      TEXT,
                issued_user TEXT,
                acquired_at REAL,
                last_heartbeat REAL,
                lease_timeout REAL,
                bypass       TEXT,
                unit         TEXT
            )
            """
        )
        self._conn.commit()

    # ---- (de)serialization ----
    @staticmethod
    def _row_to_lease(row) -> Lease:
        return Lease(
            request_id=row["request_id"],
            agent=row["agent"],
            project_id=row["project_id"],
            kind=row["kind"],
            port=row["port"],
            timer_action=row["timer_action"],
            timer_schedule=row["timer_schedule"],
            what_for=row["what_for"],
            run_as=row["run_as"],
            issued_user=row["issued_user"],
            acquired_at=row["acquired_at"],
            last_heartbeat=row["last_heartbeat"],
            lease_timeout=row["lease_timeout"],
            bypass=row["bypass"],
            unit=row["unit"],
        )

    def _load_legacy_json(self) -> List[Lease]:
        """Read a legacy leases.json snapshot (old _save_state format)."""
        if not self.snapshot_path.exists():
            return []
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        leases: List[Lease] = []
        for rec in data.get("leases", []):
            try:
                leases.append(Lease(**rec))
            except Exception:
                continue
        return leases

    # ---- load ----
    def load(self) -> Dict[str, Lease]:
        """Populate the in-memory cache from the DB.

        Each loaded lease gets a *grace*: ``last_heartbeat`` is reset to now,
        so a restart doesn't immediately reap still-valid leases (agents get
        time to re-send heartbeats). If the DB is empty but a legacy
        ``leases.json`` exists beside it, perform a one-time migration that
        preserves existing leases (e.g. ``sh`` -> 8100).
        """
        self._cache = {}
        rows = self._conn.execute("SELECT * FROM leases").fetchall()
        if rows:
            for row in rows:
                l = self._row_to_lease(row)
                l.last_heartbeat = time.time()  # grace on restart
                self._cache[l.request_id] = l
        else:
            legacy = self._load_legacy_json()
            if legacy:
                for lease in legacy:
                    lease.last_heartbeat = time.time()  # grace on migration too
                    self.put(lease, _snapshot=False)
                self.snapshot()
        return self._cache

    # ---- mutations (durable: DB + snapshot) ----
    def put(self, lease: Lease, _snapshot: bool = True) -> None:
        self._cache[lease.request_id] = lease
        self._conn.execute(
            """
            INSERT OR REPLACE INTO leases
                (request_id, agent, project_id, kind, port, timer_action,
                 timer_schedule, what_for, run_as, issued_user, acquired_at,
                 last_heartbeat, lease_timeout, bypass, unit)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lease.request_id, lease.agent, lease.project_id, lease.kind,
                lease.port, lease.timer_action, lease.timer_schedule,
                lease.what_for, lease.run_as, lease.issued_user,
                lease.acquired_at, lease.last_heartbeat, lease.lease_timeout,
                lease.bypass, lease.unit,
            ),
        )
        self._conn.commit()
        if _snapshot:
            self.snapshot()

    def delete(self, request_id: str, _snapshot: bool = True) -> None:
        self._cache.pop(request_id, None)
        self._conn.execute("DELETE FROM leases WHERE request_id = ?", (request_id,))
        self._conn.commit()
        if _snapshot:
            self.snapshot()

    # ---- reads ----
    def get(self, request_id: str) -> Optional[Lease]:
        return self._cache.get(request_id)

    def all(self) -> List[Lease]:
        return list(self._cache.values())

    def all_as_dict(self) -> Dict[str, Lease]:
        # Returned as the live cache so in-place mutations are visible to
        # readers (mirrors the old ``self.leases`` dict semantics).
        return self._cache

    # ---- snapshot mirror ----
    def snapshot(self) -> None:
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "leases": [l.to_dict() for l in self._cache.values()],
        }
        tmp = self.snapshot_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.snapshot_path)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
