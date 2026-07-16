# Agent Onboarding — Semantic Memory Gateway (MCP `memory-gateway`)

Канонический шаблон системной директивы для агентов лаборатории.
Описывает, как агенты получают доступ к семантической памяти через MCP-инструмент `search_memory`.

## Что развёрнуто

- MCP-сервер `memory-gateway` зарегистрирован в `mcp.servers` (`~/.openclaw/openclaw.json`),
  запуск через stdio: `python3 /root/LabDoctorM/projects/mcp-tools/memory-gateway/run.py`.
- Инструменты (3): `memory-gateway__search_memory`, `memory-gateway__get_document`, `memory-gateway__gateway_health`.
- Сервер выполняет гибридный поиск: AnythingLLM vector-search + FTS5/BM25 `lexical.db`,
  слияние RRF. Только сырые данные, без `/chat` и LLM-синтеза.

## Директива (деплоится как `APPEND_SYSTEM.md` в `agentDir` каждого агента)

OpenClaw автоматически дописывает содержимое `<agentDir>/APPEND_SYSTEM.md` к системному
промпту агента (механизм `discoverAppendSystemPromptFile`). Править core-файлы агентов
(AGENTS.md и т.п.) не требуется — достаточно положить/удалить `APPEND_SYSTEM.md`.

Текст директивы:

> ## Семантическая память лаборатории — ЕДИНСТВЕННЫЙ СПОСОБ: MCP `memory-gateway`
>
> Доступ к семантической памяти лаборатории (база знаний: vector + lexical, RRF)
> осуществляется ИСКЛЮЧИТЕЛЬНО через MCP-сервер `memory-gateway`:
>
> - `memory-gateway__search_memory(query, top_k?, workspace?)` — гибридный семантический поиск (ОСНОВНОЙ инструмент).
> - `memory-gateway__get_document(doc_id)` — полный текст документа по `doc_id` из результатов поиска.
> - `memory-gateway__gateway_health()` — проверка здоровья шлюза и слоёв.
>
> ПРАВИЛА:
> - При любом вопросе про документации лабы, настройки, скрипты, ключи, архитектуру,
>   инциденты, паттерны — ОБЯЗАН сначала вызвать `memory-gateway__search_memory` и
>   опираться ТОЛЬКО на полученные факты.
> - Факты — только из `memory-gateway`. LLM-синтез поверх памяти и `/chat` строго запрещены.
> - Не прописывай токены AnythingLLM/OpenRouter: шлюз берёт их из `secrets/` сам.
>
> ⛔ СТРОГО ЗАПРЕЩЕНО (не является рабочим путём):
> - Native `memory_search` OpenClaw — мёртв (ADR-0054; плагины `memory-core`/`memory-wiki` отключены). НЕ вызывать.
> - `lab_search.py` и скилл `labsearch` — выведены из эксплуатации (ALM — единственный стек). НЕ использовать.
> - Прямые REST-вызовы к AnythingLLM в обход MCP-шлюза — запрещены.
>
> Единственный рабочий и канонический стек семантической памяти лаборатории — AnythingLLM (ALM).
> Доступ к нему — ТОЛЬКО через `memory-gateway` (MCP).

## Deployment (какие агенты затронуты)

`agentDir` для каждого агента: `/root/.openclaw/agents/<agent>/agent/`.
Директива развёрнута у 8 агентов: `mangust`, `dominika`, `kotolizator`, `antcat`,
`bestia`, `owl`, `raven`, `streikbrecher`.

## Проверка (E2E)

1. `openclaw mcp show memory-gateway` — сервер зарегистрирован (stdio, command python3).
2. `openclaw mcp probe memory-gateway` — `3 tools, resources, prompts`.
3. Контрольный запуск агента с запросом по базе (напр. «TTS keypool rotation» или
   «AnythingLLM setup»): убедиться, что агент вызвал `memory-gateway__search_memory` и ответил строго
   на основе полученного контекста (см. логи шлюза `logs/memory-gateway.log` и логи OpenClaw).

## Безопасность

- Токены AnythingLLM/OpenRouter НЕ хранятся в конфиге агентов и openclaw.json.
  Шлюз читает их из `secrets/` (perms 600) сам.
- `openclaw.json` содержит служебные секреты — НЕ коммитится. Канонический шаблон
  директивы версионируется здесь (в репозитории `mcp-tools`), live-файлы — в `agentDir`.
