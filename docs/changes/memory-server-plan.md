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
