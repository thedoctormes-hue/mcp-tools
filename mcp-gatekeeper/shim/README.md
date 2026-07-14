# Shim для mcp-gatekeeper (Слой 1 + Слой 2)

Прозрачный перехват порядка портов/таймеров. Агенты НЕ меняют привычки —
они вызывают `systemctl`/`crontab` как обычно, но shim невидимо проверяет
каждый порт/таймер через mcp-gatekeeper (порт 8888) ДО применения.

**Это не ограничение свободы — это помощь:** конфликты портов, Reserved-порты
и перерасход квот блокируются автоматически, а нарушения попадают в журнал.

Реализация соответствует **ADR-0055** (Phases 1, 2, 4). Полный threat-model и
карта «дыра × фаза» — в `DoctorM_and_Ai/docs/adr/ADR-0055-gatekeeper-threat-model-mitigations.md`.

---

## Слой 1 — ОБЯЗАТЕЛЬНАЯ медиация (Фаза 1, ADR-0055)

Файлы:
- `systemctl-wrapper` → устанавливается **как `/usr/bin/systemctl`** (обязательно для ВСЕХ).
- `crontab-wrapper` → устанавливается как `/usr/local/bin/crontab` (PATH-intercept).
- `gk-register` → лёгкий MCP-клиент (handshake + `register_port`/`register_timer`).
  Возвращает `ALLOW` / `REJECT` / `DEAD` (при недоступности Gatekeeper — контракт
  ADR-0054: `GATEKEEPER_DEAD {status:dead, heal, mandatory_retry}` + exit 2).

### Mandatory mediation (обход невозможен мимо PATH)
Оригинальный `systemctl` systemd сохранён через `dpkg-divert`:

```bash
dpkg-divert --divert /usr/bin/systemctl.real --rename /usr/bin/systemctl
install -m755 shim/systemctl-wrapper /usr/bin/systemctl
```

После этого ЛЮБОЙ вызов `systemctl` в системе идёт через обёртку — обход мимо
PATH невозможен (закрывает дыры **1** и **2**). При будущем обновлении пакета
`systemd` dpkg положит новый бинарь в `/usr/bin/systemctl.real`, а наша
обёртка на `/usr/bin/systemctl` останется нетронутой.

### Self-call whitelist (без него — бесконечный цикл / блок heal)
Обёртка НЕ гейтит вызов, если:
- установлен маркер `GK_SHIM_CALLED=1` (мы сами себя рекурсивно вызвали);
- вызывающий — systemd (PPID == 1) или `systemctl`/`daemon-reload` как comm родителя;
- действие направлено на юниты `gatekeeper-shim.*` / `mcp-gatekeeper.*` (heal, path-unit).

Это позволяет systemd управлять собой и даёт возможность «вылечить» Gatekeeper
(`systemctl restart mcp-gatekeeper`), не упершись в собственную блокировку.

### Логика wrapper
1. Парсит аргументы (`enable`/`start`/`restart` + имя юнита `.service/.timer/.socket`). Юнит `.timer` теперь тоже гейтится — shim регистрирует таймер в gatekeeper (Уровень 1-Г, ADR-0056), зеркаля порт-путь.
2. Извлекает порт из юнита только из явных bind-адресов:
   `ListenStream`/`ListenAddress`/`Listen=` в формах `host:port`, `[::]:port`
   и **порт-только** (`ListenStream=48080` — без хоста, добавлено Уровень 1-Г),
   а также `127.0.0.1:`/`0.0.0.0:`/`[::]:`. Unix-сокеты (`/run/x.sock`) не гейтим.
   Порты, объявленные только в `ExecStart --port` / `Environment PORT=` —
   **НЕ извлекаются** (известный gap, ROOT 2; закрывается Фазой 3 eBPF и
   reconcile-loop Слоя 2.5).
