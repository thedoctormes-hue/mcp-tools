---
description: "mcp-tools — README"
type: readme
last_reviewed: 2026-07-09
last_code_change: 2026-07-09
status: active
---

# 🔧 MCP Tools

> **Владелец:** DoctorM&Ai | **Статус:** active | **Версия:** 0.3.0

## Описание

Инструменты для Model Context Protocol (MCP) — серверы и утилиты для интеграции MCP-совместимых инструментов с агентами лаборатории.

## Что такое MCP

Model Context Protocol — протокол для предоставления контекста и инструментов LLM-агентам. Агенты подключаются к MCP-серверам и получают доступ к инструментам (чтение файлов, выполнение команд, работа с API) через единый протокол.

**Отличие от обычных API:** MCP — это стандарт описания инструментов. Один и тот же сервер может использоваться разными агентами без изменения кода.

## Стек технологий

- **Python / FastMCP** — реализация MCP-серверов
- **OpenClaw** — целевой потребитель MCP-инструментов для интеграции с агентами

## Структура проекта

```
mcp-tools/
├── bin/               # Исполняемые MCP-серверы
├── docs/              # Документация
├── tests/             # Юнит-тесты (pytest)
├── .github/           # CI/CD (git-гигиена + тесты)
├── PROJECT.md         # Описание проекта
└── CHANGELOG.md       # История изменений
```

## Что реализовано (Фаза 1)

Готовые MCP-серверы, запускаемые как systemd-юниты по HTTP:

- **apikeys-server** (`bin/apikeys-server.py`) — раздача бесплатных API ключей из free-api-hunter. HTTP на `127.0.0.1:8086`. Инструменты: `get_key`, `list_providers`, `get_provider_docs`, `check_health`. Безопасность: маскировка ключей, allowlist провайдеров, read-only.
- **filesystem-server** (`bin/filesystem-server.py`) — read-only доступ к файлам по whitelist. HTTP на `127.0.0.1:8083`. Инструменты: `read_file`, `list_dir`, `search_files`. Разрешены только `/root/LabDoctorM/workspaces/` и `/root/LabDoctorM/projects/`, размер файла до 1MB.

Оба сервера поддерживают два режима транспорта через переменную окружения `MCP_TRANSPORT`:

- `MCP_TRANSPORT=http` — streamable-http (используется под systemd)
- `MCP_TRANSPORT=stdio` (по умолчанию) — stdio для локального запуска без systemd

## Быстрый старт

Запуск systemd-юнитов (HTTP) и подключение через mcporter:

```bash
cd /root/LabDoctorM/projects/mcp-tools

# Запуск systemd-юнитов
sudo systemctl enable --now mcp-filesystem mcp-apikeys

# Подключение через mcporter (HTTP)
mcporter config add apikeys http http://127.0.0.1:8086/mcp
mcporter config add filesystem http http://127.0.0.1:8083/mcp
```

Локальный запуск в stdio-режиме (без systemd):

```bash
MCP_TRANSPORT=stdio python3 bin/apikeys-server.py
```

## Тесты и CI

```bash
pip install pytest
pytest -v
```

CI: `.github/workflows/tests.yml` запускает `pytest` на каждый push/PR. Тесты покрывают юнит-логику (`_mask_key`, выбор транспорта) и не поднимают реальные серверы.

## Планируемые серверы

- **memory-server** — семантический поиск по памяти лаборатории (бэклог)
- **status-server** — приборная панель лабы: systemd, Docker, диск, инциденты, cron (бэклог)
- **shell-server** — безопасный exec по allowlist (бэклог, опасный)

## Документация

- [docs/README.md](docs/README.md)
- [docs/apikeys-server.md](docs/apikeys-server.md)
- [CHANGELOG.md](CHANGELOG.md)

## Лицензия

Внутренний проект LabDoctorM.

## MCP Memory Server (bin/memory-server.py)

Stateful MCP-фасад над FAISS/ONNX для семантической памяти лабы.

**Транспорт:** Streamable HTTP на `127.0.0.1:8087` (systemd `mcp-memory.service`).
**Статус:** Product Ready (eager preload, in-memory keyword-fallback, p95 monitoring, normalized cache).

**Тулы:**
- `lab_memory_search(query, top_k=5, threshold=0.3, agent="", project="", source="", date="", metadata_only=False)` — семантический поиск с фильтрами по workspace/проекту/источнику/дате. `metadata_only=True` отдаёт только карточку (без текста) для экономии токенов.
- `lab_memory_get_chunk(id, max_chars=4000)` — докачать полный текст чанка по id.
- `lab_memory_stats()` — ready-флаг, размер индекса, cache hit-ratio, avg/p95 latency.
- `lab_memory_reload()` — горячая перезагрузка индекса (после реиндекса Штрейкбрехером).

**Verifying:** `python3 scripts/mcp-verify.py --url http://127.0.0.1:8087/mcp --list-tools`
