#!/usr/bin/env python3
"""
memory-server.py — STATEFUL MCP-фасад над FAISS/ONNX.

Целевая архитектура (ADR, зона Совы): сервер держит FAISS-индекс
+ метаданные в памяти, эмбеддит запросы через локальный ONNX,
кеширует, и шарит состояние между всеми MCP-клиентами.

Улучшения (2026-07-11, EBSW):
- Фильтры agent/project/source/date (фидбек mongoose п.1)
- metadata_only + lab_memory_get_chunk(id) (п.2)
- ready-флаг + блокировка холодного старта (п.3)
- Нормализация кэш-ключа (п.4)
- Fallback → in-memory keyword-индекс, БЕЗ subprocess (п.5)
- Input validation + p95 latency + threshold 0.3 (п.6)
- Security: валидация query, Origin (через FastMCP)

Транспорт: MCP_TRANSPORT=http (systemd) | stdio (локально).
Порт: MCP_PORT (default 8087).
"""
import os
import json
import time
import re
import threading
import pickle
from collections import deque
from typing import Dict, Any, List, Optional

import numpy as np
import requests
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────
INDEX_PATH = "/root/.openclaw/memory/lab-faiss.index"
META_PATH = "/root/.openclaw/memory/lab-faiss-meta.json"
META_PATH_PKL = "/root/.openclaw/memory/lab-faiss-meta.pkl"
ONNX_URL = "http://127.0.0.1:8082/api/embeddings"
AUDIT_LOG = "/var/log/mcp-memory-audit.log"
PORT = int(os.environ.get("MCP_PORT", 8087))
TRANSPORT = os.environ.get("MCP_TRANSPORT", "http")
QUERY_MAX_LEN = 1000
DEFAULT_THRESHOLD = 0.3  # mongoose п.6: отсекать шум

# ── On-demand / observability (opt-in, experiment 2026-07-13) ───────────
# MCP_IDLE_SHUTDOWN_SEC>0: сервер сам гасится после N сек простоя (on-demand).
IDLE_SHUTDOWN_SEC = int(os.environ.get("MCP_IDLE_SHUTDOWN_SEC", 0))
# MCP_METRICS_PORT>0: side-car HTTP /health + /metrics на этом порту (0 = выкл).
METRICS_PORT = int(os.environ.get("MCP_METRICS_PORT", 0))
# MCP_BACKEND: "hybrid" (по умолчанию, RRF над FAISS+keyword в процессе) |
#             "faiss" (только семантика) | "lab_search" (subprocess в lab_search.py).
# hybrid — ДЕФОЛТ: агенты получают семантику + лексический BM25-backup без OOM-subprocess.
BACKEND = os.environ.get("MCP_BACKEND", "hybrid")
# Путь к lab_search.py (HYBRID-бэкенд). Делегирование без форка алгоритма Штрейкбрехера.
LAB_SEARCH_PATH = os.environ.get("LAB_SEARCH_PATH", "/root/LabDoctorM/projects/lab-memory/scripts")
# Side-car HTTP для /startup-brief (ретаргетинг startupContext на эндпоинт индекса). 0 = выкл.
STARTUP_BRIEF_PORT = int(os.environ.get("STARTUP_BRIEF_PORT", 8093))

mcp = FastMCP("memory", host=os.environ.get("MCP_HOST", "127.0.0.1"), port=PORT)

# ── State (in-memory, shared across all clients) ───────────────────────
_state = {
    "index": None,
    "meta": None,
    "keyword_index": None,   # term -> list of chunk indices (fallback)
    "loaded_at": None,
    "index_mtime": None,      # mtime подхваченного индекса (для авто-reload)
    "meta_mtime": None,       # mtime подхваченного meta (для авто-reload)
    "ready": False,           # mongoose п.3: сигнал готовности
    "cache": {},              # normalized_key -> result dict
    "cache_hits": 0,
    "cache_misses": 0,
    "requests": 0,
    "latencies": deque(maxlen=200),  # для p95
    "last_error": None,
    "last_activity": time.time(),    # on-demand: время последнего запроса
    "degraded_total": 0,             # on-demand: счётчик деградаций (fallback)
}
_state_lock = threading.Lock()
# Отдельный лок сериализует ТОЛЬКО перезагрузку индекса (тяжёлую), чтобы при
# гонке двух запросов индекс грузил ровно один поток, а не оба разом.
_reload_lock = threading.Lock()


