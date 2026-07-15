#!/usr/bin/env bash
# run-sync.sh — обёртка запуска синхронизации семантической памяти.
# Гарантирует взаимное исключение инкрементной (5 мин) и полной
# переиндексации (05:00): пока идёт --rebuild, инкремент НЕ запускается.
set -uo pipefail

LOCK=/root/LabDoctorM/.ops/shared/anythingllm-sync/.sync-systemd.lock
SYNC=/root/LabDoctorM/.ops/shared/anythingllm-sync/sync.py
LEX=/root/LabDoctorM/.ops/shared/anythingllm-sync/lexical_index.py
MODE="${1:-incremental}"

if [ "$MODE" = "rebuild" ]; then
  # Блокирующий flock: ждём завершения инкремента, затем полная переиндексация
  # (sync.py --rebuild) + пересборка лексического индекса (конвергенция).
  exec flock "$LOCK" bash -c "python3 '$SYNC' --rebuild && python3 '$LEX' build"
else
  # Неблокирующий flock: если полная переиндексация уже держит лок — пропускаем.
  if ! flock -n "$LOCK" bash -c "python3 '$SYNC' && python3 '$LEX' build"; then
    echo "$(date -u +%FT%TZ) SKIP incremental: full reindex lock held or prior run failed"
    exit 0
  fi
fi
