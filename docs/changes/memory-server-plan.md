<!-- SEMDEP -->
> ⚠️ **НЕАКТУАЛЬНО / SUPERSEDED (RUL-010).** Документ описывает пре-ALM стек семантической памяти лаборатории: `lab_search.py` / `labsearch` / `onnx-embedder :8082` / `mcp-memory :8087` / `Context API :8100` / FAISS+ONNX / прямые вызовы ALM `:3002`. Все эти пути **УДАЛЕНЫ и ЗАПРЕЩЕНЫ** с 15.07.2026. Единственный рабочий путь семантической памяти — MCP-инструмент **`memory-gateway__search_memory`** (сервер `memory-gateway`, 127.0.0.1:8091, бэкенд ALM/AnythingLLM). Описанные здесь команды/порты/сервисы — НЕ рабочие; не используйте их. Код, всё ещё ссылающийся на этот стек, — устаревшая (stale) зависимость, требует миграции на `memory-gateway__search_memory` (эскалация ЗавЛабу).

## Изменение: memory-server.py — MCP-сервер для семантического поиска

- **Что:** Создать `bin/memory-server.py` — MCP-сервер, оборачивающий lab_search.py в один MCP-инструмент `search(query, limit)`
- **Почему:** labsearch сейчас вызывается через exec (`python3 lab_search.py search "query"`). Это медленно, неудобно, нет стандартного интерфейса. MCP-сервер даст единообразный доступ из всех агентов через mcporter.
- **Риск:** Low (только чтение, сетевой поверхности нет — stdio transport)
- **Rollback:** Удалить `bin/memory-server.py`, убрать из `mcporter config`
- **Влияние:** Агенты получат `memory.search` tool, скорость вызова упадёт (MCP overhead ~50ms vs exec ~200ms)

### Выполнение

1. Написать memory-server.py на FastMCP
2. Реализовать один инструмент: `search(query: str, limit: int = 5)`
3. Вызывать `lab_search.py` через subprocess импорт или прямой вызов
4. Валидация: limit ≤ 20, санитизация query
5. Audit log каждого поиска
6. Тест: `python3 bin/memory-server.py` → mcporter list memory → видит tool

### Проверка

- `mcporter list memory --schema` → показывает search tool
- `mcporter call memory.search query="инциденты"` → возвращает результаты
- `python3 -m pytest tests/test_memory_server.py` → passed