# ── Audit ────────────────────────────────────────────────────────────────
def log_audit(query: str, latency: float, cache_hit: bool, count: int, err: Optional[str] = None):
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{ts}Z query={query!r} lat={latency:.3f} cache_hit={cache_hit} count={count} err={err}\n")
    except Exception:
        pass


# ── Keyword index (in-memory fallback, БЕЗ subprocess) ──────────────────
def _build_keyword_index(meta) -> Dict[str, List[int]]:
    """Простой инвертированный индекс по тексту чанков. Для fallback при ONNX-down."""
    ki: Dict[str, List[int]] = {}
    items = meta if isinstance(meta, list) else []
    for i, c in enumerate(items):
        text = (c.get("text", "") or "").lower()
        for term in set(re.findall(r"[a-zа-я0-9_]+", text)):
            ki.setdefault(term, []).append(i)
    return ki


# ── Index loading (one-shot, retried by watchdog) ─────────────────────
def load_index() -> bool:
    """Load FAISS index + meta (JSON, pkl fallback) into memory. True on success."""
    global _state
    try:
        import faiss
        if not os.path.exists(INDEX_PATH):
            return False
        if os.path.exists(META_PATH):
            with open(META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
        elif os.path.exists(META_PATH_PKL):
            with open(META_PATH_PKL, "rb") as f:
                meta = pickle.load(f)  # noqa: S301 (legacy fallback)
        else:
            return False
        idx = faiss.read_index(INDEX_PATH)
        ki = _build_keyword_index(meta)
        if idx.ntotal != len(meta):
            # Рассинхрон index/meta (напр. своп в процессе) — не отдаём
            # неконсистентный индекс; watchdog перезагрузит позже.
            raise RuntimeError(
                f"index/meta mismatch: ntotal={idx.ntotal} meta={len(meta)}")
        idx_mtime = os.path.getmtime(INDEX_PATH)
        meta_mtime = (
            os.path.getmtime(META_PATH) if os.path.exists(META_PATH)
            else os.path.getmtime(META_PATH_PKL) if os.path.exists(META_PATH_PKL)
            else None
        )
        with _state_lock:
            _state["index"] = idx
            _state["meta"] = meta
            _state["keyword_index"] = ki
            _state["loaded_at"] = time.time()
            _state["index_mtime"] = idx_mtime
            _state["meta_mtime"] = meta_mtime
            _state["ready"] = True
            _state["last_error"] = None
            # Индекс сменился на диске → старые результаты в кэше протухли
            _state["cache"] = {}
        return True
    except Exception as e:
        with _state_lock:
            _state["index"] = None
            _state["meta"] = None
            _state["keyword_index"] = None
            _state["ready"] = False
            _state["last_error"] = f"load_index failed: {e}"
        return False


# ── Embedding (ONNX, local, no API keys) ─────────────────────────────
def embed(query: str) -> Optional[List[float]]:
    try:
        r = requests.post(ONNX_URL, json={"input": query}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            if "data" in data and data["data"]:
                return data["data"][0].get("embedding")
            if "embedding" in data:
                return data["embedding"]
        return None
    except Exception:
        return None


# ── FAISS in-memory search ──────────────────────────────────────────────
def _filter_meta(chunk: Dict, agent: str, project: str, source: str, date: str) -> bool:
    if agent and chunk.get("agent", chunk.get("source", "")).lower() != agent.lower():
        return False
    if project and chunk.get("project", "").lower() != project.lower():
        return False
    if source and chunk.get("source", "").lower() != source.lower():
        return False
    if date and date not in (chunk.get("file_path", "") or ""):
        return False
    return True


def search_faiss(query: str, top_k: int, threshold: float,
                 agent: str = "", project: str = "", source: str = "", date: str = "",
                 metadata_only: bool = False) -> Optional[List[Dict]]:
    """Returns list of result dicts, or None to signal fallback."""
    with _state_lock:
        idx = _state["index"]
        meta = _state["meta"]
    if idx is None or meta is None:
        return None
    vec = embed(query)
    if vec is None:
        return None  # ONNX down → fallback
    try:
        import faiss
        q = np.array([vec], dtype="float32")
        faiss.normalize_L2(q)  # cosine/IP индекс: запрос должен быть L2-норм
        # При фильтрах ищем шире, чтобы post-filter не отсёк релевантное
        has_filter = any([agent, project, source, date])
        search_k = min(idx.ntotal, 500) if has_filter else top_k * 3
        D, Idx = idx.search(q, search_k)
    except Exception:
        return None
    results = []
    seen = 0
    for score, i in zip(D[0], Idx[0]):
        if i < 0:
            continue
        chunk = meta[i] if isinstance(meta, list) else meta.get(str(i), meta.get(i))
        c = chunk or {}
        if not _filter_meta(c, agent, project, source, date):
            continue
        results.append({
            "id": int(i),
            "score": float(score),
            "file_path": c.get("file_path", ""),
            "agent": c.get("agent", c.get("source", "")),
            "project": c.get("project", ""),
            "text": "" if metadata_only else c.get("text", "")[:2000],
            "cache_hit": False,
        })
        seen += 1
        if seen >= top_k:
            break
    return results


# ── Fallback: in-memory keyword index (БЕЗ subprocess) ──────────────────
def search_keyword(query: str, top_k: int,
                   agent: str = "", project: str = "", source: str = "", date: str = "",
                   metadata_only: bool = False) -> List[Dict]:
    """Лёгкий keyword-поиск по in-memory индексу. Замена subprocess lab_search.py (mongoose п.5)."""
    with _state_lock:
        ki = _state["keyword_index"]
        meta = _state["meta"]
    if not ki or not meta:
        return []
    terms = set(re.findall(r"[a-zа-я0-9_]+", query.lower()))
    if not terms:
        return []
    # score = количество совпавших терминов
    scores: Dict[int, int] = {}
    for t in terms:
        for ci in ki.get(t, []):
            scores[ci] = scores.get(ci, 0) + 1
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k * 3]
    out = []
    for ci, sc in ranked:
        c = meta[ci] if isinstance(meta, list) else meta.get(str(ci), meta.get(ci))
        if not c:
            continue
        if not _filter_meta(c, agent, project, source, date):
            continue
        out.append({
            "id": int(ci),
            "score": float(sc) / len(terms),  # нормализованная близость 0..1
            "file_path": c.get("file_path", ""),
            "agent": c.get("agent", c.get("source", "")),
            "project": c.get("project", ""),
            "text": "" if metadata_only else c.get("text", "")[:2000],
            "cache_hit": False,
        })
        if len(out) >= top_k:
            break
    return out


# ── Hybrid (in-process RRF: FAISS + keyword) ──────────────────────────
def _rrf_merge(result_lists, k: int = 60):
    """Reciprocal Rank Fusion над несколькими списками результатов.
    Дедуп по id; итоговый ранг = сумма 1/(rank+k). Возвращает list[dict]."""
    merged = {}
    for rl in result_lists:
        if not rl:
            continue
        for rank, item in enumerate(rl, start=1):
            rid = item.get("id")
            if rid is None:
                continue
            entry = merged.setdefault(rid, {"item": item, "rrf": 0.0})
            entry["rrf"] += 1.0 / (rank + k)
    ordered = sorted(merged.values(), key=lambda e: e["rrf"], reverse=True)
    return [e["item"] for e in ordered]


def search_hybrid(query: str, top_k: int, threshold: float,
                 agent: str = "", project: str = "", source: str = "", date: str = "",
                 metadata_only: bool = False):
    """Гибрид: RRF(FAISS-семантика + keyword-лексический), оба in-process.
    Лексический keyword_index всегда свежий (строится из текущей meta при загрузке).
    Если ONNX-down (search_faiss=None) — деградирует до чистого keyword (без пустоты)."""
    faiss_raw = search_faiss(query, top_k * 3, threshold, agent, project, source, date, metadata_only)
    faiss_ok = faiss_raw is not None
    faiss_res = faiss_raw or []
    kw_res = search_keyword(query, top_k * 3, agent, project, source, date, metadata_only)
    combined = _rrf_merge([faiss_res, kw_res])
    return combined[:top_k], (not faiss_ok)


# ── Unified backend delegation (Tier 0): lab_search HYBRID через subprocess ──
def _lab_search_subprocess(query, top_k, agent="", project="", date="", metadata_only=False):
    """Делегирование в lab_search.py (HYBRID: FAISS+FTS5/RRF). Без форка алгоритма.
    Возвращает list[dict] или бросает Exception (caller решит — fallback на faiss)."""
    import subprocess
    cmd = ["python3", os.path.join(LAB_SEARCH_PATH, "lab_search.py"), "search", query,
           "--limit", str(top_k)]
    if agent:
        cmd += ["--agent", agent]
    if project:
        cmd += ["--project", project]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"lab_search rc={out.returncode}: {out.stderr[:200]}")
    data = json.loads(out.stdout)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    raise RuntimeError("unexpected lab_search output shape")


