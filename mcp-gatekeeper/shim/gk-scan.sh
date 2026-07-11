#!/usr/bin/env bash
# gatekeeper shim: сканер новых юнитов (Слой 2, backstop / аудит).
# Вызывается systemd path-unit при изменении /etc/systemd/system/.
#
# ВАЖНО: НЕ регистрирует порты в gatekeeper. Причина: если Слой 2 создаёт lease
# от имени агента 'shim', то последующий `systemctl enable` (Слой 1, тот же агент
# 'shim') получает REJECT «уже занят shim» — шим блокирует сам себя, и ни один
# сервис с портом не включается. Поэтому Слой 2 — ТОЛЬКО наблюдение: логирует
# найденные порты в аудит-файл. Блокировка конфликтов — задача Слоя 1 (wrapper
# /usr/local/bin/systemctl), который перехватывает enable/start и звонит gatekeeper.
#
# Таймеры (.timer) не слушают порты — пропускаем (их OnCalendar=.. 03:00 раньше
# ловилось grep как «порт 0» и засоряло лог). Порт парсим строго из
# LISTEN=/0.0.0.0:/127.0.0.1:/[::]: чтобы не путать с временем (03:00).
set -u
AUDIT=/var/log/gk-shim-scan.log
for u in /etc/systemd/system/*.service; do
  [[ -f "$u" ]] || continue
  base=$(basename "$u")
  case "$base" in
    gatekeeper-shim.*|mcp-gatekeeper.*) continue ;;
  esac
  cand=$(grep -hoE 'LISTEN=:[0-9]{2,5}|0\.0\.0\.0:[0-9]{2,5}|127\.0\.0\.1:[0-9]{2,5}|\[::\]:[0-9]{2,5}' "$u" | head -1)
  PORT=$(echo "$cand" | grep -oE '[0-9]{2,5}$')
  if [[ -n "$PORT" ]] && [[ "$PORT" -gt 0 ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SCAN $base port=$PORT" >> "$AUDIT"
  fi
done
