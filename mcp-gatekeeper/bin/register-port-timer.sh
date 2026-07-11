#!/usr/bin/env bash
# ============================================================================
# register-port-timer.sh — скрипт-зародыш (scaffold) для агентов.
# ----------------------------------------------------------------------------
# Тонкая обёртка поверх MCP-сервера привратника (mcp-gatekeeper). Агент НЕ
# биндит порт/таймер напрямую — он регистрирует ресурс через привратник.
#
# Режимы работы:
#   1) CLI (по умолчанию) — вызывает bin/mcp-gatekeeper-server.py --cli ...
#      (не требует запущенного MCP-сервера; удобно для скриптов/CI).
#   2) MCP (--via-mcp)     — вызывает тот же инструмент через mcporter
#      (требует запущенный юнит mcp-gatekeeper и настроенный mcporter).
#
# Примеры:
#   ./register-port-timer.sh port   --agent raven --project X --port 8081 --what-for "api gateway"
#   ./register-port-timer.sh timer  --agent raven --project X --action "backup.sh" --schedule "0 3 * * *" --what-for "nightly backup"
#   ./register-port-timer.sh service --agent raven --project X --port 8081 --action "worker.sh" --schedule "*/5 * * * *" --what-for "poll loop"
#   ./register-port-timer.sh release --request-id rk-abc123
#   ./register-port-timer.sh heartbeat --request-id rk-abc123
# ============================================================================

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$HERE/mcp-gatekeeper-server.py"
MCP_SERVER_NAME="${MCP_GATEKEEPER_NAME:-mcp-gatekeeper}"

die() { echo "register-port-timer: $*" >&2; exit 1; }

[ -x "$SERVER" ] || die "сервер не найден: $SERVER"

MODE="cli"
case "${1:-}" in
  --via-mcp) MODE="mcp"; shift ;;
esac

KIND="${1:-}"; [ -n "$KIND" ] || die "укажите kind: port|timer|service|release|heartbeat|transfer|list"
shift

# разбор общих аргументов в массив (сохраняет пробелы в значениях)
# KIND (короткий) -> SUBCMD (подкоманда сервера: register-port/...)
case "$KIND" in
  port) SUBCMD=register-port ;;
  timer) SUBCMD=register-timer ;;
  service) SUBCMD=register-service ;;
  release) SUBCMD=release ;;
  heartbeat) SUBCMD=heartbeat ;;
  transfer) SUBCMD=transfer ;;
  list) SUBCMD=list ;;
  *) die "неизвестный kind: $KIND (port|timer|service|release|heartbeat|transfer|list)" ;;
esac
cli_args=("$SUBCMD")
AGENT=""; PROJECT=""; WHAT_FOR=""; PORT=""; ACTION=""; SCHEDULE=""
REQUEST_ID=""; TO_AGENT=""; BY_AGENT=""; RUN_AS=""; AS_ROOT=""; BYPASS_REASON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; cli_args+=(--agent "$2"); shift 2 ;;
    --project) PROJECT="$2"; cli_args+=(--project "$2"); shift 2 ;;
    --what-for) WHAT_FOR="$2"; cli_args+=(--what-for "$2"); shift 2 ;;
    --port) PORT="$2"; cli_args+=(--port "$2"); shift 2 ;;
    --action) ACTION="$2"; cli_args+=(--action "$2"); shift 2 ;;
    --schedule) SCHEDULE="$2"; cli_args+=(--schedule "$2"); shift 2 ;;
    --request-id) REQUEST_ID="$2"; cli_args+=(--request-id "$2"); shift 2 ;;
    --to-agent) TO_AGENT="$2"; cli_args+=(--to-agent "$2"); shift 2 ;;
    --by-agent) BY_AGENT="$2"; cli_args+=(--by-agent "$2"); shift 2 ;;
    --run-as) RUN_AS="$2"; cli_args+=(--run-as "$2"); shift 2 ;;
    --as-root) AS_ROOT="1"; cli_args+=(--as-root); shift ;;
    --bypass-reason) BYPASS_REASON="$2"; cli_args+=(--bypass-reason "$2"); shift 2 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done

