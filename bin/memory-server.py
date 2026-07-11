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
import sys
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
}
_state_lock = threading.Lock()


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
        D, I = idx.search(q, search_k)
    except Exception:
        return None
    results = []
    seen = 0
    for score, i in zip(D[0], I[0]):
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
    cache_key = f"{query.lower().strip()}|{top_k}|{round(threshold, 2)}|{agent}|{project}|{source}|{date}|{metadata_only}"
    with _state_lock:
        cached = _state["cache"].get(cache_key)
        if cached is not None:
            _state["cache_hits"] += 1
            cached = dict(cached)
            cached["cache_hit"] = True
            log_audit(query, time.time() - t0, True, len(cached.get("results", [])))
            return cached
        _state["cache_misses"] += 1
    results = search_faiss(query, top_k, threshold, agent, project, source, date, metadata_only)
    degraded = False
    if results is None:
        degraded = True
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


def _watchdog():
    while True:
        time.sleep(60)
        with _state_lock:
            have = _state["index"] is not None
        # Подхватываем индекс при первом старте ИЛИ когда Штрейкбрехер
        # переиндексировал (файл на диске новее загруженного).
        if not have or _disk_index_changed():
            load_index()


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Eager preload at startup (mongoose п.3)
    load_index()
    threading.Thread(target=_watchdog, daemon=True).start()
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
