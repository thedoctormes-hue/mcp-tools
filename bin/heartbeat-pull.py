#!/usr/bin/env python3
# heartbeat-pull.py — клиент агента для получения стандартного задания
# (health-check сервера+лабы) от MCP-сервера mcp-heartbeat.
#
# Сервер = настоящий MCP (FastMCP, streamable-http) на 127.0.0.1:8088,
# эндпоинт протокола /mcp. Клиент говорит по MCP: initialize -> call_tool pull.
#
# Поведение (вариант «Гибрид», утверждён ЗавЛабом 2026-07-12):
#   1. Дёрнуть pull(agent) через MCP.
#   2. Если сервер жив — вывести summary_text (минималистичный health-check).
#   3. Если сервер мёртв — одна попытка самолечения:
#        systemctl start mcp-heartbeat.service, пауза, повторный pull.
#      Если помогло — вывести summary_text.
#      Если нет — вывести инструкцию ДЛЯ ОПЕРАТОРА (без вечного авто-рестарта).
#
# ВАЖНО: скрипт НИКОГДА не трогает gateway. Только mcp-heartbeat.service.
import os
import sys
import json
import time
import asyncio
import subprocess

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("HB_URL", "http://127.0.0.1:8088/mcp")
AGENT = sys.argv[1] if len(sys.argv) > 1 else "dominika"
# Имя сервиса можно переопределить (для тестов / будущих серверов).
SERVICE = os.environ.get("HB_SERVICE", "mcp-heartbeat.service")
TIMEOUT = int(os.environ.get("HB_TIMEOUT", "8"))
HEAL_WAIT = int(os.environ.get("HB_HEAL_WAIT", "3"))


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


def _operator_instruction():
    print(f"⚠️ heartbeat-сервер недоступен (127.0.0.1:8088) — ДЛЯ ОПЕРАТОРА:")
    print(f"Поднять: systemctl start {SERVICE}")
    print(f"Перепроверить: python3 {os.path.abspath(__file__)} {AGENT}")


def _try_heal() -> bool:
    """Одна попытка самолечения. True, если systemctl start прошёл."""
    try:
        res = subprocess.run(
            ["systemctl", "start", SERVICE],
            capture_output=True, text=True, timeout=30,
        )
        return res.returncode == 0
    except Exception:
        return False


# --- 1. первая попытка ---
summary = _do_pull()
if summary:
    print(summary)
    sys.exit(0)

# --- 2. сервер мёртв → гибрид: одна попытка самолечения ---
print("⚠️ heartbeat-сервер недоступен — пробуем самолечение (1 попытка)...",
      file=sys.stderr)

if _try_heal():
    time.sleep(HEAL_WAIT)
    summary = _do_pull()
    if summary:
        print(summary)
        sys.exit(0)

# --- 3. самолечение не помогло → инструкция оператору ---
_operator_instruction()
sys.exit(1)
