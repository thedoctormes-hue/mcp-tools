# Shim для mcp-gatekeeper (Слой 1 + Слой 2)

Прозрачный перехват порядка портов/таймеров. Агенты НЕ меняют привычки —
они вызывают `systemctl`/`crontab` как обычно, но shim невидимо проверяет
каждый порт/таймер через mcp-gatekeeper (порт 8888) ДО применения.

**Это не ограничение свободы — это помощь:** конфликты портов,Reserved-порты
и перерасход квот блокируются автоматически, а нарушения попадают в журнал.

## Слой 1 — принудительный перехват (gateway exec interception)

Файлы:
- `systemctl-wrapper` → устанавливается как `/usr/local/bin/systemctl`
  (приоритет в PATH перед `/usr/bin/systemctl` для агентов через gateway).
- `crontab-wrapper` → устанавливается как `/usr/local/bin/crontab`.
- `gk-register` → лёгкий MCP-клиент (handshake + `register_port`/`register_timer`).
  Возвращает `ALLOW` / `REJECT` / `ERROR`.

Логика wrapper:
1. Парсит аргументы (`enable`/`start`/`restart` + имя юнита).
2. Извлекает порт из юнита (`:PORT` в `ExecStart`/`Environment`).
3. Вызывает gatekeeper через `gk-register` (timeout 5s).
4. `REJECT` → блокирует оригинал, возвращает ошибку агенту.
5. `ALLOW` / `ERROR` (timeout/недоступность) → fail-open, вызывает оригинал.
6. Self-loop защита: не перехватывает `gatekeeper-shim.*` / `mcp-gatekeeper.*`.

## Слой 2 — реактивный backstop (systemd path-unit)

Файлы:
- `gatekeeper-shim.path` → `/etc/systemd/system/gatekeeper-shim.path`
  (мониторит `PathChanged=/etc/systemd/system/`).
- `gatekeeper-shim.service` → `/etc/systemd/system/gatekeeper-shim.service`
  (oneshot, вызывает `gk-scan.sh`).
- `gk-scan.sh` → сканирует все `.service`/`.timer` на порты, регистрирует
  через gatekeeper (аудит обходов gateway).

Self-loop: `gk-scan.sh` игнорирует `gatekeeper-shim.*` и `mcp-gatekeeper.*`.

## Установка

```bash
# Слой 1
install -m755 shim/systemctl-wrapper /usr/local/bin/systemctl
install -m755 shim/crontab-wrapper /usr/local/bin/crontab
install -m755 shim/gk-register /usr/local/bin/gk-register

# Слой 2
install -m644 shim/gatekeeper-shim.path /etc/systemd/system/
install -m644 shim/gatekeeper-shim.service /etc/systemd/system/
install -m755 shim/gk-scan.sh /usr/local/bin/gk-scan.sh
systemctl daemon-reload
systemctl enable --now gatekeeper-shim.path
```

## Тестирование (пройдено)

- ALLOW: `systemctl enable` юнита с портом 8081 (в пуле raven) → enabled OK.
- REJECT: `systemctl enable` юнита с портом 8086 (reserved) → blocked.
- Backstop: создание юнита напрямую в `/etc/systemd/system/` → path-unit
  регистрирует его через gatekeeper (видно в `data/port-timer-log.jsonl`).

## Ограничения

- Gatekeeper должен быть жив на `127.0.0.1:8888`. При недоступности — fail-open
  (порядок не блокируется, но и не аудируется до восстановления).
- Gatekeeper видит только порты, прошедшие через него. Внешние сервисы
  (например, snablab на 8200, не зарегистрированный через gatekeeper) НЕ видны —
  нужно добавить их в `PORT_REGISTRY` / политику как reserved или агента.
- В некоторых средах (exec через gateway) `systemctl enable` может вернуть
  `Bad message` (glitch systemd, не shim) — wrapper корректно вызывает оригинал.
- Слой 3 (seccomp-bpf/eBPF на `bind()`) — будущее, не реализован.

## Связь с ADR

ADR-0053 (DoctorM_and_Ai/docs/adr/) — архитектура shim, fact-check паттернов,
обоснование "польза, не тюрьма".
