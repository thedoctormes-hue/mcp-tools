# mcp-gatekeeper — MCP-привратник портов/таймеров

Единый MCP-сервер = привратник. Агент **не должен** напрямую занимать порт/таймер — только через этот сервер. Принудительная медиация работает на слое systemd через shim (см. `shim/README.md`; устанавливается как `/usr/bin/systemctl` через `dpkg-divert`). **Без shim** (или до eBPF, Фаза 3) прямой `bind()` и `.timer`/cron обходят gatekeeper. Все решения принимает детерминированный **PDP (policy-as-code), БЕЗ LLM в ядре**.

Контракт: [`docs/CONTRACT.md`](../mcp-gatekeeper/docs/CONTRACT.md).

## Структура

```
mcp-gatekeeper/
├── bin/
│   ├── mcp-gatekeeper-server.py   # MCP-сервер + CLI-режим
│   └── register-port-timer.sh      # скрипт-зародыш для агентов
├── policies/
│   └── policy_v1.yaml              # policy-as-code (источник истины для PDP)
├── systemd/
│   └── mcp-gatekeeper.service      # юнит (Type=simple, без watchdog)
├── docs/
│   └── CONTRACT.md                 # контракт (зона Ворона)
├── data/                           # port-timer-log.jsonl + leases.json (gitignored)
└── tests/                          # unit PDP + integration systemd
```

## Эндпоинты (MCP-инструменты)

| Инструмент | Назначение |
|------------|------------|
| `register_port` | только порт |
| `register_timer` | только таймер |
| `register_service` | порт + таймер **атомарно** (один `request_id`) |
| `release_resource` | освобождение по `request_id` |
| `heartbeat` | продление lease (сброс таймаута) |
| `transfer_lease` | handoff lease между агентами (project-scoped) |
| `list_leases` | список активных lease |
| `check_health` | здоровье + PDP-счётчики |

## PDP-цепочка (policy-as-code)

`Identity → Диапазон портов → Квота → Резерв → Дедуп → Justification →
Least-privilege → Project-scoped lease → Root backdoor`

- **Identity** — агент известен (см. `policy_v1.yaml`, `agents`).
- **Диапазон портов** — глобальный `[1024, 65535]` (per-agent пулов **НЕТ**;
  ЗавЛаб 12.07: любой агент может брать любой порт по назначению).
  Зоны назначений: API 8000–8099, вспомогательные 8100–8199, метрики
  9000–9099 (см. `policies/policy_v1.yaml` и `docs/PORT_REGISTRY.md`).
- **Квота** — ≤3 порта, ≤5 таймеров на агента.
- **Резерв** — блок <1024 и зарезервированные порты из
  `policies/policy_v1.yaml` → `reserve.blocked_ports` (**единый источник**;
  см. также `docs/PORT_REGISTRY.md`).
- **Дедуп** — таймер уникален по (action+schedule); порт не занят (с подсказкой
  свободного порта при отказе).
- **Justification** — `what_for` обязателен; v1 = точный (exact) match дедупа
  оправданий. v2 (семантика, fail-open) — заглушка, готова к ONNX+FAISS.
- **Least-privilege** — `lease_user` записывается в lease (метаданные).
  Внимание: в лабе все сервисы бегут от root (non-root юзеров нет), поэтому
  `run_as=root` **разрешён для известных агентов**; принудительного понижения
  прав нет. `as_root=True` обходит ВСЕ PDP-проверки (кроме аудита `BYPASS=root`)
  для агентов из `authorized_root_agents`.
- **Project-scoped lease** — ресурс за `project_id` + агент-арендатор +
  heartbeat; handoff между агентами; таймаут heartbeat → авто-освобождение.
- **Root backdoor** — `as_root=True` обходит проверки, но пишет `BYPASS=root`
  в журнал (отключается `allow_root_backdoor: false`).

Любой отказ — с понятной причиной (и подсказкой свободного порта).

## Журнал

Каждое действие атомарно (fcntl + fsync) пишет JSONL
`data/port-timer-log.jsonl`: `request_id`, `when`, `what_for`, `why`,
`agent`, `project` (+ служебные поля).

## Запуск

```bash
# Локально (stdio)
python3 bin/mcp-gatekeeper-server.py

# HTTP (systemd) — см. systemd/mcp-gatekeeper.service
MCP_TRANSPORT=http MCP_PORT=8888 python3 bin/mcp-gatekeeper-server.py
```

### CLI-режим (для скриптов/CI)

```bash
python3 bin/mcp-gatekeeper-server.py register-port --agent raven --project X --port 8081 --what-for "api gw"
python3 bin/mcp-gatekeeper-server.py register-service --agent raven --project X --port 8081 --action "w.sh" --schedule "*/5 * * * *" --what-for "poll"
python3 bin/mcp-gatekeeper-server.py release --request-id rk-xxxx
```

### Скрипт-зародыш (агентам)

```bash
bin/register-port-timer.sh port   --agent raven --project X --port 8081 --what-for "api gw"
bin/register-port-timer.sh timer  --agent raven --project X --action "backup.sh" --schedule "0 3 * * *" --what-for "nightly"
bin/register-port-timer.sh service ...
bin/register-port-timer.sh release --request-id rk-xxxx
```

## systemd

```bash
sudo cp systemd/mcp-gatekeeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-gatekeeper
sudo systemctl status mcp-gatekeeper
```

- `Type=simple`: **без watchdog** — «жив ли сервер» доказывается ответом на
  реальный запрос агента (событийно). Клиент (`register-port-timer.sh`) ставит
  таймаут 5с на `register_*`; при таймауте → `GATEKEEPER_TIMEOUT` в журнале +
  эскалация без auto-restart.
- `Restart=on-failure` + `StartLimitBurst=3`: защита от crash-loop.
- Config validation **fail-fast**: невалидная политика → быстрый выход.
- Log rate-limit защищает диск.

## Установка shim (обязательная медиация, Слой 1+2)

Без shim gatekeeper — только advisory-реестр: прямой `bind()` и `.timer`/cron
его обходят. Установка описана в `shim/README.md` (ключевое — `dpkg-divert`
оригинального `systemctl` и копия wrapper на `/usr/bin/systemctl`). В этой
инсталляции shim уже установлен и активен.

## Тесты

```bash
pytest mcp-gatekeeper/tests -q
```

Покрыто: вся PDP-цепочка (unit), атомарность `register_service`, handoff,
reaper/lease-timeout, root backdoor, аудит-журнал, fail-fast, одноразовые
уведомления systemd (READY/STOPPING), реальный MCP stdio-транспорт, ключи
юнит-файла (событийная модель: без WatchdogSec).
