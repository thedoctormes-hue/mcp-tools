# memory-gateway — MCP Semantic Memory Gateway

Единое окно доступа 8 агентов OpenClaw к семантической памяти лаборатории
(AnythingLLM). Гибридный retrieval: **vector** (`/vector-search`) + **lexical**
(FTS5/BM25 по `lexical.db`), слияние через **score-calibrated fusion** (по
умолчанию `weighted`; RRF — фолбэк через `fusion="rrf"`). Только сырые данные —
никаких `/chat` и LLM-прослоек.

## Инструменты (MCP tools)

- `search_memory(query, top_k=5, workspace=None)` — гибридный поиск. Возвращает
  чистый JSON: `{query, count, results[], degraded, layers, latency_ms}`.
  Каждый результат: `{doc_id, title, workspace, text, sources[], vector_score,
  lexical_score, rrf_score}`.
- `get_document(doc_id, max_chars=20000)` — полный сырой текст документа
  (из `lexical.db`; fallback — метаданные AnythingLLM API).
- `gateway_health()` — диагностика: токен, `lexical.db`, число workspace,
  состояние ALM (`vector_layer.vector_count`), **телеметрия latency** ALM
  (`last_ms / p50_ms / p95_ms`, `inflight_limit`).

## Архитектура

- Векторный слой: официальный REST API AnythingLLM, Bearer из
  `secrets/anythingllm_token.txt`. Параллельный опрос workspace через
  пул потоков, ограниченный семафором `VECTOR_MAX_INFLIGHT` (защита ALM от
  перегруза). Ответы кэшируются (`MG_VECTOR_CACHE_TTL`, см. ниже).
- Лексический слой: read-only (`mode=ro`) доступ к `lexical.db` (FTS5/BM25).
- Слияние: score-calibrated fusion (нормализация vector-cosine + lexical-BM25
  в общую 0..1 шкалу, взвешенная сумма) + совокупный порог + дедуп по basename.
- Надёжность: таймауты на каждый запрос, деградация при падении одного слоя
  (второй всё равно отдаёт данные), логирование в `logs/memory-gateway.log`.

## Запуск

### stdio (per-agent, legacy)

```
python3 /root/LabDoctorM/projects/mcp-tools/memory-gateway/run.py
```
(транспорт stdio — по умолчанию, `MG_TRANSPORT=stdio`).

### streamable-http (централизованный, рекомендуется)

Один процесс на `:8091`, к которому подключаются все 8 агентов (общий
rate-limit + телеметрия):

```
MG_TRANSPORT=streamable-http MG_PORT=8091 python3 run.py
```

или через systemd: `systemd/memory-gateway.service` (уже установлен и enabled).
В `openclaw.json` агентам прописан `transport: streamable-http`,
`url: http://127.0.0.1:8091/mcp`.

## Конфигурация (env, префикс `MG_`)

- `MG_ALM_BASE` (default `http://127.0.0.1:3002/api/v1`)
- `MG_TOKEN_FILE`, `MG_LEXICAL_DB`, `MG_MAP_FILE`
- `MG_LIST_TIMEOUT` (15), `MG_SEARCH_TIMEOUT` (30)
- `MG_DEFAULT_TOP_K` (5), `MG_MAX_TOP_K` (25)
- `MG_FUSION_MODE` (`weighted`|`rrf`, default `weighted`),
  `MG_FUSION_VECTOR_WEIGHT` (0.6), `MG_FUSION_MIN_COMBINED` (0.05)
- `MG_VECTOR_MAX_WORKERS` (6), `MG_VECTOR_MAX_INFLIGHT` (4, семафор на ALM)
- `MG_VECTOR_CACHE_TTL` (120 с, кэш ответов ALM), `MG_VECTOR_CACHE_MAX` (256)
- `MG_EXPAND_PARAGRAPHS` (2), `MG_EXPAND_MAX_CHARS` (6000) — Context Assembly
- `MG_RRF_K` (60), `MG_TRANSPORT` (stdio|streamable-http), `MG_HOST`, `MG_PORT`

## Тесты

```
cd /root/LabDoctorM/projects/mcp-tools/memory-gateway
python3 -m pytest -q
```

Тесты изолированы от сети: векторный слой мокается (вкл. contract-test ALM на
mock HTTP-сервере + live-probe), лексический — на временном FTS5. Проверяются
fusion/дедуп, валидация, get_document, деградация, **контракт ALM**, кэш.

## Мониторинг здоровья (P5-2)

`systemd/memory-gateway-health.{service,timer}` (enabled, каждые 10 мин)
дёргает `gateway_health()` и при `ok=False` / p95 latency > 2000 мс пишет алерт
в `/root/LabDoctorM/.ops/shared/memory-gateway-health/alerts.log` + systemd failed.

## Ограничения

- Правка исходников AnythingLLM (`/app/server`) запрещена.
- Только официальный REST API AnythingLLM через Bearer-токен.
- Никаких `/chat` и LLM-синтезов — исключительно retrieval сырых данных.
- Кэш vector-ответов имеет TTL (по умолч. 120 с) — семпамять не гарантирует
  абсолютный real-time (документы меняются редко, этого достаточно).