3. Вызывает gatekeeper через `gk-register` (timeout 5s) с **реальным агентом** (см. Фаза 2).
4. `REJECT` → **блокирует**, exit 1 (Фаза 4: fail-closed).
5. `GATEKEEPER_DEAD*` → **блокирует**, exit 2, печатает контракт `heal` (ADR-0054).
   `DEAD` (Gatekeeper недоступен) → агент обязан вылечить и повторить.
6. `ALLOW` → выполняет оригинал `/usr/bin/systemctl.real`.
7. Любой иной исход (ERROR/таймаут/пусто) → **блокирует**, exit 2 (Фаза 4: fail-closed,
   не отступает к fail-open).
8. При регистрации shim передаёт `--unit <имя юнита>` (для порта и таймера), чтобы
   lease в gatekeeper хранил привязку к конкретному systemd-юниту (Уровень 1-Г, ADR-0056).
   Повторная регистрация того же порта/таймера тем же агентом (restart) = refresh lease,
   а не плодит новые (идемпотентность, как у портов).

---

## Фаза 2 — Реальный агент (закрывает дыры 4, 5)

Раньше shim хардкодил `agent=shim`. Теперь shim подставляет **РЕАЛЬНОГО**
агента, и Gatekeeper проверяет глобальный диапазон портов + резерв по реальному
агенту (per-agent пулов НЕТ — ЗавЛаб 12.07: любой агент может порты/таймеры).

### Откуда берётся реальный агент
`gk-register` (и wrapper) резолвят агента в порядке:
1. `AGENT_ID` из окружения (установлен в окружении агента);
2. обход вверх по дереву процессов — `AGENT_ID` в `environ` предков;
3. comm процесса-родителя (PPID);
4. иначе — `unknown` (Gatekeeper отвергнет → fail-closed).

> **Требование к агентам:** агент ДОЛЖЕН экспортировать `AGENT_ID` в своём
> окружении. Иначе его деплой будет отвергнут (`unknown` неизвестен политике).
> Операторский fallback: `GK_DEFAULT_AGENT=<agent>` (по умолчанию **`sh`** —
> легитимный инфра-вызыватель; совпадает с `authorized_root_agents`, поэтому
> ручной `systemctl restart` от админа идёт под `sh` и аудируется, но НЕ блокируется).

### `as_root` / bypass — только авторизованные + аудит (дыра 5)
`root backdoor` (`as_root=True`) в Gatekeeper теперь разрешён **только** для
агентов из `policy.gatekeeper.authorized_root_agents`. Неавторизованный агент
получает `REJECT` с пояснением. Любой `as_root`-обход строго аудируется как
`BYPASS=root` в журнале. Для операторского форсирования: `GK_AS_ROOT=1`
(агент должен быть в `authorized_root_agents`).

Поле политики (добавлено для Фазы 2, структурно не ломает `policy_v1.yaml`):

```yaml
gatekeeper:
  allow_root_backdoor: true
  authorized_root_agents: [raven]   # кто может делать as_root-обход
```

---

## Фаза 4 — Fail-closed (доводит дыру 7)

Дыра **7**: агент получил `REJECT`/`DEAD`-контракт, но всё равно задеплоил.
Теперь shim при `REJECT`/`DEAD` (и при любом не-`ALLOW` исходе) **реально НЕ
выполняет** `systemctl enable/start/restart` — exit 1 (REJECT) или exit 2 (DEAD/прочее).
Блокировка сохраняется до heal, отступления к fail-open нет.

Контракт **ADR-0054** сохранён: при недоступности Gatekeeper `gk-register`
возвращает `GATEKEEPER_DEAD {status:dead, heal, mandatory_retry}`, shim печатает
его агенту и exit 2 (оригал НЕ вызывается). Агент обязан вылечить
(`systemctl restart mcp-gatekeeper`) и повторить `register_port`.

---

## Слой 2 — реактивный backstop (systemd path-unit)

