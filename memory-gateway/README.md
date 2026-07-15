# memory-gateway — MCP Semantic Memory Gateway

Единое окно доступа 8 агентов OpenClaw к семантической памяти лаборатории
(AnythingLLM). Гибридный retrieval: **vector** (`/vector-search`) + **lexical**
(FTS5/BM25 по `lexical.db`), слияние через **RRF**. Только сырые данные —
никаких `/chat` и LLM-прослоек.

## Инструменты (MCP tools)

- `search_memory(query, top_k=5, workspace=None)` — гибридный поиск. Возвращает
  чистый JSON: `{query, count, results[], degraded, layers, latency_ms}`.
  Каждый результат: `{doc_id, title, workspace, text, sources[], vector_score,
  lexical_score, rrf_score}`.
- `get_document(doc_id, max_chars=20000)` — полный сырой текст документа
  (из `lexical.db`; fallback — метаданные AnythingLLM API).
- `gateway_health()` — диагностика: токен, `lexical.db`, число workspace.

## Архитектура

- Векторный слой: официальный REST API AnythingLLM, Bearer из
  `secrets/anythingllm_token.txt` (600). Параллельный опрос workspace.
- Лексический слой: read-only (`mode=ro`) доступ к `lexical.db` (FTS5/BM25).
- Слияние: Reciprocal Rank Fusion (k=60) + дедуп по basename документа.
- Надёжность: таймауты на каждый запрос, деградация при падении одного слоя
  (второй всё равно отдаёт данные), логирование в `logs/memory-gateway.log`.

## Запуск

### stdio (рекомендуется для агентов OpenClaw)

Прописать в конфиге агента команду запуска:

```
python3 /root/LabDoctorM/projects/mcp-tools/memory-gateway/run.py
```

(транспорт stdio — по умолчанию, `MG_TRANSPORT=stdio`).

### streamable-http (сетевой деплой)

```
MG_TRANSPORT=streamable-http MG_PORT=8091 python3 run.py
```

или через systemd: `systemd/memory-gateway.service`.

## Конфигурация (env, префикс `MG_`)

- `MG_ALM_BASE` (default `http://127.0.0.1:3002/api/v1`)
- `MG_TOKEN_FILE`, `MG_LEXICAL_DB`, `MG_MAP_FILE`
- `MG_LIST_TIMEOUT` (15), `MG_SEARCH_TIMEOUT` (30)
- `MG_DEFAULT_TOP_K` (5), `MG_MAX_TOP_K` (25), `MG_RRF_K` (60)
- `MG_TRANSPORT` (stdio|streamable-http), `MG_HOST`, `MG_PORT`

## Тесты

```
cd /root/LabDoctorM/projects/mcp-tools/memory-gateway
python3 -m pytest -q
```

Тесты изолированы от сети: векторный слой мокается, лексический — на временном
FTS5. Проверяются RRF/дедуп, валидация, get_document, деградация.

## Ограничения

- Правка исходников AnythingLLM (`/app/server`) запрещена.
- Только официальный REST API AnythingLLM через Bearer-токен.
- Никаких `/chat` и LLM-синтезов — исключительно retrieval сырых данных.
