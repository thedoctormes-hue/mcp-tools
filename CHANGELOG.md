---
description: "mcp-tools — история изменений"
type: changelog
last_reviewed: 2026-06-21
last_code_change: 2026-06-21
status: active
---

# Changelog

## [0.3.0] — 2026-07-09

### Added
- **Фаза 1**: MCP-серверы с HTTP-транспортом (streamable-http) как systemd-юниты
  - **apikeys-server** (bin/apikeys-server.py): раздача free API ключей, порт 8086
    - 4 инструмента: `get_key`, `list_providers`, `get_provider_docs`, `check_health`
    - 8 провайдеров; маскировка ключей, allowlist провайдеров, read-only доступ
    - Интеграция с vault/free-api-hunter
  - **filesystem-server** (bin/filesystem-server.py): read-only whitelist, порт 8083
    - инструменты: `read_file`, `list_dir`, `search_files`
- Юнит-тесты (tests/test_mcp_tools.py): покрытие `_mask_key` и выбора транспорта
- CI: `.github/workflows/tests.yml` (pytest)

### Changed
- Серверы поддерживают оба режима транспорта через `MCP_TRANSPORT` (stdio | http)
- Под systemd запускается HTTP на 127.0.0.1 (8086 / 8083)
- Обновлена документация (README/PROJECT/docs) под реальное состояние

## [Unreleased]

- Создан базовый CHANGELOG.
