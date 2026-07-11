# Test surface — real implemented interface (as tested)

These tests exercise the actual server in `bin/mcp-gatekeeper-server.py`.

## Core object
```python
class Gatekeeper(policy: dict, data_dir: Path, fail_fast: bool = False)
    pdp(req: dict) -> (allow: bool, reason: str)     # deterministic PDP chain
    register_port(agent, project_id, port, what_for, run_as=None, as_root=False, ...)
    register_timer(agent, project_id, action, schedule, what_for, ...)
    register_service(agent, project_id, port, action, schedule, what_for, ...)
    release(request_id, by_agent=None)
    transfer(request_id, to_agent, project_id, by_agent=None)   # lease handoff
    heartbeat(request_id)
    reaper_tick() -> [released_request_id, ...]                 # lease-timeout
    journal(event: dict)                                        # atomic JSONL append
```
`register_*` return a dict with `status` in {ALLOW, REJECT} (plus request_id,
bypass, error, ...).

## PDP request fields
`agent`, `project_id`, `port`, `timer={action,schedule}`, `what_for`, `run_as`, `as_root`.

## Policy (policies/policy_v1.yaml)
Agent ids + port pools: raven 8080-8099, antcat 8100-8119, owl 8120-8139,
kotolizator 8140-8159, mangust 8160-8169, bestia 8170-8179, dominika 8180-8189,
streikbrecher 8190-8199. Quotas 3 ports / 5 timers. Reserve: <1024 and
[5432, 8086, 8087, 9100, 9187].

## Journal (data/port-timer-log.jsonl)
Required keys per record: `request_id, when, what_for, why, agent, project`.
Root backdoor grants carry `why` containing `BYPASS=root`.

## Test files
- `test_pdp.py`     — 41 tests, PDP rules 1-9 (allow+reject), ranges, quota,
                      reserve, dedup, justification, least-priv, lease/handoff/timeout,
                      root backdoor + audit, reject-message quality.
- `test_journal.py` —  5 tests, atomic write + required fields + reject/bypass audit.
- `test_systemd.py` —  5 tests (implementer-owned): unit keys, systemd-analyze verify,
                      fail-fast on bad policy, sd_notify READY+WATCHDOG, MCP stdio register_port.
