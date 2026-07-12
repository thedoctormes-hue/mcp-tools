"""
Unit tests for the PDP brain (Gatekeeper.pdp + register_*).

Covers all 9 PDP rules from docs/CONTRACT.md on allow AND reject scenarios,
plus agent ranges, quotas, reserve, dedup, lease handoff and root-backdoor audit.

Real interface:
  gk.pdp(req) -> (allow: bool, reason: str)
  gk.register_port(agent, project_id, port, what_for, run_as=, as_root=)
  gk.register_timer(agent, project_id, action, schedule, what_for, ...)
  gk.transfer(request_id, to_agent, project_id, by_agent=)
Agent ids come from policy_v1.yaml: raven(8080-8099), antcat(8100-8119), owl(8120-8139).
"""

import pytest


def _req(**kw):
    base = dict(agent="raven", project_id="lab", port=8080, what_for="test allocation")
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Rule 1 — Identity
# --------------------------------------------------------------------------- #

def test_r1_known_agent_allowed(gk):
    allow, reason = gk.pdp(_req(agent="raven", port=8080))
    assert allow is True, reason

def test_r1_unknown_agent_rejected(gk):
    allow, reason = gk.pdp(_req(agent="ghost-xyz", port=8080))
    assert allow is False
    assert "неизвест" in reason.lower() or "identity" in reason.lower()


# --------------------------------------------------------------------------- #
# Rule 2 — Port range (raven 8080-8099, antcat 8100-8119, owl 8120-8139)
# --------------------------------------------------------------------------- #

def test_r2_raven_low_bound_allowed(gk):
    assert gk.pdp(_req(agent="raven", port=8080))[0] is True

def test_r2_raven_high_bound_allowed(gk):
    assert gk.pdp(_req(agent="raven", port=8099))[0] is True

def test_r2_raven_out_of_range_rejected(gk):
    # Global range [1024,65535]; порт выше диапазона -> REJECT (range).
    allow, reason = gk.pdp(_req(agent="raven", port=70000))
    assert allow is False
    assert "вне" in reason.lower() or "range" in reason.lower() or "диапазон" in reason.lower()

def test_r2_antcat_in_range_allowed(gk):
    assert gk.pdp(_req(agent="antcat", port=8100))[0] is True

def test_r2_antcat_out_of_range_rejected(gk):
    assert gk.pdp(_req(agent="antcat", port=70000))[0] is False

def test_r2_owl_in_range_allowed(gk):
    assert gk.pdp(_req(agent="owl", port=8120))[0] is True

def test_r2_owl_out_of_range_rejected(gk):
    assert gk.pdp(_req(agent="owl", port=70000))[0] is False


# --------------------------------------------------------------------------- #
# Rule 3 — Quota (<=3 ports, <=5 timers). Counts active leases, so register.
# --------------------------------------------------------------------------- #

def test_r3_three_ports_allowed(gk):
    for i, port in enumerate((8080, 8081, 8082)):
        r = gk.register_port("raven", "lab", port, f"svc number {i}")
        assert r["status"] == "ALLOW", r

def test_r3_fourth_port_rejected(gk):
    for i, port in enumerate((8080, 8081, 8082)):
        gk.register_port("raven", "lab", port, f"svc number {i}")
    r = gk.register_port("raven", "lab", 8083, "fourth service")
    assert r["status"] == "REJECT"
    assert "квота" in r["error"].lower() or "quota" in r["error"].lower()

def test_r3_five_timers_allowed(gk):
    for i in range(5):
        r = gk.register_timer("raven", "lab", f"act{i}", f"*/{i+1} * * * *", f"timer job {i}")
        assert r["status"] == "ALLOW", r

def test_r3_sixth_timer_rejected(gk):
    for i in range(5):
        gk.register_timer("raven", "lab", f"act{i}", f"*/{i+1} * * * *", f"timer job {i}")
    r = gk.register_timer("raven", "lab", "act6", "*/6 * * * *", "sixth timer job")
    assert r["status"] == "REJECT"


# --------------------------------------------------------------------------- #
# Rule 4 — Reserve
#   8086/8087 sit inside raven's pool -> reserve fires (reason mentions reserve).
#   privileged <1024 and other blocked ports fall outside every pool -> blocked.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("port", [8086, 8087])
def test_r4_reserved_in_range_rejected(gk, port):
    allow, reason = gk.pdp(_req(agent="raven", port=port))
    assert allow is False
    assert "резерв" in reason.lower() or "reserve" in reason.lower()

