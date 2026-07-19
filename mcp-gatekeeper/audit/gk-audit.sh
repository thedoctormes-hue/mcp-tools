#!/usr/bin/env bash
# =============================================================================
# gk-audit.sh — Фаза 6 ADR-0055: Audit live listening ports (Слой 2.5)
# -----------------------------------------------------------------------------
# Регулярно (по systemd timer) снимает `ss -tlnp`, сверяет каждый слушающий
# TCP-порт с источниками «разрешено» (ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ — policies/policy_v1.yaml,
# Уровень Е ADR-0056):
#   1) reserve.blocked_ports      policies/policy_v1.yaml (инфра-резерв PDP, ожидаемые инфра-порты)
#   2) собственный порт gatekeeper (listen_port из policy)
#   3) порты < block_privileged_below — системные, разрешены по умолчанию
#   4) активные lease             data/leases.json (heartbeat не просрочен)
#
# docs/PORT_REGISTRY.md — УСТАРЕЛО как ручной allowlist. Теперь это read-only вид,
# генерируемый scripts/gen-port-registry.sh ИЗ policy. Правь policy_v1.yaml, не md.
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
#   GK_POLICY / GK_LEASES / GK_AUDIT_LOG / GK_NOTIFY
#   STRICT_PRIVILEGED=1 — порты < block_privileged_below НЕ считать авто-разрешёнными
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
  local port="$1"; local human="$2"; local raw="${3:-}"
  local line="$NOW_ISO ALERT $human"
  echo "GK-AUDIT [ALERT] $line"
  # [TELEGRAM] — пометка: строка уходит/пойдёт в Telegram-нотификатор (grep по логу)
  if [[ -n "$raw" ]]; then
    echo "$line | raw: $raw [TELEGRAM]" >> "$AUDIT_LOG" 2>/dev/null || true
  else
    echo "$line [TELEGRAM]" >> "$AUDIT_LOG" 2>/dev/null || true
  fi
  if [[ -n "$NOTIFY_CMD" && -x "$NOTIFY_CMD" ]]; then
    "$NOTIFY_CMD" "GK-AUDIT ALERT $human" 2>/dev/null || true
  fi
}

# --- собрать разрешённые порты в ассоциативный массив ---
declare -A ALLOWED=()

# 1) policy: listen_port + reserve.blocked_ports + block_privileged_below
#    (docs/PORT_REGISTRY.md более НЕ источник — см. Уровень Е ADR-0056)
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

# --- Слой 0 (ADR-0058 band-aid): известные инфра-порты ---
# Порты из allowed_ports.txt НЕ алертятся (легит, но не регистрируются в GK:
# Docker/direct биндят в обход shim, либо lease-стор пуст из-за короткого
# lease_timeout). Правильное решение — наполнить lease-стор (persistence +
# регистрация), после чего этот список сокращается. Чужой/новый порт вне
# списка и вне leases — всё равно алертится.
ALLOW_LIST="${GK_ALLOW_PORTS:-$SCRIPT_DIR/allowed_ports.txt}"
if [[ -f "$ALLOW_LIST" ]]; then
  while IFS= read -r _p; do
    _p="${_p%%#*}"            # отсечь inline-комментарий
    _p="$(echo "$_p" | tr -d '[:space:]')"
    [[ -z "$_p" ]] && continue
    if [[ "$_p" =~ ^[0-9]+$ ]]; then
      ALLOWED[$_p]=1
    fi
  done < "$ALLOW_LIST"
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
    # разбор ss-строки в человекочитаемый вид
    # addrport bind_addr proc_name proc_pid human_detail
    addrport="$(echo "$line" | awk '{print $4}')"
    bind_addr="$(echo "$addrport" | awk -F: '{print $1}')"
    proc_name="$(echo "$line" | grep -oE 'users:\(\("[^"]+"' | head -1 | sed -E 's/^users:\(\("//; s/"$//')"
    proc_pid="$(echo "$line" | grep -oE 'pid=[0-9]+' | head -1 | sed -E 's/pid=//')"
    if [[ -n "$proc_name" ]]; then
      human_detail="порт $port на $bind_addr: слушает $proc_name"
      [[ -n "$proc_pid" ]] && human_detail="$human_detail [pid $proc_pid]"
    else
      human_detail="порт $port на $bind_addr: неизвестный процесс"
    fi
    human_detail="$human_detail — НЕ зарегистрирован в gatekeeper (unauthorized listening socket)"
    log_alert "$port" "$human_detail" "$line"
  fi
done < <(ss -tlnp 2>/dev/null | tail -n +2)

# --- итог (всегда exit 0 — audit только видимость) ---
if [[ "$ALERTS" -gt 0 ]]; then
  echo "GK-AUDIT summary: checked=$SEEN alerts=$ALERTS (see $AUDIT_LOG)"
else
  echo "GK-AUDIT summary: checked=$SEEN alerts=0 OK"
fi
exit 0