def _startup_brief(agent, limit, query=None):
    """Курируемый блок для старта сессии агента. ВСЕГДА lab_search (HYBRID) —
    вызов редкий (раз в старт сессии), поэтому subprocess-стоимость неважна,
    а качество инъекции (BM25+semantic) — то, что нужно. Fallback на faiss."""
    q = query or f"{agent} identity role context responsibilities startup briefing"
    try:
        return _lab_search_subprocess(q, limit, agent=agent), "lab_search"
    except Exception as e:
        with _state_lock:
            _state["last_error"] = f"startup_brief lab_search failed ({e}); faiss fallback"
    res = search_faiss(q, limit, 0.0, agent=agent) or []
    return res, "faiss"


# ── Tools ────────────────────────────────────────────────────────────────
@mcp.tool(name="lab_memory_search")
def search(query: str, top_k: int = 5, threshold: float = DEFAULT_THRESHOLD,
           agent: str = "", project: str = "", source: str = "", date: str = "",
           metadata_only: bool = False) -> Dict[str, Any]:
    """Semantic search over lab memory (FAISS in-memory). Filters: agent/project/source/date.
    metadata_only=True returns facets without text. Degrades to keyword-index fallback (no subprocess)."""
    t0 = time.time()
    # Input validation (security, spec)
    if not query or not query.strip():
        return {"query": query, "count": 0, "results": [], "cache_hit": False,
                "degraded": False, "error": "empty query"}
    query = query.strip()[:QUERY_MAX_LEN]
    with _state_lock:
        _state["requests"] += 1
        _state["last_activity"] = time.time()
    cache_key = f"{query.lower().strip()}|{top_k}|{round(threshold, 2)}|{agent}|{project}|{source}|{date}|{metadata_only}"
    # Проверяем свежесть ДО чтения кэша: если индекс на диске сменился,
    # _ensure_fresh_index перезагрузит его и очистит кэш (load_index чистит
    # cache), поэтому протухшие результаты не отдадим.
    _ensure_fresh_index()
    with _state_lock:
        cached = _state["cache"].get(cache_key)
        if cached is not None:
            _state["cache_hits"] += 1
            cached = dict(cached)
            cached["cache_hit"] = True
            log_audit(query, time.time() - t0, True, len(cached.get("results", [])))
            return cached
        _state["cache_misses"] += 1
    if BACKEND == "lab_search":
        try:
            delegated = _lab_search_subprocess(query, top_k, agent, project, date, metadata_only)
            out = {
                "query": query,
                "count": len(delegated),
                "results": delegated,
                "cache_hit": False,
                "degraded": False,
                "backend": "lab_search",
            }
            with _state_lock:
                _state["cache"][cache_key] = out
                _state["latencies"].append(time.time() - t0)
            log_audit(query, time.time() - t0, False, len(delegated))
            return out
        except Exception as e:
            with _state_lock:
                _state["last_error"] = f"lab_search backend failed ({e}); fallback to faiss"
    if BACKEND == "hybrid":
        try:
            delegated, degraded = search_hybrid(query, top_k, threshold, agent, project, source, date, metadata_only)
            out = {
                "query": query,
                "count": len(delegated),
                "results": delegated,
                "cache_hit": False,
                "degraded": degraded,
                "backend": "hybrid",
            }
            with _state_lock:
                _state["cache"][cache_key] = out
                _state["latencies"].append(time.time() - t0)
            log_audit(query, time.time() - t0, False, len(delegated))
            return out
        except Exception as e:
            with _state_lock:
                _state["last_error"] = f"hybrid backend failed ({e}); fallback to faiss"
    results = search_faiss(query, top_k, threshold, agent, project, source, date, metadata_only)
    degraded = False
    if results is None:
        degraded = True
        with _state_lock:
            _state["degraded_total"] += 1
        results = search_keyword(query, top_k, agent, project, source, date, metadata_only)
    out = {
        "query": query,
        "count": len(results),
        "results": results,
        "cache_hit": False,
        "degraded": degraded,
    }
    with _state_lock:
        _state["cache"][cache_key] = out
        _state["latencies"].append(time.time() - t0)
    log_audit(query, time.time() - t0, False, len(results))
    return out


