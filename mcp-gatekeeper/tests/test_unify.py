"""Уровень Е ADR-0056 — unification: policy = единый источник портов.

Проверяет, что:
- docs/PORT_REGISTRY.md генерируется из policy (read-only вид, без дрейфа);
- audit/gk-audit.sh больше НЕ читает PORT_REGISTRY.md как источник разрешений.
"""
import subprocess
import tempfile
import os
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(REPO, "scripts", "gen-port-registry.sh")
AUDIT = os.path.join(REPO, "audit", "gk-audit.sh")
POLICY = os.path.join(REPO, "policies", "policy_v1.yaml")


@pytest.mark.skipif(not os.path.exists(GEN), reason="generator script missing")
def test_generator_emits_policy_ports():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "PORT_REGISTRY.md")
        r = subprocess.run(
            ["bash", GEN, POLICY, out], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        text = open(out, encoding="utf-8").read()
        assert "AUTO-GENERATED" in text
        # известные зарезервированные порты из policy обязаны попасть в вид
        assert "5432" in text  # PostgreSQL
        assert "8888" in text  # listen_port gatekeeper
        assert "8086" in text  # mcp-apikeys
        # агентские порты НЕ пре-разрешены в виде (только через lease)
        assert "Агентские порты" in text


def test_audit_no_longer_reads_port_registry_md():
    """gk-audit.sh не должен читать PORT_REGISTRY.md как источник разрешений."""
    src = open(AUDIT, encoding="utf-8").read()
    # старый блок чтения реестра удалён
    assert 'if [[ -f "$PORT_REGISTRY" ]]' not in src
    # policy — единственный источник
    assert "ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ" in src
    # скрипт синтаксически валиден
    r = subprocess.run(["bash", "-n", AUDIT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_quota_policy_matches_readme():
    """Квоты в policy и README согласованы (не дрейфуют)."""
    pol = open(POLICY, encoding="utf-8").read()
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    assert "max_ports: 30" in pol
    assert "max_timers: 50" in pol
    assert "≤30 портов" in readme
    assert "≤50 таймеров" in readme
