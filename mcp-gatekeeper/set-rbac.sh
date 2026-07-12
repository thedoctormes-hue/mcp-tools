#!/usr/bin/env bash
# =============================================================================
# set-rbac.sh — Фаза 5 ADR-0055: RBAC на конфигурацию mcp-gatekeeper
# -----------------------------------------------------------------------------
# Выставляет владение/права на критичные файлы конфигурации и обёртку systemctl:
#   - policies/policy_v1.yaml      -> root:gatekeeper  640
#   - data/leases.json             -> root:gatekeeper  640
#   - shim/systemctl-wrapper       -> root:gatekeeper  640 (исходник-шаблон)
#   - /usr/bin/systemctl (shim)    -> root:gatekeeper  755 (исполняемый; см. DEVIATION)
#
# ЗАПУСК ТОЛЬКО С ПОДТВЕРЖДЕНИЯ КООРДИНАТОРА.
# По умолчанию НИЧЕГО НЕ МЕНЯЕТ — это --dry-run.
#   --apply        реально применить chown/chmod/groupadd
#   --with-polkit  ДОПОЛНИТЕЛЬНО установить polkit-правило (иначе не трогаем)
#   --dry-run/-n   только показать план (по умолчанию)
#
# DEVIATION (отклонение от «640 для обёртки» в ADR):
#   Развёрнутый wrapper /usr/bin/systemctl ДОЛЖЕН быть исполняемым для агентов
#   (иначе `systemctl` не запустится ни у кого — ломается весь PATH-shim Слоя 1).
#   640 не даёт бита x -> сломает вызов systemctl агентами. Поэтому развёрнутый
#   wrapper = 755 root:gatekeeper:
#     - owner root  : только root может писать/удалять (закрывает hole 2/8)
#     - group/other : только чтение + исполнение, БЕЗ записи (агенты запускают,
#                     но не меняют и не удаляют обёртку).
#   Исходник shim/systemctl-wrapper остаётся 640 (он не исполняется напрямую).
#   Если нужен ровно 640 на развёрнутом wrapper — WRAPPER_MODE=640 (НЕ рекомендуется,
#   сломает вызов systemctl агентами; нужно будет добавить агентов в group gatekeeper
#   и дать им x через группу => тогда 750, а не 640).
# =============================================================================
set -euo pipefail

DRY_RUN=1
WITH_POLKIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      DRY_RUN=0 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    --with-polkit) WITH_POLKIT=1 ;;
    -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_MODE="${WRAPPER_MODE:-755}"
DEPLOYED_WRAPPER="${DEPLOYED_WRAPPER:-/usr/bin/systemctl}"
SHIM_MARKER="gatekeeper shim"   # строка-маркер внутри shim-обёртки

# Файлы конфигурации (repo)
CONFIG_FILES=(
  "$REPO_ROOT/policies/policy_v1.yaml"
  "$REPO_ROOT/data/leases.json"
)
SHIM_SRC="$REPO_ROOT/shim/systemctl-wrapper"

POLKIT_RULE_SRC="$REPO_ROOT/polkit/10-gatekeeper.rules"
POLKIT_DST="/etc/polkit-1/rules.d/10-gatekeeper.rules"

run() {
  # $1 — готовая команда (одной строкой)
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] $1"
  else
    echo "[apply]   $1"
    eval "$1"
  fi
}

ensure_group() {
  local g="$1"
  if getent group "$g" >/dev/null 2>&1; then
    echo "group '$g' already exists"
  else
    run "groupadd --system '$g'"
  fi
}

echo "=== Phase 5 RBAC ($([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo APPLY)) ==="

# 1) Группы (нужны для владельца/прав)
ensure_group gatekeeper
ensure_group agents

# 2) Конфигурационные файлы -> root:gatekeeper 640
for f in "${CONFIG_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    run "chown root:gatekeeper '$f'"
    run "chmod 640 '$f'"
  else
    echo "WARN: $f not found, skip"
  fi
done

# 3) Исходник shim -> root:gatekeeper 640
if [[ -f "$SHIM_SRC" ]]; then
  run "chown root:gatekeeper '$SHIM_SRC'"
  run "chmod 640 '$SHIM_SRC'"
else
  echo "WARN: $SHIM_SRC not found, skip"
fi

# 4) Развёрнутый wrapper (только если это реально shim, а не оригинальный systemd)
if [[ -f "$DEPLOYED_WRAPPER" ]]; then
  if grep -q "$SHIM_MARKER" "$DEPLOYED_WRAPPER" 2>/dev/null; then
    run "chown root:gatekeeper '$DEPLOYED_WRAPPER'"
    run "chmod $WRAPPER_MODE '$DEPLOYED_WRAPPER'"
    echo "deployed wrapper is the gatekeeper shim -> perms applied ($WRAPPER_MODE)"
  else
    echo "SKIP: $DEPLOYED_WRAPPER не похож на shim (вероятно оригинальный systemd)."
    echo "      Сначала примените dpkg-divert (Фаза 1), затем повторите. Реальный"
    echo "      /usr/bin/systemctl НЕ трогаем (иначе сломается управление systemd)."
  fi
else
  echo "SKIP: $DEPLOYED_WRAPPER не существует (shim ещё не развёрнут?)."
fi

# 5) (опционально) polkit rule — только с --with-polkit
if [[ $WITH_POLKIT -eq 1 ]]; then
  if [[ -f "$POLKIT_RULE_SRC" ]]; then
    run "install -D -m 644 -o root -g root '$POLKIT_RULE_SRC' '$POLKIT_DST'"
    run "systemctl restart polkit"
    echo "polkit rule installed -> $POLKIT_DST"
  else
    echo "WARN: $POLKIT_RULE_SRC not found"
  fi
else
  echo "polkit rule НЕ трогаем (без --with-polkit). Установите вручную, см. polkit/10-gatekeeper.rules."
fi

echo "=== done ==="
