# Lab Research MCP Server

Нативный MCP-сервер — единая «фронт-дверь» к поиску лаборатории. Обёртка над
**Unified Search Gateway** (SearXNG на `:8889`) и оркестратором `/research`.

## Назначение

Даёт всем агентам типизированный инструмент поиска вместо того, чтобы каждый
агент сам читал SKILL.md и дёргал оркестратор. Ригор (verify, merge, freshness,
детект противоречий) инкапсулирован в оркестраторе — агент получает уже
проверенный результат.

## Инструменты

- `web_search(query, max_results=10, engines="")` — сырой поиск через SearXNG.
  `engines` опционально фильтрует по движку/категории (по умолчанию — все
  активные движки, включая платные пулы и бесплатные).
- `deep_research(query, count=10)` — полноценный deep research через
  `search-orchestrator.sh <query> deep_research <count>` (Tavily/Firecrawl/
  TinyFish/SearXNG, merge+dedup+freshness+contradictions+синтез).

## Конфигурация (env)

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `SEARXNG_URL` | `http://localhost:8889/search` | URL gateway |
| `LABSEARCH_ORCHESTRATOR` | `.../free-api-hunter/scripts/search-orchestrator.sh` | путь к оркестратору |
| `LABSEARCH_ENGINES` | `""` (все) | фильтр движков для `web_search` |
| `MCP_TRANSPORT` | `stdio` | `http` для systemd, `stdio` для локального |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8089` | HTTP-адрес |

## Деплой

Сервер зарегистрирован в OpenClaw как HTTP MCP-сервер (как и прочие mcp-tools
сервера):

```bash
# systemd (HTTP режим)
sudo systemctl enable --now mcp-lab-research
# регистрация в OpenClaw
openclaw mcp add lab-research --url http://127.0.0.1:8089/mcp --transport streamable-http
```

## Связь с архитектурой

- Backend (SearXNG + пулы + failover + кэш) живёт в проекте `free-api-hunter`.
- Front-door (этот сервер) живёт в `mcp-tools` — единственный правильный дом
  для MCP-серверов лаборатории.
- Связь только через `SEARXNG_URL` (HTTP), без общего состояния.
