#!/usr/bin/env bash
# alert-gk-bypass.sh — детект BYPASS=root событий шима gatekeeper в journald.
#
# Шим (FIX 3, ADR-0055) при GK_AS_ROOT=1 пишет в journal строку с тегом
# 'gatekeeper-shim' и маркером 'BYPASS=root', включая identity агента
# (agent=, project=, unit=, action=). Этот скрипт извлекает такие события
# из journald и выводит их.
#
# НЕ шлёт алерты самостоятельно: scheduling + доставка alert — зона другого
# агента (этот скрипт НЕ создаёт таймеров/cron). Возвращает:
#   0 — событий BYPASS=root за период нет
#   1 — найдены события BYPASS=root (сигнал для внешнего оркестратора/alert)
#
# Опционально: ALERT_HOOK=<cmd> — если задано, вывод передаётся этой команде
# (например webhook/notification), но сам скрипт НЕ планирует вызовов.
set -u

SINCE="${1:--1h}"
TAG="gatekeeper-shim"
MATCH="BYPASS=root"

OUT="$(journalctl -t "$TAG" --since "$SINCE" 2>/dev/null | grep -F "$MATCH")"

if [[ -n "$OUT" ]]; then
  echo "=== gatekeeper BYPASS=root events (tag=$TAG, since=$SINCE) ==="
  echo "$OUT"
  if [[ -n "${ALERT_HOOK:-}" ]]; then
    printf '%s\n' "$OUT" | "${ALERT_HOOK}" 2>/dev/null || true
  fi
  exit 1
else
  echo "no BYPASS=root events in journal (tag=$TAG, since=$SINCE)"
  exit 0
fi
