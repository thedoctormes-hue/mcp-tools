#!/usr/bin/env bash
# gatekeeper shim: сканер новых юнитов (Слой 2, backstop).
# Вызывается systemd path-unit при изменении /etc/systemd/system/.
# Регистрирует через gatekeeper все .service/.timer с портами (кроме own/gatekeeper).
set -u
HELPER=/usr/local/bin/gk-register
for u in /etc/systemd/system/*.service /etc/systemd/system/*.timer; do
  [[ -f "$u" ]] || continue
  base=$(basename "$u")
  case "$base" in
    gatekeeper-shim.*|mcp-gatekeeper.*) continue ;;
  esac
  PORT=$(grep -oE ':[0-9]{2,5}' "$u" | head -1 | tr -d ':')
  if [[ -n "$PORT" ]]; then
    timeout 5 "$HELPER" port "$PORT" "scan $base" "shim" "shim" >/dev/null 2>&1 || true
  fi
done
