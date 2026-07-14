# audit/ — Gatekeeper Audit live ports (Фаза 6, Слой 2.5, ADR-0055)

Скрипт `gk-audit.sh` периодически снимает `ss -tlnp` и сверяет **каждый**
слушающий TCP-порт хоста с источниками «разрешено». Любой порт вне разрешённых
→ **АЛЕРТ** (вывод + лог + пометка для Telegram). Ничего не блокирует — только
видимость (audit = visibility). Закрывает дыры: **1** (полный пропуск),
**3** (ручной процесс), **7** (игнор REJECT/DEAD), **9** (race/TTL).

## Что проверяет

Порт считается **разрешённым**, если он есть в одном из:

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
- **`[TELEGRAM]`** — пометка: внешний коллектор может `grep '[TELEGRAM]' /var/log/gk-audit.log`
  и переслать в Telegram/Myrmex.
- Опционально: если задать `GK_NOTIFY=/путь/к/нотификатору` (исполняемый файл,
  принимает текст алерта аргументом), скрипт сам вызовет его.

**Что делать при алерте:**
1. `ss -tlnp | grep :N` — подтвердить и найти процесс/PID.
2. Если это легитимный новый инфра-сервис → добавить порт в
   `policies/policy_v1.yaml` → `reserve.blocked_ports`, перегенерировать
   `docs/PORT_REGISTRY.md` (`bash scripts/gen-port-registry.sh`) и закоммитить.
   Алерт перестанет срабатывать.
3. Если это агент задеплоил порт в обход Gatekeeper → зарегистрировать через
   `gk-register` (получить lease) ИЛИ остановить несанкционированный процесс.
   Это сигнал обхода PDP (дыры 1/3/7/9) — зафиксировать инцидент.

## Настройка через env (для ручного запуска)

| Переменная | Значение по умолчанию | Назначение |
|------------|-----------------------|-----------|
| `GK_POLICY` | `policies/policy_v1.yaml` | путь к политике (ЕДИНЫЙ ИСТОЧНИК портов) |
| `GK_LEASES` | `data/leases.json` | путь к leases |
| `GK_AUDIT_LOG` | `/var/log/gk-audit.log` | файл инцидент-лога |
| `GK_NOTIFY` | *(пусто)* | путь к нотификатору (опц.) |
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