Файлы:
- `gatekeeper-shim.path` → `/etc/systemd/system/gatekeeper-shim.path`
  (мониторит `PathChanged=/etc/systemd/system/`).
- `gatekeeper-shim.service` → `/etc/systemd/system/gatekeeper-shim.service`
  (oneshot, вызывает `gk-scan.sh`).
- `gk-scan.sh` → сканирует все `.service` на порты и **только логирует** их
  в аудит-файл (`/var/log/gk-shim-scan.log`); НЕ регистрирует в gatekeeper
  (иначе shim заблокировал бы сам себя — см. комментарий в скрипте). Реальный
  reconcile (запись observed в gatekeeper) — отдельная задача (ADR-0056, шаг Д).

Self-loop: `gk-scan.sh` игнорирует `gatekeeper-shim.*` и `mcp-gatekeeper.*`.

## Установка

```bash
# Слой 1 — ОБЯЗАТЕЛЬНАЯ медиация (Фаза 1)
dpkg-divert --divert /usr/bin/systemctl.real --rename /usr/bin/systemctl
install -m755 shim/systemctl-wrapper /usr/bin/systemctl
install -m755 shim/systemctl-wrapper /usr/local/bin/systemctl   # копия для PATH
install -m755 shim/crontab-wrapper /usr/local/bin/crontab
install -m755 shim/gk-register /usr/local/bin/gk-register

# Слой 2
install -m644 shim/gatekeeper-shim.path /etc/systemd/system/
install -m644 shim/gatekeeper-shim.service /etc/systemd/system/
install -m755 shim/gk-scan.sh /usr/local/bin/gk-scan.sh
systemctl daemon-reload
systemctl enable --now gatekeeper-shim.path
```

## Тестирование

- ALLOW: `AGENT_ID=raven systemctl enable <unit>` с портом 8081 (глобальный диапазон, не резерв) → enabled.
- REJECT (резерв): `AGENT_ID=raven systemctl enable <unit>` с портом 8086 → blocked (exit 1).
- REJECT (вне диапазона): `AGENT_ID=raven systemctl enable <unit>` с портом 70000 → blocked
  (exit 1) — глобальный диапазон `[1024, 65535]`.
- REJECT (неизвестный агент): `systemctl enable <unit>` БЕЗ `AGENT_ID` → агент `unknown`/`sh`
  отвергнут (exit 1) — spoofing закрыт (Фаза 2).
- DEAD: при остановленном Gatekeeper `enable` порт-юнита → exit 2 + контракт `GATEKEEPER_DEAD`
  (Фаза 4 + ADR-0054).

## Ограничения / риски

- Gatekeeper должен быть жив на `127.0.0.1:8888`. При `DEAD` enable блокируется (fail-closed).
  Восстановление: `systemctl restart mcp-gatekeeper`, затем повтор `register_port`.
- **Агенты обязаны экспортировать `AGENT_ID`** — иначе их деплои порт-юнитов блокируются.
- PPID==1 whitelist (systemd-инициированные вызовы) необходим, чтобы не блокировать boot/heal,
  но означает: сервис, запущенный напрямую systemd и вызывающий `systemctl`, тоже не гейтится.
  Доводится Фазой 3 (eBPF LSM `socket_bind`) и Фазой 5 (RBAC на обёртку).
- Слой 3 (seccomp-bpf/eBPF на `bind()`) — будущее (Фаза 3, ADR-0055), не реализован здесь.

## Связь с ADR

- **ADR-0055** (DoctorM_and_Ai/docs/adr/) — threat model и 6 фаз (здесь: Фазы 1, 2, 4).
- ADR-0053 (DoctorM_and_Ai/docs/adr/) — архитектура shim, fact-check паттернов,
  обоснование "польза, не тюрьма".
- ADR-0054 (DoctorM_and_Ai/docs/adr/) — протокол «dead + heal + mandatory_retry»:
  при недоступности Gatekeeper клиент возвращает контракт, агент лечит и повторяет.
