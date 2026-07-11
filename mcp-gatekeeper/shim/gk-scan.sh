#!/usr/bin/env bash
# gatekeeper shim: сканер новых юнитов (Слой 2, backstop).
# Вызывается systemd path-unit при изменении /etc/systemd/system/.
# Регистрирует через gatekeeper все .service с портами (кроме own/gatekeeper).
#
# ВАЖНО: таймеры (.timer) не слушают порты — пропускаем (иначе OnCalendar=.. 03:00
# ловится grep как «порт 0» и засоряет лог REJECT). Порты парсим строго из
# LISTEN=/0.0.0.0:/127.0.0.1:/[::]: чтобы не путать с временем (03:00).
set -u
HELPER=/usr/local/bin/gk-register
for u in /etc/systemd/system/*.service; do
  [[ -f "$u" ]] || continue
  base=$(basename "$u")
  case "$base" in
    gatekeeper-shim.*|mcp-gatekeeper.*) continue ;;
  esac
  cand=$(grep -hoE 'LISTEN=:[0-9]{2,5}|0\.0\.0\.0:[0-9]{2,5}|127\.0\.0\.1:[0-9]{2,5}|\[::\]:[0-9]{2,5}' "$u" | head -1)
  PORT=$(echo "$cand" | grep -oE '[0-9]{2,5}$')
  if [[ -n "$PORT" ]] && [[ "$PORT" -gt 0 ]]; then
    timeout 5 "$HELPER" port "$PORT" "scan $base" "shim" "shim" >/dev/null 2>&1 || true
  fi
done