@mcp.tool(name="lab_memory_get_chunk")
def get_chunk(id: int, max_chars: int = 4000) -> Dict[str, Any]:
    """Fetch full text of a chunk by id (from metadata). Use after lab_memory_search metadata_only=True."""
    with _state_lock:
        meta = _state["meta"]
    if meta is None:
        return {"id": id, "found": False, "text": ""}
    c = meta[id] if isinstance(meta, list) else meta.get(str(id), meta.get(id))
    if not c:
        return {"id": id, "found": False, "text": ""}
    return {
        "id": id,
        "found": True,
        "file_path": c.get("file_path", ""),
        "agent": c.get("agent", c.get("source", "")),
        "project": c.get("project", ""),
        "text": c.get("text", "")[:max_chars],
    }


@mcp.tool(name="lab_memory_stats")
def stats() -> Dict[str, Any]:
    """Index stats: size, cache hit ratio, avg/p95 latency, loaded state, ready signal."""
    with _state_lock:
        req = _state["requests"]
        hits = _state["cache_hits"]
        misses = _state["cache_misses"]
        lats = list(_state["latencies"])
        idx = _state["index"]
        meta = _state["meta"]
        loaded = _state["loaded_at"]
        idx_mtime = _state["index_mtime"]
        ready = _state["ready"]
        err = _state["last_error"]
    total_cache = hits + misses
    avg = (sum(lats) / len(lats)) if lats else 0.0
    # p95 требует отсортированной выборки (перцентиль по порядковой статистике).
    if lats:
        slats = sorted(lats)
        p95_idx = min(int(len(slats) * 0.95), len(slats) - 1)
        p95 = slats[p95_idx]
    else:
        p95 = 0.0
    return {
        "ready": ready,
        "index_loaded": idx is not None,
        "index_ntotal": idx.ntotal if idx else 0,
        "meta_loaded": meta is not None,
        "loaded_at": loaded,
        "index_mtime": idx_mtime,
        "index_stale": _disk_index_changed(),
        "requests": req,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_ratio": (hits / total_cache) if total_cache else 0.0,
        "avg_latency": avg,
        "p95_latency": p95,
        "last_error": err,
    }


