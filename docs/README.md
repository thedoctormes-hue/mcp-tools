# mcp-tools — Документация

## filesystem-server

**Файл:** `bin/filesystem-server.py`
**Статус:** ✅ production-ready

Инструменты:
- `read_file(path)` — читать файлы (только в whitelisted директориях)
- `list_dir(path)` — список содержимого директорий
- `search_files(pattern, path)` — поиск файлов по glob

Ограничения:
- Только `/root/LabDoctorM/workspaces/` и `/root/LabDoctorM/projects/`
- Максимальный размер файла: 1MB
- Read-only режим

## apikeys-server

**Файл:** `bin/apikeys-server.py`
**Статус:** ✅ stable

Инструменты:
- `get_key(provider)` — получить API ключ и метаданные
- `list_providers()` — список доступных провайдеров
- `get_provider_docs(provider)` — документация по провайдеру
- `check_health(provider)` — проверить доступность API

Поддерживаемые провайдеры:
- Cerebras (verified) — gpt-oss-120b, zai-glm-4.7
- Cloudflare (verified) — Edge inference
- Pollinations (verified) — 23 free models
- Mistral (verified) — mistral-*
- Cohere (blocked) — command-*
- Gemini (rate_limited) — gemini-*
- OCR.space (verified) — OCR API
- ElevenLabs (verified) — TTS

## memory-server (запланирован)

**Файл:** `bin/memory-server.py`
**Статус:** ⏸ в бэклоге

Инструменты (планируемые):
- `search(query, limit, min_score)` — семантический поиск
- `get_chunk(chunk_id)` — получить чанк
- `index_status()` — статус индекса

## status-server (запланирован)

**Файл:** `bin/status-server.py`
**Статус:** ⏸ в бэклоге

Инструменты (планируемые):
- `get_status()` — агрегация статуса
  - systemd сервисы
  - Docker контейнеры
  - Дисковое пространство
  - Инциденты
  - Cron задачи

## shell-server (запланирован)

**Файл:** `bin/shell-server.py`
**Статус:** ⏸ в бэклоге (ОПАСНЫЙ)

Инструменты (планируемые):
- `execute_command(command)` — выполнение с allowlist

Безопасность:
- Только разрешённые команды
- Таймаут 30 секунд
- Audit log
