#!/usr/bin/env bash
# =============================================================================
# gk-audit.sh — Фаза 6 ADR-0055: Audit live listening ports (Слой 2.5)
# -----------------------------------------------------------------------------
# Регулярно (по systemd timer) снимает `ss -tlnp`, сверяет каждый слушающий
# TCP-порт с источниками «разрешено»:
#   1) реестр разрешённых портов  docs/PORT_REGISTRY.md
#   2) reserve.blocked_ports      policies/policy_v1.yaml (инфра-резерв PDP)
#   3) собственный порт gatekeeper (listen_port из policy)
#   4) активные lease             data/leases.json (heartbeat не просрочен)
#
# Любой слушающий порт ВНЕ разрешённых -> АЛЕРТ:
#   - echo в stdout/stderr (видно в journal юнита)
#   - запись в /var/log/gk-audit.log (инцидент-лог)
#   - пометка [TELEGRAM] в лог-строке + (опционально) вызов $GK_NOTIFY
#
# НЕ блокирует, только видимость (audit = visibility). Всегда exit 0
# (чтобы.timer/юнит не падали и не уходили в fail-состояние из-за алертов).
#
# Требует root для `ss -p` (чтобы видеть PID/процесс). Запускается от root
# через gatekeeper-audit.service.
#
# Переопределения через env:
#   GK_REGISTRY / GK_POLICY / GK_LEASES / GK_AUDIT_LOG / GK_NOTIFY
#   STRICT_PRIVILEGED=1 — порты < block_privileged_below НЕ считать авто-разрешёнными
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PORT_REGISTRY="${GK_REGISTRY:-$REPO_ROOT/docs/PORT_REGISTRY.md}"
POLICY_FILE="${GK_POLICY:-$REPO_ROOT/policies/policy_v1.yaml}"
LEASES_FILE="${GK_LEASES:-$REPO_ROOT/data/leases.json}"
AUDIT_LOG="${GK_AUDIT_LOG:-/var/log/gk-audit.log}"
NOTIFY_CMD="${GK_NOTIFY:-}"   # опционально: путь к исполняемому нотификатору (Telegram/Myrmex)

# По умолчанию порты < block_privileged_below (обычно <1024) считаем системными
# и разрешёнными, чтобы не генерить фальш-алерты на ssh/dns и т.п.
# STRICT_PRIVILEGED=1 отключает это (всё должно быть явно в реестре).
STRICT_PRIVILEGED="${STRICT_PRIVILEGED:-0}"

NOW_EPOCH="$(date +%s)"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

ALERTS=0
SEEN=0

log_alert() {
  local port="$1"; shift
  local detail="$*"
  local line="$NOW_ISO ALERT port=$port $detail"
  echo "GK-AUDIT [ALERT] $line"
  # [TELEGRAM] — пометка для внешнего коллектора (grep /var/log/gk-audit.log)
  echo "$line [TELEGRAM]" >> "$AUDIT_LOG" 2>/dev/null || true
  if [[ -n "$NOTIFY_CMD" && -x "$NOTIFY_CMD" ]]; then
    "$NOTIFY_CMD" "GK-AUDIT ALERT port=$port $detail" 2>/dev/null || true
  fi
}

# --- собрать разрешённые порты в ассоциативный массив ---
declare -A ALLOWED=()

# 1) реестр PORT_REGISTRY.md (строки таблицы | 5432 | ... и строки PORT 5432)
if [[ -f "$PORT_REGISTRY" ]]; then
  while IFS= read -r raw; do
    p=""
    if [[ "$raw" =~ ^[[:space:]]*PORT[[:space:]]+([0-9]+) ]]; then
      p="${BASH_REMATCH[1]}"
    elif [[ "$raw" =~ \|[[:space:]]*([0-9]+)[[:space:]]*\| ]]; then
      p="${BASH_REMATCH[1]}"
    fi
    [[ -n "$p" ]] && ALLOWED[$p]=1
  done < "$PORT_REGISTRY"
fi

# 2)+3) policy: listen_port + reserve.blocked_ports + block_privileged_below
bpb=1024
if [[ -f "$POLICY_FILE" ]]; then
  lp=$(grep -E '^[[:space:]]*listen_port:' "$POLICY_FILE" | grep -oE '[0-9]+' | head -1)
  [[ -n "$lp" ]] && ALLOWED[$lp]=1

  bp_line=$(grep -E '^[[:space:]]*blocked_ports:' "$POLICY_FILE" | head -1)
  if [[ -n "$bp_line" ]]; then
    for p in $(echo "$bp_line" | grep -oE '[0-9]+'); do
      ALLOWED[$p]=1
    done
  fi

  bpb_val=$(grep -E '^[[:space:]]*block_privileged_below:' "$POLICY_FILE" | grep -oE '[0-9]+' | head -1)
  [[ -n "$bpb_val" ]] && bpb="$bpb_val"
fi

# 4) активные lease: порт задан И heartbeat+lease_timeout > now
if [[ -f "$LEASES_FILE" ]]; then
  while IFS= read -r p; do
    [[ -n "$p" ]] && ALLOWED[$p]=1
  done < <(jq -r --arg now "$NOW_EPOCH" '
    .leases[]?
    | select(.port != null)
    | select((.last_heartbeat // 0) + (.lease_timeout // 300) > ($now | tonumber))
    | "\(.port)"' "$LEASES_FILE" 2>/dev/null)
fi

# --- сканировать слушающие TCP-порты (ss -tlnp; хедер дропаем tail -n +2) ---
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  local_field="$(echo "$line" | awk '{print $4}')"
  port="$(echo "$local_field" | awk -F: '{print $NF}')"
  [[ "$port" =~ ^[0-9]+$ ]] || continue
  SEEN=$((SEEN + 1))

  allowed=0
  if [[ -n "${ALLOWED[$port]+x}" ]]; then
    allowed=1
  elif [[ "$STRICT_PRIVILEGED" != "1" && "$port" -lt "$bpb" ]]; then
    # привилегированный системный порт — разрешён по умолчанию
    allowed=1
  fi

  if [[ "$allowed" -eq 0 ]]; then
    ALERTS=$((ALERTS + 1))
    log_alert "$port" "unauthorized listening socket: $line"
  fi
done < <(ss -tlnp 2>/dev/null | tail -n +2)

# --- итог (всегда exit 0 — audit только видимость) ---
if [[ "$ALERTS" -gt 0 ]]; then
  echo "GK-AUDIT summary: checked=$SEEN alerts=$ALERTS (see $AUDIT_LOG)"
else
  echo "GK-AUDIT summary: checked=$SEEN alerts=0 OK"
fi
exit 0