# валидация обязательных полей по kind (понятные ошибки)
case "$KIND" in
  port)
    [ -n "$AGENT" ] && [ -n "$PROJECT" ] && [ -n "$PORT" ] && [ -n "$WHAT_FOR" ] || \
      die "port требует --agent --project --port --what-for" ;;
  timer)
    [ -n "$AGENT" ] && [ -n "$PROJECT" ] && [ -n "$ACTION" ] && [ -n "$SCHEDULE" ] && [ -n "$WHAT_FOR" ] || \
      die "timer требует --agent --project --action --schedule --what-for" ;;
  service)
    [ -n "$AGENT" ] && [ -n "$PROJECT" ] && [ -n "$PORT" ] && [ -n "$ACTION" ] && [ -n "$SCHEDULE" ] && [ -n "$WHAT_FOR" ] || \
      die "service требует --agent --project --port --action --schedule --what-for" ;;
  release|heartbeat)
    [ -n "$REQUEST_ID" ] || die "$KIND требует --request-id" ;;
  transfer)
    [ -n "$REQUEST_ID" ] && [ -n "$TO_AGENT" ] && [ -n "$PROJECT" ] || \
      die "transfer требует --request-id --to-agent --project" ;;
  list) : ;;
  *) die "неизвестный kind: $KIND (port|timer|service|release|heartbeat|transfer|list)" ;;
esac

if [ "$MODE" = "mcp" ]; then
  # через mcporter: server <tool> key=value ...
  tool=$(echo "$SUBCMD" | tr '-' '_')
  mcpargs=()
  i=1
  n=${#cli_args[@]}
  while [ "$i" -lt "$n" ]; do
    key="${cli_args[$i]}"; val="${cli_args[$((i+1))]}"
    key="${key#--}"; key="${key//-/_}"
    mcpargs+=("$key=$val")
    i=$((i+2))
  done
fi

# Куда писать журнал событий (data/port-timer-log.jsonl)
REPO_DIR="$(cd "$HERE/.." && pwd)"
DATA_DIR="${GATEKEEPER_DATA:-$REPO_DIR/data}"

# Событийный детект зависания: таймаут 5с на вызов register_*. Сервер НЕ шлёт
# periodic heartbeat — «жив ли он» доказывается ответом на реальный запрос.
# При таймауте — событие GATEKEEPER_TIMEOUT в журнале + эскалация (БЕЗ auto-restart).
is_register=0
case "$KIND" in
  port|timer|service) is_register=1 ;;
esac

if [ "$is_register" -eq 1 ]; then
  set +e
  if [ "$MODE" = "mcp" ]; then
    timeout 5 mcporter call --stdio "python3 $SERVER" "$tool" "${mcpargs[@]}"
    rc=$?
  else
    timeout 5 python3 "$SERVER" "${cli_args[@]}"
    rc=$?
  fi
  set -e
  if [ "$rc" -eq 124 ]; then
    rid="GK-TIMEOUT-$(date +%s)-$$"
    when="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$DATA_DIR"
    # Атомарная дозапись JSONL-события (поля: request_id/when/agent/project/why)
    python3 - "$DATA_DIR" "$rid" "$when" "$WHAT_FOR" "$AGENT" "$PROJECT" "$KIND" <<'PYEOF'
import json, sys, os
data_dir, rid, when, what_for, agent, project, kind = sys.argv[1:8]
ev = {
    "request_id": rid,
    "when": when,
    "what_for": what_for,
    "why": "GATEKEEPER_TIMEOUT",
    "agent": agent,
    "project": project,
    "action": "register_" + kind,
}
with open(os.path.join(data_dir, "port-timer-log.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
PYEOF
    cat >&2 <<EOF

[ESCALATION] mcp-gatekeeper НЕ ответил в течение 5с на вызов '$KIND'
  (agent=$AGENT, project=$PROJECT, what_for='$WHAT_FOR').

Событие GATEKEEPER_TIMEOUT записано в: $DATA_DIR/port-timer-log.jsonl
Сервер НЕ перезапущен автоматически — это изменение состояния, требует ведома.

Что сделать оператору (по согласованию):
  - проверить статус:   systemctl status mcp-gatekeeper
  - посмотреть логи:    journalctl -u mcp-gatekeeper -n 100 --no-pager
  - при необходимости:  systemctl restart mcp-gatekeeper   # только с ведома
EOF
    exit 1
  fi
  exit "$rc"
else
  if [ "$MODE" = "mcp" ]; then
    exec mcporter call --stdio "python3 $SERVER" "$tool" "${mcpargs[@]}"
  else
    # CLI напрямую
    exec python3 "$SERVER" "${cli_args[@]}"
  fi
fi
