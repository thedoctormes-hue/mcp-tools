# Agent Onboarding — Semantic Memory Gateway (MCP `memory-gateway`)

Канонический шаблон системной директивы для агентов лаборатории.
Описывает, как агенты получают доступ к семантической памяти через MCP-инструмент `search_memory`.

## Что развёрнуто

- MCP-сервер `memory-gateway` зарегистрирован в `mcp.servers` (`~/.openclaw/openclaw.json`),
  запуск через stdio: `python3 /root/LabDoctorM/projects/mcp-tools/memory-gateway/run.py`.
- Инструменты (3): `search_memory`, `get_document`, `gateway_health`.
- Сервер выполняет гибридный поиск: AnythingLLM vector-search + FTS5/BM25 `lexical.db`,
  слияние RRF. Только сырые данные, без `/chat` и LLM-синтеза.

## Директива (деплоится как `APPEND_SYSTEM.md` в `agentDir` каждого агента)

OpenClaw автоматически дописывает содержимое `<agentDir>/APPEND_SYSTEM.md` к системному
промпту агента (механизм `discoverAppendSystemPromptFile`). Править core-файлы агентов
(AGENTS.md и т.п.) не требуется — достаточно положить/удалить `APPEND_SYSTEM.md`.

Текст директивы:

> У тебя есть прямой доступ к семантической памяти лаборатории (базе знаний) через
> подключённый MCP-инструмент `search_memory` (сервер `memory-gateway`).
>
> Правила использования:
> - При любом вопросе, касающемся документации лабы, настроек, скриптов, ключей,
>   архитектуры, инцидентов или паттернов — ОБЯЗАН сначала вызвать `search_memory`
>   и использовать только полученные факты.
> - Для получения полного текста документа по `doc_id` из результатов поиска используй
>   `get_document`.
> - Использование старых чат-костылей (`/chat`, LLM-синтез поверх памяти) и галлюцинации
>   строго запрещены. Факты — только из `search_memory`.
> - Не прописывай токены AnythingLLM/OpenRouter: шлюз берёт их из `secrets/` сам.

## Deployment (какие агенты затронуты)

`agentDir` для каждого агента: `/root/.openclaw/agents/<agent>/agent/`.
Директива развёрнута у 8 агентов: `mangust`, `dominika`, `kotolizator`, `antcat`,
`bestia`, `owl`, `raven`, `streikbrecher`.

## Проверка (E2E)

1. `openclaw mcp show memory-gateway` — сервер зарегистрирован (stdio, command python3).
2. `openclaw mcp probe memory-gateway` — `3 tools, resources, prompts`.
3. Контрольный запуск агента с запросом по базе (напр. «TTS keypool rotation» или
   «AnythingLLM setup»): убедиться, что агент вызвал `search_memory` и ответил строго
   на основе полученного контекста (см. логи шлюза `logs/memory-gateway.log` и логи OpenClaw).

## Безопасность

- Токены AnythingLLM/OpenRouter НЕ хранятся в конфиге агентов и openclaw.json.
  Шлюз читает их из `secrets/` (perms 600) сам.
- `openclaw.json` содержит служебные секреты — НЕ коммитится. Канонический шаблон
  директивы версионируется здесь (в репозитории `mcp-tools`), live-файлы — в `agentDir`.
