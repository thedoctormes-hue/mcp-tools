#!/usr/bin/env bash
# =============================================================================
# gen-port-registry.sh — Уровень Е ADR-0056: генератор docs/PORT_REGISTRY.md
# -----------------------------------------------------------------------------
# ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ — policies/policy_v1.yaml (reserve.blocked_ports +
# listen_port + block_privileged_below). Этот скрипт лишь рендерит ЧИТАЕМЫЙ
# read-only вид из policy, чтобы не править md руками (источник дрейфа).
#
#   bash scripts/gen-port-registry.sh [--policy PATH] [--out PATH]
#
# Всегда перезаписывает out (по умолчанию docs/PORT_REGISTRY.md). Idempotent.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

POLICY_FILE="${GK_POLICY:-${1:-$REPO_ROOT/policies/policy_v1.yaml}}"
OUT_FILE="${2:-$REPO_ROOT/docs/PORT_REGISTRY.md}"

if [[ ! -f "$POLICY_FILE" ]]; then
  echo "gen-port-registry: нет policy: $POLICY_FILE" >&2
  exit 1
fi

lp=$(grep -E '^[[:space:]]*listen_port:' "$POLICY_FILE" | grep -oE '[0-9]+' | head -1)
bpb=$(grep -E '^[[:space:]]*block_privileged_below:' "$POLICY_FILE" | grep -oE '[0-9]+' | head -1)
bpb="${bpb:-1024}"
bp_line=$(grep -E '^[[:space:]]*blocked_ports:' "$POLICY_FILE" | head -1)

# собрать зарезервированные порты (из policy)
declare -A PORTS=()
while IFS= read -r p; do
  [[ -n "$p" ]] && PORTS[$p]=1
done < <(echo "$bp_line" | grep -oE '[0-9]+')

# собственный порт gatekeeper тоже ожидаем
[[ -n "$lp" ]] && PORTS[$lp]=1

# человекочитаемые подписи (для удобства; не влияют на логику аудита)
declare -A LABEL=(
  [22]="ssh (system)"
  [53]="dns/systemd-resolved (system)"
  [111]="rpcbind (system)"
  [123]="ntp (system)"
  [389]="ldap (infra)"
  [443]="https (infra)"
  [3000]="grafana (infra)"
  [5432]="PostgreSQL (infra)"
  [6379]="Redis (infra)"
  [8001]="reserved (infra)"
  [8080]="agent API range 8080-8099 (infra-reserved)"
  [8082]="onnx-embedder (infra)"
  [8084]="reserved (infra)"
  [8085]="reserved (infra)"
  [8086]="mcp-apikeys (infra)"
  [8087]="mcp-memory (infra)"
  [8090]="reserved (infra)"
  [8099]="reserved (infra)"
  [8200]="reserved (infra)"
  [8202]="reserved (infra)"
  [8300]="reserved (infra)"
  [8443]="reserved (infra)"
  [8444]="reserved (infra)"
  [8445]="reserved (infra)"
  [8888]="mcp-gatekeeper (listen_port)"
  [8889]="reserved (infra)"
  [8899]="reserved (infra)"
  [9090]="Prometheus (infra)"
  [9100]="node_exporter (infra)"
  [9187]="postgres_exporter (infra)"
  [9443]="reserved (infra)"
  [10443]="reserved (infra)"
  [18789]="reserved (infra)"
  [36401]="reserved (infra)"
)

# отсортированный список портов
mapfile -t SORTED < <(printf '%s\n' "${!PORTS[@]}" | sort -n)

{
  echo "# PORT_REGISTRY.md — AUTO-GENERATED (Уровень Е ADR-0056)"
  echo
  echo "> **НЕ РЕДАКТИРУЙ ВРУЧНУЮ.** Этот файл генерируется"
  echo "> \`scripts/gen-port-registry.sh\` из \`policies/policy_v1.yaml\`."
  echo "> Единственный источник правды — policy (reserve.blocked_ports + listen_port"
  echo "> + block_privileged_below). Правки вноси в policy, затем перегенерируй."
  echo
  echo "## Роль"
  echo
  echo "Read-only вид зарезервированных/ожидаемых портов для аудита"
  echo "(\`audit/gk-audit.sh\`). Любой слушающий порт, которого НЕТ ни здесь,"
  echo "ни в активных lease (gatekeeper), ни среди системных (< \`block_privileged_below\`"
  echo "= $bpb) — считается несанкционированным и генерит АЛЕРТ."
  echo
  echo "Агентские порты (8080–8099 и т.п.) разрешены ТОЛЬКО при наличии"
  echo "активного lease в gatekeeper — они НЕ пре-разрешены здесь."
  echo
  echo "## Реестр (из policy)"
  echo
  echo "| Port | Service | Notes |"
  echo "|------|---------|-------|"
  for p in "${SORTED[@]}"; do
    lbl="${LABEL[$p]:-reserved/infra}"
    note=""
    if [[ "$p" == "$lp" ]]; then note="собственный порт PDP (listen_port)"; fi
    echo "| $p | $lbl | $note |"
  done
  echo
  echo "## Как обновить"
  echo
  echo "1. Правь \`reserve.blocked_ports\` / \`listen_port\` / \`block_privileged_below\`"
  echo "   в \`policies/policy_v1.yaml\`."
  echo "2. Перегенерируй: \`bash scripts/gen-port-registry.sh\`."
  echo "3. Закоммить оба файла через \`lab-commit.sh\`."
} > "$OUT_FILE"

echo "gen-port-registry: записал $OUT_FILE ($((${#SORTED[@]})) портов)"
