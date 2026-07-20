<!-- SEMDEP -->
> ⚠️ **НЕАКТУАЛЬНО / SUPERSEDED (RUL-010).** Документ описывает пре-ALM стек семантической памяти лаборатории: `lab_search.py` / `labsearch` / `onnx-embedder :8082` / `mcp-memory :8087` / `Context API :8100` / FAISS+ONNX / прямые вызовы ALM `:3002`. Все эти пути **УДАЛЕНЫ и ЗАПРЕЩЕНЫ** с 15.07.2026. Единственный рабочий путь семантической памяти — MCP-инструмент **`memory-gateway__search_memory`** (сервер `memory-gateway`, 127.0.0.1:8091, бэкенд ALM/AnythingLLM). Описанные здесь команды/порты/сервисы — НЕ рабочие; не используйте их. Код, всё ещё ссылающийся на этот стек, — устаревшая (stale) зависимость, требует миграции на `memory-gateway__search_memory` (эскалация ЗавЛабу).

# MCP Tools — Предложение по реализации

**Дата:** 2026-06-29
**Автор:** Доминика (Scout)
**Статус:** draft

## Боль → Решение

### 1. shell Server (`bin/shell-server.py`)

**Боль:** Агенты используют `exec` напрямую без стандартизации безопасности.
**Решение:** MCP-сервер с allowlist команд.

Безопасность:
- Allowlist команд (только безопасные: ls, cat, grep, git status, find)
- Запрет: rm, mv, cp, curl, wget, pip, npm install
- Таймаут 30с на команду
- Audit log в `/var/log/mcp-tools/shell.log`

### 2. memory Server (`bin/memory-server.py`)

**Боль:** labsearch — скрипт, который нужно вызывать ручной строкой.
**Решение:** MCP-сервер → один инструмент `search(query)` → FAISS поиск по лаборатории.

Безопасность:
- Только чтение, без модификации индекса
- Лимит топ-K (default 5, max 20)

### 3. status Server (`bin/status-server.py`)

**Боль:** ЗавЛабу/агентам нет единой точки статуса.
**Решение:** MCP-сервер → `get_status()` → агрегирует:

- Статус systemd-сервисов (running/failed)
- Статус Docker-контейнеров
- Использование диска
- Последние инциденты
- Статус cron-задач

## Общая архитектура

```
mcp-tools/
├── bin/
│   ├── filesystem-server.py  ✅ (spike готов)
│   ├── shell-server.py
│   ├── memory-server.py
│   └── status-server.py
├── docs/
│   ├── proposal.md          ← этот файл
│   └── security.md
└── tests/
```

## Интеграция с OpenClaw

Агенты подключают MCP-серверы через mcporter:

```bash
mcporter config add filesystem stdio -- python3 /root/LabDoctorM/projects/mcp-tools/bin/filesystem-server.py
mcporter config add memory stdio -- python3 /root/LabDoctorM/projects/mcp-tools/bin/memory-server.py
mcporter config add status stdio -- python3 /root/LabDoctorM/projects/mcp-tools/bin/status-server.py
```

Затем вызывают тулы:
```bash
mcporter call filesystem.read_file path=/root/LabDoctorM/README.md
mcporter call memory.search query="redis cache pattern"
mcporter call status.get_status
```

## Приоритет

1. **filesystem-server.py** ✅ готов (spike)
2. **memory-server.py** (наибольшая ценность для агентов)
3. **status-server.py** (приборный щит лаборатории)
4. **shell-server.py** (опасный, последним, с максимальной защитой)

## Следующий шаг

Написать memory-server.py — он даст наибольшую пользу агентам, потому что labsearch сейчас неудобен.
