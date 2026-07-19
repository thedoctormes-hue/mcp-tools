# audit/ — Gatekeeper Audit live ports (Фаза 6, Слой 2.5, ADR-0055)

Скрипт `gk-audit.sh` периодически снимает `ss -tlnp` и сверяет **каждый**
слушающий TCP-порт хоста с источниками «разрешено». Любой порт вне разрешённых
→ **АЛЕРТ** (вывод + лог + пометка для Telegram). Ничего не блокирует — только
видимость (audit = visibility). Закрывает дыры: **1** (полный пропуск),
**3** (ручной процесс), **7** (игнор REJECT/DEAD), **9** (race/TTL).

## Что проверяет

Порт считается **разрешённым**, если он есть в одном из:

0.5. `audit/trusted_procs.txt` — Слой 0.5: exemption по **реальному бинарю**
    владельца сокета (`/proc/$pid/exe`), а не по номеру порта. Для системных
    демонов с эфемерными портами (containerd, dockerd): бинарь перечисляется
    здесь один раз, и все порты этого процесса не алертятся вне зависимости
    от номера (см. раздел «Что делать при алерте»). Exemption по бинарю
    надёжнее exempt по порту — номер *spoofable* и *эфемерен*, путь к бинарю
    стабилен и не подделывается выбором порта.
1. `policies/policy_v1.yaml` → `reserve.blocked_ports` (резерв PDP, инфра) — **ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ** (Уровень Е ADR-0056).
2. `policies/policy_v1.yaml` → `gatekeeper.listen_port` (собственный порт PDP).
3. `data/leases.json` → **активный** lease (порт задан + `last_heartbeat + lease_timeout > now`).
4. Дополнительно (по умолчанию): порты `< block_privileged_below` (обычно <1024)
   трактуются как системные и разрешены, чтобы не алертить ssh/dns и т.п.

Всё остальное слушающее → АЛЕРТ.

> `docs/PORT_REGISTRY.md` — УСТАРЕЛО как ручной allowlist. Это read-only вид,
> генерируемый `scripts/gen-port-registry.sh` из policy. Правь policy, не md.

> Намеренно: порты из пулов агентов (8080–8199 и др.) **не** пре-разрешены. Агентский
> порт разрешён только при активном lease. Задеплоенный в обход Gatekeeper порт
> (ручной процесс, проигнорированный REJECT) → АЛЕРТ.

## Установка (systemd timer, ~7 мин)

```bash
# от root, с подтверждения координатора
cp systemd/gatekeeper-audit.service /etc/systemd/system/
cp systemd/gatekeeper-audit.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gatekeeper-audit.timer

# проверить статус таймера и последний прогон:
systemctl status gatekeeper-audit.timer
journalctl -u gatekeeper-audit.service -n 50
```

Запустить вручную (проверка):

```bash
bash audit/gk-audit.sh
# или через юнит:
systemctl start gatekeeper-audit.service
```

## Интерпретация алертов

Алерт-строка в `/var/log/gk-audit.log` и в `journalctl -u gatekeeper-audit.service`:

```
2026-07-12T10:34:00Z ALERT port=15000 unauthorized listening socket: \
  LISTEN 0 128 0.0.0.0:15000 0.0.0.0:* users:(("python3",pid=4242,fd=5)) [TELEGRAM]
```

- **`port=N`** — подозрительный слушающий порт.
- **`users:(("имя",pid,fd))`** — кто и какой процесс слушает (нужен root для `ss -p`).
- **`[TELEGRAM]`** — пометка: строка отправлена в Telegram нотификатором
  `audit/gk_notify.py` (подключён через `GK_NOTIFY` в юните). Саму строку можно
  дохватить внешним коллектором: `grep '[TELEGRAM]' /var/log/gk-audit.log`.
- Нотификатор вызывается самим `gk-audit.sh` (переменная `GK_NOTIFY`):
  `"$GK_NOTIFY" "GK-AUDIT ALERT port=... detail"`. Подробности — в разделе
  «Telegram-алерты» ниже.

**Что делать при алерте:**
1. `ss -tlnp | grep :N` — подтвердить и найти процесс/PID.
2. Разобраться, КТО слушает порт, и выбрать правильный вид exemption — иначе
   процесс научится костылям:

   **а) СТАБИЛЬНЫЙ инфра-порт (постоянный, известный заранее)** — Postgres 5432,
      nginx 8080, собственный PDP-порт и т.п. Порт не меняется между рестартами
      → легитимно добавить его в `policies/policy_v1.yaml` →
      `reserve.blocked_ports`, перегенерировать `docs/PORT_REGISTRY.md`
      (`bash scripts/gen-port-registry.sh`) и закоммитить. Алерт перестанет
      срабатывать.

   **б) СИСТЕМНЫЙ ДЕМОН с ЭФЕМЕРНЫМИ портами (containerd, dockerd и подобные)** —
      рантайм-порты меняются при КАЖДОМ рестарте (36401 → 46199 → 39727 → …).
      **НЕ писать номер порта в `blocked_ports`** — это породит whack-a-mole
      (каждый рестарт → новый алерт → новый костыль). Вместо этого добавить
      путь к реальному бинарю (например `/usr/bin/containerd`) в
      [`audit/trusted_procs.txt`](./trusted_procs.txt) (Слой 0.5). Скрипт сверяет
      **реальный бинарь владельца сокета** через `/proc/$pid/exe`, и все порты
      этого процесса перестают алертиться вне зависимости от номера.

   > **Почему exempt по процессу надёжнее exempt по порту:** номер порта
   > *spoofable* (любой процесс может выбрать тот же ephemeral-диапазон) и
   > *эфемерен* (меняется каждый рестарт), поэтому allowlist по порту либо
   > дырявый, либо вечно догоняющий. Путь к бинарю владельца (`/proc/$pid/exe`)
   > — стабильный идентификатор конкретного доверенного системного процесса,
   > который не меняется при рестарте и не подделывается простым выбором порта.

