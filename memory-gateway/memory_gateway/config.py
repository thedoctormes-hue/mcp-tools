"""Централизованная конфигурация memory-gateway.

Все пути и таймауты — здесь. Значения можно переопределить через env
(префикс MG_) для деплоя, не трогая код.
"""
import os

# ── AnythingLLM REST API (только официальный API, Bearer) ──────────────
ALM_BASE = os.environ.get("MG_ALM_BASE", "http://127.0.0.1:3002/api/v1")

# ── Секреты (600) ──────────────────────────────────────────────────────
# Системный Bearer-токен AnythingLLM для vector-search.
TOKEN_FILE = os.environ.get(
    "MG_TOKEN_FILE",
    "/root/LabDoctorM/workspaces/streikbrecher/secrets/anythingllm_token.txt",
)

# ── Операционная директория (лексический слой) ─────────────────────────
OPS_DIR = os.environ.get(
    "MG_OPS_DIR", "/root/LabDoctorM/.ops/shared/anythingllm-sync"
)
# FTS5/BM25 индекс по .md-корпусу лаборатории (read-only доступ).
LEXICAL_DB = os.environ.get("MG_LEXICAL_DB", os.path.join(OPS_DIR, "lexical.db"))
# Локальный список слагов workspace (быстрее и без прав на /workspaces).
MAP_FILE = os.environ.get(
    "MG_MAP_FILE",
    "/root/LabDoctorM/workspaces/streikbrecher/secrets/anythingllm_workspaces.map",
)

# ── Таймауты (сек) ─────────────────────────────────────────────────────
LIST_TIMEOUT = float(os.environ.get("MG_LIST_TIMEOUT", "15"))
SEARCH_TIMEOUT = float(os.environ.get("MG_SEARCH_TIMEOUT", "30"))

# ── Поиск ──────────────────────────────────────────────────────────────
DEFAULT_TOP_K = int(os.environ.get("MG_DEFAULT_TOP_K", "5"))
MAX_TOP_K = int(os.environ.get("MG_MAX_TOP_K", "25"))
VECTOR_SCORE_THRESHOLD = float(os.environ.get("MG_VECTOR_SCORE_THRESHOLD", "0.15"))
RRF_K = int(os.environ.get("MG_RRF_K", "60"))
QUERY_MAX_LEN = int(os.environ.get("MG_QUERY_MAX_LEN", "1000"))
# Сколько кандидатов тянуть из каждого слоя перед слиянием (recall > precision).
CANDIDATE_MULT = int(os.environ.get("MG_CANDIDATE_MULT", "3"))
# Ограничение параллельных vector-запросов по workspace.
VECTOR_MAX_WORKERS = int(os.environ.get("MG_VECTOR_MAX_WORKERS", "6"))

# ── Логи ───────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get(
    "MG_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"),
)
LOG_FILE = os.environ.get("MG_LOG_FILE", os.path.join(LOG_DIR, "memory-gateway.log"))
LOG_LEVEL = os.environ.get("MG_LOG_LEVEL", "INFO")

# ── MCP-транспорт ──────────────────────────────────────────────────────
# stdio — для запуска как subprocess из конфига агента (рекомендуется);
# streamable-http — для сетевого деплоя (systemd), порт MG_PORT.
TRANSPORT = os.environ.get("MG_TRANSPORT", "stdio")
HOST = os.environ.get("MG_HOST", "127.0.0.1")
PORT = int(os.environ.get("MG_PORT", "8091"))