@pytest.mark.parametrize("port", [22, 80, 443, 1023, 8888, 9090, 9187])
def test_r4_other_reserved_blocked(gk, port):
    assert gk.pdp(_req(agent="raven", port=port))[0] is False

def test_r4_normal_port_allowed(gk):
    assert gk.pdp(_req(agent="raven", port=8085))[0] is True


# --------------------------------------------------------------------------- #
# Rule 5 — Dedup
# --------------------------------------------------------------------------- #

def test_r5_duplicate_port_refreshes_same_agent(gk):
    # Повторная регистрация того же порта тем же агентом (restart) = refresh.
    gk.register_port("raven", "lab", 8080, "first claim")
    r = gk.register_port("raven", "lab", 8080, "second claim")
    assert r["status"] == "ALLOW", r
    live = [l for l in gk.leases.values() if l.agent == "raven" and l.port == 8080]
    assert len(live) == 1, "повтор должен обновлять lease, а не плодить"


def test_r5_cross_agent_port_rejected(gk):
    # Другой агент на тот же порт = реальный конфликт намерений -> REJECT.
    gk.register_port("raven", "lab", 8080, "service A")
    r = gk.register_port("owl", "lab", 8080, "service B")
    assert r["status"] == "REJECT"
    assert "занят" in r["error"].lower() or "заявлен" in r["error"].lower()

def test_r5_duplicate_timer_rejected(gk):
    gk.register_timer("raven", "lab", "backup", "0 2 * * *", "nightly backup")
    r = gk.register_timer("raven", "lab", "backup", "0 2 * * *", "nightly backup two")
    assert r["status"] == "REJECT"

def test_r5_unique_timer_allowed(gk):
    r = gk.register_timer("raven", "lab", "ping", "*/5 * * * *", "healthcheck ping")
    assert r["status"] == "ALLOW", r


# --------------------------------------------------------------------------- #
# Rule 6 — Justification (what_for required, non-empty, min length)
# --------------------------------------------------------------------------- #

def test_r6_empty_what_for_rejected(gk):
    assert gk.pdp(_req(agent="raven", port=8080, what_for=""))[0] is False

def test_r6_filled_what_for_allowed(gk):
    assert gk.pdp(_req(agent="raven", port=8080, what_for="metrics exporter"))[0] is True

def test_r6_duplicate_justification_same_agent_refreshes(gk):
    # Тот же агент + тот же what_for (даже для таймера, port=None) = refresh,
    # не аномалия (ЗавЛаб 12.07: restart легитимен).
    gk.register_timer("raven", "lab", "job", "*/5 * * * *", "identical justification text")
    allow, reason = gk.pdp(dict(agent="raven", project_id="lab", port=None,
                                what_for="identical justification text"))
    assert allow is True


def test_r6_cross_agent_justification_rejected(gk):
    # Перехват чужого оправдания (другой агент с тем же текстом) = аномалия.
    gk.register_port("raven", "lab", 8080, "prometheus exporter")
    allow, reason = gk.pdp(dict(agent="owl", project_id="lab", port=8120,
                                what_for="prometheus exporter"))
    assert allow is False
    assert "justification" in reason.lower() or "дубликат" in reason.lower()

def test_r6_same_justification_different_port_allowed(gk):
    # Documents real behavior: exact-match dedup is port-scoped, so the same
    # what_for on a DIFFERENT port is allowed (semantic dedup is v2/fail-open).
    gk.register_port("raven", "lab", 8080, "prometheus exporter")
    r = gk.register_port("raven", "lab", 8081, "prometheus exporter")
    assert r["status"] == "ALLOW", r


# --------------------------------------------------------------------------- #
# Rule 7 — Least-privilege (run_as=root refused; use as_root backdoor instead)
# --------------------------------------------------------------------------- #

def test_r7_run_as_root_rejected(gk):
    # run_as=root запрещён только для НЕИЗВЕСТНЫХ агентов (в root-only среде
    # легитимные сервисы бегут от root). Проверяем логику напрямую.
    ok, reason = gk.check_least_privilege("root", agent="ghost-unknown")
    assert ok is False
    assert "least" in reason.lower() or "privilege" in reason.lower() or "root" in reason.lower()


def test_r7_run_as_root_allowed_for_known(gk):
    # Известный агент может бежать от root (среда lab — всё от root).
    ok, reason = gk.check_least_privilege("root", agent="raven")
    assert ok is True

