---
description: "Схема hb-status.json — канонический файл чек-листа heartbeat"
type: doc
last_reviewed: 2026-07-12
status: active
---

# hb-status.json — канонический файл чек-листа heartbeat

## Идея
Heartbeat агента становится **тупым триггером**: «дёрни свой эндпоинт».
Вся тяжёлая сборка (systemctl, диск, reindex, порты, инциденты) — на **cron**,
который раз в сутки пишет этот файл. `heartbeat-server` (MCP, порт 8088)
только **читает** его и отдаёт агенту как ДАННЫЕ (не команды).

## Путь
`/root/LabDoctorM/workspaces/<agent>/hb-status.json`

Писать может **только cron владельца агента**. `heartbeat-server` — read-only.

## Схема
```json
{
  "agent": "dominika",
  "updated_at": "2026-07-12T00:00:00Z",   // ISO-8601 UTC, обязательно
  "priority": "Починить lab_search embed-таймаут (P0)",  // опц. фокус дня (DATA)
  "checks": [
    {
      "name": "grimoire.md жив",
      "result": "pass",                    // pass | fail | unknown
      "note": ">=1 строка '- '"            // опц. краткое пояснение
    },
    {
      "name": "search-stack alive",
      "result": "fail",
      "note": "lab_search embed timeout при health=ok"
    }
  ]
}
```

## Правила
- `updated_at` обязателен. Сервер считает файл **устаревшим**, если старше ~26ч
  (частота heartbeat — раз в сутки).
- `checks[].result` строго `pass|fail|unknown`.
- Первый пункт `checks` у КАЖДОГО агента — **grimoire.md жив** (требование ЗавЛаба).
- `priority` — это ДАННЫЕ (фокус дня), НЕ исполняемая команда. Агент читает как
  подсказку, решение остаётся за агентом. (Защита от MCP-инъекции.)
- Файл — плоский, маленький (<256KB). Никаких секретов/ключей.

## Как это читает агент
Heartbeat-директива в `HEARTBEAT.md` схлопывается до:
> «Дёрни `heartbeat://<agent>` (или tool `pull`), выдай `summary_text`.
>  Если `overall == alert` — доложи ЗавЛабу список 🔴, иначе HEARTBEAT_OK.»

## Overall (вычисляет сервер)
- `ok` — все checks pass
- `alert` — есть хоть один fail
- `partial` — есть unknown, нет fail
- `no_checks` — checks пуст
- `no_data` — hb-status.json отсутствует (cron ещё не писал)

## Аудит-журнал (audit.log)

Сервер ведёт подробный append-only журнал каждого вызова.

- **Путь:** `/root/LabDoctorM/.ops/mcp-heartbeat/audit.log` (env `HB_AUDIT_DIR`)
- **Дубль:** каждая строка также идёт в journald с префиксом `AUDIT`
  (`journalctl -u mcp-heartbeat.service | grep AUDIT`)
- **Формат:** одна JSON-строка на вызов
- **Ротация:** RotatingFileHandler, 10MB × 5 (~50MB cap)

### Поля
- `ts` — ISO-8601 UTC, момент вызова
- `tool` — `pull` | `resource` | `colony` | `list_agents`
- `agent` — какой агент запрошен (для pull/resource)
- `uri` — `heartbeat://<agent>` (для resource)
- `overall`, `fail`, `checklist_pending`, `grimoire_available` — выданный вердикт
- `ok`, `error` — успех/ошибка запроса (для pull)
- `totals`, `alerts` — сводка (для colony)
- `count` — число агентов (для list_agents)

### Примеры
```json
{"ts":"...","tool":"pull","agent":"raven","ok":true,"overall":"alert","fail":2,"checklist_pending":false,"grimoire_available":true,"error":null}
{"ts":"...","tool":"colony","totals":{"ok":0,"alert":8,...},"alerts":["raven",...]}
```

### Зачем (proof heartbeat)
Отвечает на «кто и когда дёрнул»: `grep '"agent": "raven"' audit.log` показывает,
ходил ли Ворон на свой эндпоинт и когда. Это observability (лог), НЕ запись в
состояние агентов — read-only-инвариант сервера сохранён.

> ⚠️ **[НЕАКТУАЛЬНО]** Устарело: концепция «Собор сердца» (heartbeat→grimoire) и grimoire-файлы перенесены/сломаны (2026-07-13). Не использовать как актуальное состояние системы.
