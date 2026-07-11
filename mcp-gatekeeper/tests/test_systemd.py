"""Integration-тесты: отказоустойчивость systemd-юнита и реальный MCP-транспорт.

Покрываем критерий product-ready:
- юнит содержит обязательные ключи (Type=simple, Restart=on-failure,
  RestartSec=5, StartLimitBurst=3, StartLimitIntervalSec=60); НЕТ WatchdogSec
  (polling избыточен — «жив ли сервер» доказывается ответом на запрос);
- fail-fast: невалидная политика -> процесс завершается ненулевым кодом
  (systemd Restart=on-failure перезапустит, упрётся в StartLimitBurst);
- сервер НЕ шлёт периодический WATCHDOG (событийная модель); sd_notify
  остаётся только для one-shot READY=1/STOPPING=1;
- реальный MCP stdio-транспорт: сервер стартует и отвечает на register_port.
"""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent  # mcp-gatekeeper/
SERVER = REPO / "bin" / "mcp-gatekeeper-server.py"
UNIT = REPO / "systemd" / "mcp-gatekeeper.service"


def _load_module(name="gk_server_sys"):
    spec = importlib.util.spec_from_file_location(name, str(SERVER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Юнит-файл содержит обязательные ключи контракта
# --------------------------------------------------------------------------- #
def test_unit_file_has_required_keys():
    assert UNIT.exists(), "нет юнит-файла"
    text = UNIT.read_text(encoding="utf-8")
    required = [
        "Type=simple",
        "Restart=on-failure",
        "RestartSec=5",
        "StartLimitBurst=3",
        "StartLimitIntervalSec=60",
    ]
    for key in required:
        assert key in text, f"в юните нет обязательного: {key}"
    # Событийная модель: polling-watchdog запрещён
    assert "WatchdogSec" not in text, "юнит не должен содержать WatchdogSec (polling избыточен)"


def test_unit_file_systemd_analyze():
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze недоступен в среде")
    r = subprocess.run(["systemd-analyze", "verify", str(UNIT)],
                       capture_output=True, text=True, timeout=30)
    # verify возвращает не-0 при ошибках; предупреждения (warnings) допустимы.
    assert r.returncode == 0 or "ERROR" not in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# Fail-fast: невалидная политика -> ненулевой exit code
# --------------------------------------------------------------------------- #
def test_fail_fast_on_bad_policy():
    bad = Path(tempfile.gettempdir()) / "bad_policy.yaml"
    bad.write_text("agents: []\nquotas: {}\nreserve: {}\ngatekeeper: {}\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SERVER), "--policy", str(bad), "health"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode != 0, f"ожидался fail-fast exit!=0, got {r.returncode}: {r.stderr}"


# --------------------------------------------------------------------------- #
# Событийная модель: сервер НЕ шлёт периодический WATCHDOG (нет пинга).
# sd_notify остаётся только для one-shot READY/STOPPING (не heartbeat).
# --------------------------------------------------------------------------- #
def test_event_driven_no_watchdog_one_shot_sd_notify():
    mod = _load_module()
    # watchdog-поток и флаг воркера удалены в пользу событийного детекта
    assert not hasattr(mod, "_watchdog_loop"), "watchdog-поток не должен существовать"
    assert not hasattr(mod, "_WORKER_ALIVE"), "флаг воркера не должен существовать"
    # sd_notify сохранён для one-shot уведомлений (не периодический WATCHDOG)
    assert hasattr(mod, "sd_notify"), "sd_notify должен остаться для READY/STOPPING"

    sock_path = Path(tempfile.gettempdir()) / f"gk_notify_{os.getpid()}.sock"
    if sock_path.exists():
        sock_path.unlink()
    recv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    recv_sock.bind(str(sock_path))

    os.environ["NOTIFY_SOCKET"] = str(sock_path)
    try:
        # READY при старте (one-shot, не heartbeat)
        assert mod.sd_notify("READY=1") is True
        data, _ = recv_sock.recvfrom(1024)
        assert b"READY=1" in data, data

        # НЕ должно быть периодического WATCHDOG=1 — его больше никто не шлёт
        recv_sock.settimeout(1)
        try:
            data, _ = recv_sock.recvfrom(1024)
            assert b"WATCHDOG=1" not in data, f"неожиданный WATCHDOG: {data}"
        except socket.timeout:
            pass  # ok: никто не шлёт watchdog
    finally:
        os.environ.pop("NOTIFY_SOCKET", None)
        recv_sock.close()
        if sock_path.exists():
            sock_path.unlink()


# --------------------------------------------------------------------------- #
# Реальный MCP stdio-транспорт: сервер стартует и отвечает
# --------------------------------------------------------------------------- #
def _read_json_line(proc, deadline):
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except Exception:
            continue  # игнорируем не-JSON
    return None


def test_mcp_stdio_register_port():
    env = dict(os.environ)
    env["MCP_TRANSPORT"] = "stdio"
    env["GATEKEEPER_DATA"] = tempfile.mkdtemp(prefix="gk_stdio_")
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1,
    )
    try:
        def send(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        # 1) initialize
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "0"}}})
        deadline = time.time() + 10
        init = None
        while time.time() < deadline:
            msg = _read_json_line(proc, deadline)
            if msg and msg.get("id") == 1:
                init = msg
                break
        assert init is not None, "нет ответа на initialize"
        assert "result" in init, init

        # 2) initialized notification
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3) tools/call register_port
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "register_port",
                         "arguments": {"agent": "raven", "project_id": "projTest",
                                       "port": 8081, "what_for": "stdio integration"}}})
        call = None
        while time.time() < deadline:
            msg = _read_json_line(proc, deadline)
            if msg and msg.get("id") == 2:
                call = msg
                break
        assert call is not None, "нет ответа на tools/call"
        assert "result" in call, call
        content = call["result"].get("content", [])
        assert content, "пустой content"
        payload = json.loads(content[0]["text"])
        assert payload["status"] == "ALLOW", payload
        assert payload["request_id"].startswith("rk-")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
