# searxng-gateway — MCP веб-поиск + deep-research для агентов лабы

MCP-сервер (stdio), дающий агентам OpenClaw типизированный веб-поиск
через локальный **SearXNG** (`http://localhost:8889`) + глубокое исследование
через `/research` оркестратор (fan-out Tavily/Firecrawl/TinyFish/SearXNG,
merge + dedup + freshness + синтез).

## Инструменты (MCP)

- `search_web(query, max_results=10, categories=None, language="auto", safesearch=0)`
  — сырой поиск через SearXNG. Возвращает `{query, count, results[], latency_ms}`.
- `deep_research(query, count=10)` — **комбайн**: веб (`answer`) + семантическая
  память лабы (`semantic_memory`) в ОДНОМ вызове (см. Spayka ниже).
- `search_memory(query, top_k=5, workspace=None)` — прокси к `memory-gateway`
  (удобно вызывать из одного сервера).
- `prompts` / `resources` — MCP-метаданные.

## Spayka: deep_research = веб + семпамять лабы

`deep_research` тянет в одном вызове ДВА слоя и возвращает их вместе:

```json
{
  "query": "...",
  "answer": "<веб-синтез от оркестратора /research>",
  "semantic_memory": {
    "query": "...",
    "count": 5,
    "results": [ { "doc_id", "title", "workspace", "text",
                   "vector_score", "lexical_score", "rrf_score" } ],
    "degraded": false,
    "layers": { "vector": 15, "lexical": 15 }
  },
  "degraded": false
}
```

- **Веб-слой:** `subprocess` → `search-orchestrator.sh <query> deep_research <count>`.
- **Семантический слой:** `memory_gateway.search.hybrid_search(query, top_k,
  expand_context, fusion)` — vector ALM + lexical FTS5 (RRF/weighted).
- Пакет `memory_gateway` недоступен в PYTHONPATH (установлен только
  `searxng-gateway`), поэтому в `server.py` добавляется `sys.path` к
  `mcp-tools/memory-gateway`. Импорт завёрнут в try/except — при недоступности
  слоя `semantic_memory` помечается `degraded`, веб продолжает работать.

### Грациозная деградация
- Веб упал (оркестратор не найден / returncode≠0) → `answer=null`,
  `degraded=true`, но `semantic_memory` отдаётся.
- Семпамять упала (ALM недоступен) → `semantic_memory.degraded=true`,
  но `answer` отдаётся.
- Оба слоя ок → `degraded:false`.

## Конфигурация (env)

| Переменная | Дефолт | Назначение |
|------------|--------|------------|
| `SEARXNG_URL` | `http://localhost:8889` | URL SearXNG |
| `DEEP_RESEARCH_ORCHESTRATOR` | `.../free-api-hunter/scripts/search-orchestrator.sh` | путь к оркестратору |
| `DEEP_RESEARCH_TIMEOUT` | `240` | таймаут оркестратора (с) |
| `SEMANTIC_ENABLED` | `1` | вкл/выкл семпамять в deep_research |
| `SEMANTIC_TOP_K` | `5` | сколько результатов лабы тянуть |
| `SEMANTIC_EXPAND` | `1` | Context Assembly (расширение пассажа) |
| `SEMANTIC_FUSION` | `weighted` | `weighted` \| `rrf` |

## Регистрация в OpenClaw

```json
{
  "mcp": {
    "servers": {
      "searxng-gateway": {
        "command": "python3",
        "args": ["-m", "searxng_gateway.server"],
        "env": { "SEARXNG_URL": "http://localhost:8889" }
      }
    }
  }
}
```

## Тесты

```bash
cd searxng-gateway
python3 -m pytest -q
```

Тесты изолированы: `hybrid_search` мокается (не зависят от живого ALM),
оркестратор мокается (не дёргает реальный веб).

## Зависимости

`mcp>=1.20`, `requests>=2.28`.