3. Если это агент задеплоил порт в обход Gatekeeper → зарегистрировать через
   `gk-register` (получить lease) ИЛИ остановить несанкционированный процесс.
   Это сигнал обхода PDP (дыры 1/3/7/9) — зафиксировать инцидент.

## Telegram-алерты (gk_notify.py)

Готовый нотификатор `audit/gk_notify.py` шлёт каждый ALERT в Telegram ЗавЛабу.
Подключён в `systemd/gatekeeper-audit.service` через `GK_NOTIFY=.../gk_notify.py`
(юнит уже выставляет переменную). Работает так:

- **Токен**: из `$GK_TG_BOT_TOKEN`, иначе из `/root/.openclaw/openclaw.json`
  (`channels.telegram.accounts.<GK_TG_ACCOUNT, по умолчанию raven>.botToken`).
  Юнит крутится от root, поэтому openclaw.json читается напрямую — отдельный
  секрет-файл не нужен.
- **Кому**: `$GK_TG_CHAT_ID`, по умолчанию `173681771` (ЗавЛаб).
- **Дедуп**: одинаковый текст алерта не шлётся чаще раза в `GK_NOTIFY_TTL`
  секунд (по умолчанию 3600). Защита от спама, если порт остаётся
  несанкционированным несколько прогонов подряд.
- **Безопасность**: нотификатор никогда не падает (exit 0) и не пишет токен
  в логи; при ошибке отправки — stderr + повтор в следующем прогоне.
- **Состояние дедупа**: `/var/lib/gatekeeper/notify-state.json` (создаётся
  автоматически, root-владелец).

Переопределить токен/chat вручную (без правки openclaw.json) можно через
`EnvironmentFile=-/etc/gatekeeper-alert.env` (уже прописан в юните; `-` =
игнорировать, если файла нет):

```bash
# /etc/gatekeeper-alert.env  (chmod 600, root:root)
GK_TG_BOT_TOKEN=...
GK_TG_CHAT_ID=173681771
```

Проверка доставки (один тестовый алерт):

```bash
/root/LabDoctorM/projects/mcp-tools/mcp-gatekeeper/audit/gk_notify.py \
  "GK-NOTIFY test: Telegram alert delivery OK (raven)"
```

## Настройка через env (для ручного запуска)

| Переменная | Значение по умолчанию | Назначение |
|------------|-----------------------|-----------|
| `GK_POLICY` | `policies/policy_v1.yaml` | путь к политике (ЕДИНЫЙ ИСТОЧНИК портов) |
| `GK_LEASES` | `data/leases.json` | путь к leases |
| `GK_AUDIT_LOG` | `/var/log/gk-audit.log` | файл инцидент-лога |
| `GK_NOTIFY` | `/root/.../audit/gk_notify.py` | нотификатор Telegram (выставлен юнитом) |
| `GK_TG_BOT_TOKEN` | *(из openclaw.json)* | токен Telegram-бота (переопределяет fallback) |
| `GK_TG_CHAT_ID` | `173681771` | кому слать алерты (ЗавЛаб) |
| `GK_TG_ACCOUNT` | `raven` | аккаунт в openclaw.json для токена |
| `GK_NOTIFY_TTL` | `3600` | секунды между повторами одинакового алерта |
| `STRICT_PRIVILEGED` | `0` | `1` — не разрешать авто-порты <1024 (всё в реестре) |

## Связь с Фазой 5 (RBAC / polkit)

- `gk-audit.sh` читает `policy_v1.yaml` и `leases.json`. По Фазе 5 эти файлы —
  `root:gatekeeper` `640`; агенты читают их (через группу) но не правят/удаляют
  (закрывает hole 8).
- polkit-правило (`polkit/10-gatekeeper.rules`) запрещает агентам
  `systemctl stop/disable/mask mcp-gatekeeper` (только root) — чтобы аудит-юнит
  и сам PDP нельзя было выключить (закрывает hole 2/8).
  Установка (вручную):
  ```bash
  cp polkit/10-gatekeeper.rules /etc/polkit-1/rules.d/10-gatekeeper.rules
  chown root:root /etc/polkit-1/rules.d/10-gatekeeper.rules
  chmod 644      /etc/polkit-1/rules.d/10-gatekeeper.rules
  systemctl restart polkit
  ```

## Проверка синтаксиса

```bash
bash -n audit/gk-audit.sh
bash -n set-rbac.sh
```
