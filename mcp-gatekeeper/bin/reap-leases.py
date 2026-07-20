#!/usr/bin/env python3
"""On-demand reaper for mcp-gatekeeper orphaned/dead-agent leases.

Releases leases whose heartbeat deadline has passed, i.e. ``last_heartbeat +
lease_timeout < now`` — leases belonging to agents that died without calling
``release`` (crash/OOM) and would otherwise accumulate forever in the durable
store.

This is the store-level complement to the server's in-process ``reaper_tick()``.
It is *on-demand only*: it does NOT install a scheduler, does NOT restart the
gatekeeper service, and does NOT touch systemd units. To automate it you can
register a periodic call via gatekeeper's own ``register_timer`` tool, e.g.::

    python3 bin/mcp-gatekeeper-server.py --data <DIR> register-timer \\
        --agent streikbrecher --project mcp-tools \\
        --action "reap-leases --data <DIR>" \\
        --schedule "*/15 * * * *" --what-for "reap orphaned gatekeeper leases"

(NOte: this example only documents how it *could* be wired — registration is
NOT performed by this script.)

Usage:
    python3 bin/reap-leases.py [--data DIR] [--dry-run]
Prints the number of leases released (0 if none).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # mcp-gatekeeper/
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gatekeeper.store import LeaseStore  # noqa: E402

DEFAULT_DATA = REPO / "data"


def _expired_count(store: LeaseStore, now: float) -> int:
    return sum(
        1 for l in store.all() if (l.last_heartbeat + l.lease_timeout) < now
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="reap-leases",
        description="Release mcp-gatekeeper leases expired by heartbeat timeout.",
    )
    ap.add_argument(
        "--data",
        default=os.environ.get("GATEKEEPER_DATA", str(DEFAULT_DATA)),
        help="gatekeeper data dir holding leases.db / leases.json",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="count expired leases but do not release them",
    )
    args = ap.parse_args()

    data_dir = Path(args.data)
    db = data_dir / "leases.db"
    snap = data_dir / "leases.json"

    if not db.exists():
        print(f"0  (no lease DB at {db})")
        return 0

    store = LeaseStore(db, snap)
    # grace=False: judge expiry against the TRUE stored last_heartbeat, not a
    # fresh restart grace (the server's load() applies grace on startup).
    store.load(grace=False)

    if args.dry_run:
        count = _expired_count(store, time.time())
        print(f"{count}  (dry-run: not released)")
        store.close()
        return 0

    released = store.reap_expired_leases()
    print(f"{released}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
