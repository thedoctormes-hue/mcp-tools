---
name: MCP Tools
owner: DoctorM&Ai
type: devtools
status: active
last_reviewed: 2026-07-09
last_code_change: 2026-07-09
priority: high
stack: [Python, FastMCP]
version: "0.3.0"
---

# MCP Tools

Инструменты для Model Context Protocol (MCP) — серверы и утилиты для интеграции MCP-совместимых инструментов с агентами лаборатории.

## Готовые серверы

| Сервер | Файл | Статус | Назначение |
|--------|------|--------|------------|
| filesystem-server | bin/filesystem-server.py | ✅ ready | Чтение файлов (whitelist) |
| apikeys-server | bin/apikeys-server.py | ✅ stable | Доступ к API ключам |
| memory-server | (planned) | ⏸ backlog | Поиск по семантической памяти |
| status-server | (planned) | ⏸ backlog | Приборная панель лабы |
| shell-server | (planned) | ⏸ backlog | Безопасный exec (опасный) |

## Быстрый старт

```bash
cd /root/LabDoctorM/projects/mcp-tools

# Проверить доступные серверы
ls bin/

# Вызвать инструмент через mcporter
mcporter call --stdio "python3 bin/apikeys-server.py" list_providers
mcporter call --stdio "python3 bin/apikeys-server.py" get_key provider=cerebras
```

## Установка (HTTP режим)

```bash
# Запустить systemd сервисы
sudo systemctl enable --now mcp-filesystem mcp-apikeys

# Добавить в mcporter конфиг
mcporter config add filesystem http http://127.0.0.1:8083/mcp
mcporter config add apikeys http http://127.0.0.1:8086/mcp
```