@mcp.tool(name="lab_memory_reload")
def reload_index() -> Dict[str, Any]:
    """Hot-reload FAISS index + meta from disk (after reindex by Штрейкбрехер)."""
    ok = load_index()
    with _state_lock:
        idx = _state["index"]
        ready = _state["ready"]
    return {"reloaded": ok, "ready": ready, "index_ntotal": idx.ntotal if idx else 0}


# ── Watchdog: wait for live index from Штрейкбрехер ───────────────────
def _disk_index_changed() -> bool:
    """True если индекс/meta на диске новее подхваченного в памяти."""
    try:
        with _state_lock:
            cur_idx_mtime = _state["index_mtime"]
            cur_meta_mtime = _state["meta_mtime"]
        if not os.path.exists(INDEX_PATH):
            return False
        disk_idx = os.path.getmtime(INDEX_PATH)
        disk_meta = (
            os.path.getmtime(META_PATH) if os.path.exists(META_PATH)
            else os.path.getmtime(META_PATH_PKL) if os.path.exists(META_PATH_PKL)
            else None
        )
        if cur_idx_mtime is None or disk_idx > cur_idx_mtime:
            return True
        if disk_meta is not None and (cur_meta_mtime is None or disk_meta > cur_meta_mtime):
            return True
        return False
    except Exception:
        return False