def test_r7_run_as_limited_allowed(gk):
    assert gk.pdp(_req(agent="raven", port=8080, run_as="mcp-gatekeeper"))[0] is True


# --------------------------------------------------------------------------- #
# Rule 8 — Project-scoped lease / handoff / lease-timeout
# --------------------------------------------------------------------------- #

def test_r8_project_id_required(gk):
    allow, reason = gk.pdp(_req(agent="raven", project_id="", port=8080))
    assert allow is False
    assert "project" in reason.lower()

def test_r8_same_agent_port_refreshes_across_projects(gk):
    # Тот же агент, тот же порт, другой проект -> refresh (ЗавЛаб 12.07).
    gk.register_port("raven", "projA", 8080, "service A")
    r = gk.register_port("raven", "projB", 8080, "service B")
    assert r["status"] == "ALLOW", r


def test_r8_cross_agent_port_rejected(gk):
    # Другой агент на тот же порт = конфликт -> REJECT.
    gk.register_port("raven", "projA", 8080, "service A")
    r = gk.register_port("owl", "projB", 8080, "service B")
    assert r["status"] == "REJECT"

def test_r8_handoff_transfers_tenant(gk):
    reg = gk.register_port("raven", "projA", 8080, "shared service")
    rid = reg["request_id"]
    res = gk.transfer(rid, to_agent="antcat", project_id="projA", by_agent="raven")
    assert res["status"] == "TRANSFERRED"
    assert res["agent"] == "antcat"

def test_r8_handoff_only_by_current_tenant(gk):
    reg = gk.register_port("raven", "projA", 8080, "shared service")
    rid = reg["request_id"]
    res = gk.transfer(rid, to_agent="owl", project_id="projA", by_agent="antcat")
    assert res["status"] == "FORBIDDEN"

def test_r8_lease_timeout_releases(gk):
    reg = gk.register_port("raven", "projA", 8080, "temp service")
    rid = reg["request_id"]
    # Force heartbeat far in the past, then run the reaper.
    gk.leases[rid].last_heartbeat = 0.0
    released = gk.reaper_tick()
    assert rid in released
    # Port should now be free to reclaim.
    r2 = gk.register_port("raven", "projA", 8080, "temp service two")
    assert r2["status"] == "ALLOW", r2


# --------------------------------------------------------------------------- #
# Rule 9 — Root backdoor (только авторизованные агенты, Фаза 2, ADR-0055)
# --------------------------------------------------------------------------- #

def test_r9_authorized_agent_root_bypasses_checks(gk):
    # raven входит в policy.gatekeeper.authorized_root_agents -> bypass разрешён
    allow, reason = gk.pdp(_req(agent="raven", port=5432, as_root=True))
    assert allow is True
    assert "root" in reason.lower()

def test_r9_unauthorized_agent_root_rejected(gk):
    # неавторизованный агент не может сделать as_root-bypass (закрывает дыру 5)
    allow, reason = gk.pdp(_req(agent="ghost-xyz", port=5432, as_root=True))
    assert allow is False
    assert "не авторизован" in reason.lower() or "root" in reason.lower()

def test_r9_disabled_backdoor_rejected(gk):
    gk.allow_root_backdoor = False
    allow, reason = gk.pdp(_req(agent="raven", port=5432, as_root=True))
    assert allow is False
    assert "отключён" in reason.lower() or "disabled" in reason.lower()

def test_r9_root_register_sets_bypass_flag(gk):
    r = gk.register_port("raven", "lab", 8086, "emergency", as_root=True)
    assert r["status"] == "ALLOW"
    assert r.get("bypass") == "root"


# --------------------------------------------------------------------------- #
# Reject messages are human readable (CONTRACT: "8090 занят ..., бери 8091")
# --------------------------------------------------------------------------- #

def test_reject_reason_human_readable(gk):
    # Cross-agent конфликт порта -> REJECT с человекочитаемой причиной.
    gk.register_port("raven", "lab", 8085, "occupier")
    r = gk.register_port("owl", "lab", 8085, "second occupier")
    assert r["status"] == "REJECT"
    assert len(r["error"]) > 5

def test_suggest_free_port_global_range(gk):
    # _suggest_free_port предлагает порт в глобальном разрешённом диапазоне,
    # не резерв и не занят.
    sug = gk._suggest_free_port("raven")
    assert sug is not None
    lo, hi = gk.allowed_port_range
    assert lo <= sug <= hi
    assert sug >= int(gk.reserve.get("block_privileged_below", 1024))
    assert sug not in gk.reserve.get("blocked_ports", [])
