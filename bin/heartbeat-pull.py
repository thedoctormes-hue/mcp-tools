#!/usr/bin/env python3
# heartbeat-pull.py — клиент агента для получения стандартного задания
# (health-check сервера+лабы) от MCP-сервера mcp-heartbeat.
#
# ECONOMY MODEL (ЗавЛаб, 2026-07-12): сервер обычно ВЫКЛЮЧЕН. Каждый крон
# поднимает его на время запроса, забирает задачу и гасит — сервер крутится
# не 24/7, а считанные секунды на прогон. Это и есть «Гибрид»: старт по
# требованию вместо держания сервиса вечно живым.
#
# Поток на один вызов:
#   1. если сервер не поднят -> systemctl start (одна попытка)
#   2. pull(agent) через MCP
#   3. вывести summary_text
#   4. systemctl stop (экономия: гасим)
# Если start или pull не удались -> инструкция ДЛЯ ОПЕРАТОРА, сервер гасим.
#
# ВАЖНО: скрипт НИКОГДА не трогает gateway. Только mcp-heartbeat.service.
import os
import sys
import json
import time
import socket
import asyncio
import subprocess

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("HB_URL", "http://127.0.0.1:8088/mcp")
AGENT = sys.argv[1] if len(sys.argv) > 1 else "dominika"
# Имя сервиса можно переопределить (для тестов / будущих серверов).
SERVICE = os.environ.get("HB_SERVICE", "mcp-heartbeat.service")
TIMEOUT = int(os.environ.get("HB_TIMEOUT", "8"))
START_WAIT = int(os.environ.get("HB_START_WAIT", "3"))


def _extract_summary(result) -> "str | None":
    """Достать summary_text из CallToolResult (structuredContent или текст)."""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict) and "summary_text" in sc:
        return sc.get("summary_text")
    for c in getattr(result, "content", []) or []:
        txt = getattr(c, "text", None)
        if txt:
            try:
                d = json.loads(txt)
                if isinstance(d, dict) and "summary_text" in d:
                    return d["summary_text"]
            except Exception:
                return txt
    return None


async def _pull_once() -> "str | None":
    async with streamablehttp_client(URL) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=TIMEOUT)
            result = await asyncio.wait_for(
                session.call_tool("pull", {"agent": AGENT}), timeout=TIMEOUT
            )
            return _extract_summary(result)


def _do_pull() -> "str | None":
    try:
        return asyncio.run(_pull_once())
    except Exception:
        return None


def _server_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 8088), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _try_start() -> bool:
    """Одна попытка поднять сервер (он обычно выключен — это норма)."""
    try:
        res = subprocess.run(
            ["systemctl", "start", SERVICE],
            capture_output=True, text=True, timeout=30,
        )
        return res.returncode == 0
    except Exception:
        return False


def _try_stop() -> None:
    """Гасим сервер (экономия). Ошибки игнорируем."""
    try:
        subprocess.run(
            ["systemctl", "stop", SERVICE],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _operator_instruction():
    print(f"⚠️ heartbeat-сервер недоступен (127.0.0.1:8088) — ДЛЯ ОПЕРАТОРА:")
    print(f"Поднять вручную: systemctl start {SERVICE}")
    print(f"Перепроверить: python3 {os.path.abspath(__file__)} {AGENT}")


def main():
    # сервер обычно выключен (экономия) — поднимаем на время запроса
    if not _server_up():
        if not _try_start():
            _operator_instruction()
            sys.exit(1)
        time.sleep(START_WAIT)

    summary = _do_pull()
    if summary:
        print(summary)
        _try_stop()  # экономия: гасим сервер
        sys.exit(0)

    # pull не удался при поднятом сервере
    _operator_instruction()
    _try_stop()
    sys.exit(1)


if __name__ == "__main__":
    main()