def _ensure_fresh_index() -> bool:
    """Check-on-request: если индекс на диске новее загруженного — перезагрузить.

    Дешёвый путь: один os.stat через _disk_index_changed(). Если ничего не
    менялось (обычный случай) — мгновенный возврат, никакой тяжёлой работы.

    Защита от гонки: если несколько запросов одновременно увидели "устарело",
    _reload_lock пропускает грузить ровно один поток; остальные ждут, затем
    повторная проверка под локом показывает "уже свежо" — и они просто ищут.
    Returns True если была выполнена перезагрузка.
    """
    if not _disk_index_changed():
        return False
    with _reload_lock:
        # double-check под локом: пока ждали, другой поток мог уже перезагрузить
        if not _disk_index_changed():
            return False
        return load_index()


# ── On-demand: idle-shutdown + observability side-car (opt-in) ───────────
def _idle_shutdown_watchdog():
    """Гасит процесс после IDLE_SHUTDOWN_SEC сек простоя. Флаг=0 → no-op.
    on-demand паттерн: сервер не висит впустую, экономит ~350MB RAM."""
    if IDLE_SHUTDOWN_SEC <= 0:
        return
    while True:
        time.sleep(max(5, IDLE_SHUTDOWN_SEC // 10))
        with _state_lock:
            last = _state.get("last_activity") or time.time()
        idle = time.time() - last
        if idle >= IDLE_SHUTDOWN_SEC:
            os._exit(0)  # юнит Restart=no не воскрешает


import http.server  # noqa: E402
import socketserver  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402


def _metrics_text() -> str:
    with _state_lock:
        req = _state["requests"]
        hits = _state["cache_hits"]
        misses = _state["cache_misses"]
        deg = _state["degraded_total"]
        idx = _state["index"]
        lats = list(_state["latencies"])
        ready = int(bool(_state["ready"]))
    # _disk_index_changed() сам берёт _state_lock -> вызываем ВНЕ лока,
    # иначе re-entrant deadlock (threading.Lock не реентерабелен).
    stale = int(_disk_index_changed())
    p95 = 0.0
    if lats:
        sl = sorted(lats)
        p95 = sl[min(int(len(sl) * 0.95), len(sl) - 1)]
    return (
        f"mcp_up 1\n"
        f"mcp_ready {ready}\n"
        f"mcp_requests_total {req}\n"
        f"mcp_cache_hits_total {hits}\n"
        f"mcp_cache_misses_total {misses}\n"
        f"mcp_degraded_total {deg}\n"
        f"mcp_p95_latency_seconds {p95:.4f}\n"
        f"mcp_index_ntotal {idx.ntotal if idx else 0}\n"
        f"mcp_index_stale {stale}\n"
    )


class _MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            with _state_lock:
                body = json.dumps({
                    "up": True,
                    "ready": bool(_state["ready"]),
                    "index_ntotal": _state["index"].ntotal if _state["index"] else 0,
                    "degraded_total": _state["degraded_total"],
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif p == "/metrics":
            body = _metrics_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass


def _start_metrics_server():
    if METRICS_PORT <= 0:
        return
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", METRICS_PORT), _MetricsHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()


# ── Startup-brief side-car HTTP (Tier 1): ретаргетинг startupContext на индекс ──
class _StartupBriefHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p != "/startup-brief":
            body = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        qs = parse_qs(urlparse(self.path).query)
        agent = qs.get("agent", [""])[0]
        try:
            limit = max(1, min(int(qs.get("limit", ["8"])[0]), 20))
        except ValueError:
            limit = 8
        q = qs.get("query", [None])[0]
        try:
            chunks, backend = _startup_brief(agent, limit, q)
        except Exception:
            chunks, backend = [], "error"
        body = json.dumps(
            {"agent": agent, "backend": backend, "count": len(chunks), "chunks": chunks},
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_startup_brief_server():
    if STARTUP_BRIEF_PORT <= 0:
        return
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", STARTUP_BRIEF_PORT), _StartupBriefHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Eager preload at startup: подхватываем то, что уже лежит на диске.
    # Дальше свежесть держится ленивой ревалидацией на каждом запросе
    # (_ensure_fresh_index) — без фонового поллинга и без inotify.
    load_index()
    # On-demand / observability (opt-in): side-car метрики + idle-shutdown.
    _start_metrics_server()
    _start_startup_brief_server()
    threading.Thread(target=_idle_shutdown_watchdog, daemon=True).start()
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
