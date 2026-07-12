"""
Journal tests: port-timer-log.jsonl must be written ATOMICALLY and contain the
required CONTRACT fields: request_id, when, what_for, why, agent, project.

Real interface: gk.journal(event) appends one JSONL line atomically
(open 'a' + flock LOCK_EX + fsync). register_* calls audit through it.
"""

import json


REQUIRED_KEYS = ["request_id", "when", "what_for", "why", "agent", "project"]


def _journal_lines(gk):
    p = gk.data_dir / "port-timer-log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_journal_written_on_register(gk):
    gk.register_port("raven", "lab", 8080, "prometheus exporter")
    recs = _journal_lines(gk)
    assert len(recs) >= 1


def test_journal_has_required_fields(gk):
    gk.register_port("raven", "lab", 8080, "prometheus exporter")
    recs = _journal_lines(gk)
    assert recs, "no journal records written"
    rec = recs[-1]
    for k in REQUIRED_KEYS:
        assert k in rec, f"journal record missing required key: {k}"


def test_journal_direct_event_atomic(gk):
    for i in range(3):
        gk.journal({
            "request_id": f"req-{i}", "what_for": f"job {i}", "why": "TEST",
            "agent": "raven", "project": "lab",
        })
    p = gk.data_dir / "port-timer-log.jsonl"
    lines = p.read_text().splitlines()
    # Every line must be complete/parseable JSON (no torn writes).
    for line in lines:
        json.loads(line)
    assert len(lines) == 3


def test_journal_records_reject(gk):
    # out-of-range -> REJECT must still be audited
    gk.register_port("raven", "lab", 9999, "bad port")
    recs = _journal_lines(gk)
    assert any(r.get("decision") == "REJECT" or "REJECT" in str(r.get("why", "")) for r in recs)


def test_journal_backdoor_audits_bypass_root(gk):
    gk.register_port("raven", "lab", 8086, "emergency fix", as_root=True)
    recs = _journal_lines(gk)
    assert any("BYPASS=root" in str(r.get("why", "")) for r in recs), \
        "root backdoor grant must be audited with BYPASS=root"
