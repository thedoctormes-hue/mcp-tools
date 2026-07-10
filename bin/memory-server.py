#!/usr/bin/env python3
"""
memory-server.py — STATEFUL MCP-фасад над FAISS/ONNX.

Целевая архитектура (ADR, зона Совы): сервер держит FAISS-индекс
+ метаданные в памяти, эмбеддит запросы через локальный ONNX,
кеширует, и шарит состояние между всеми MCP-клиентами.
НЕ дёргает lab_search.py subprocess на каждый вызов (антипаттерн pass-through).

Жизненный цикл:
  - Старт: пытается загрузить FAISS + meta.pkl в память (один раз).
  - Watchdog (60с): если индекс не загружен — ждёт живой индекс
    от Штрейкбрехера (он чинит reindex/meta.pkl). Как только появился
    — переключается на in-memory FAISS.
  - Fallback: ONNX лёг → keyword/grep через lab_search.py;
    FAISS/meat нет → lab_search.py. Всегда возвращает результат,
    деградируя по качеству, не падая пустым.

Транспорт: MCP_TRANSPORT=http (systemd) | stdio (локально).
Порт: MCP_PORT (default 8087).
"""
import os
import sys
import json
import time
import pickle
import threading
import subprocess
from typing import Dict, Any, List, Optional

import numpy as np
import requests
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────
INDEX_PATH = "/root/.openclaw/memory/lab-faiss.index"
# Штрейкбрехер строит метаданные как JSON (lab-faiss-meta.json), не pickle.
# Совпадает с lab_search.py (DEFAULT_FAISS_META). .pkl оставлен как fallback.
META_PATH = "/root/.openclaw/memory/lab-faiss-meta.json"
META_PATH_PKL = "/root/.openclaw/memory/lab-faiss-meta.pkl"
LAB_SEARCH = "/root/LabDoctorM/projects/lab-memory/scripts/lab_search.py"
ONNX_URL = "http://127.0.0.1:8082/api/embeddings"
AUDIT_LOG = "/var/log/mcp-memory-audit.log"
PORT = int(os.environ.get("MCP_PORT", 8087))
TRANSPORT = os.environ.get("MCP_TRANSPORT", "http")

mcp = FastMCP("memory", host=os.environ.get("MCP_HOST", "127.0.0.1"), port=PORT)

# ── State (in-memory, shared across all clients) ───────────────────────
_state = {
    "index": None,
    "meta": None,
    "loaded_at": None,
    "cache": {},          # query|top_k|threshold -> result dict
    "cache_hits": 0,
    "cache_misses": 0,
    "requests": 0,
    "latency_sum": 0.0,
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


# ── Index loading (one-shot, retried by watchdog) ─────────────────────
def load_index() -> bool:
    """Load FAISS index + meta (JSON, pkl fallback) into memory. True on success."""
    global _state
    try:
        import faiss
        if not os.path.exists(INDEX_PATH):
            return False
        # meta: JSON (Штрейк) приоритет, pickle fallback
        if os.path.exists(META_PATH):
            with open(META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
        elif os.path.exists(META_PATH_PKL):
            with open(META_PATH_PKL, "rb") as f:
                meta = pickle.load(f)
        else:
            return False
        idx = faiss.read_index(INDEX_PATH)
        with _state_lock:
            _state["index"] = idx
            _state["meta"] = meta
            _state["loaded_at"] = time.time()
        return True
    except Exception as e:
        with _state_lock:
            _state["index"] = None
            _state["meta"] = None
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
def search_faiss(query: str, top_k: int, threshold: float) -> Optional[List[Dict]]:
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
        D, I = idx.search(q, top_k)
    except Exception:
        return None
    results = []
    for score, i in zip(D[0], I[0]):
        if i < 0:
            continue
        if threshold and float(score) < threshold:
            continue
        chunk = None
        if isinstance(meta, list):
            chunk = meta[i] if i < len(meta) else None
        elif isinstance(meta, dict):
            chunk = meta.get(str(i), meta.get(i))
        c = chunk or {}
        results.append({
            "score": float(score),
            "file_path": c.get("file_path", ""),
            # новый формат meta: ключ 'source'; старый: 'agent'
            "agent": c.get("agent", c.get("source", "")),
            "project": c.get("project", ""),
            "text": c.get("text", "")[:2000],
            "cache_hit": False,
        })
    return results


# ── Fallback: lab_search.py subprocess ──────────────────────────────────
def search_fallback(query: str, top_k: int) -> List[Dict]:
    try:
        proc = subprocess.run(
            [sys.executable, LAB_SEARCH, "search", query, "--limit", str(top_k)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        items = data if isinstance(data, list) else data.get("results", [])
        return [{
            "score": float(r.get("score", 0)),
            "file_path": r.get("file_path", ""),
            "agent": r.get("agent", ""),
            "project": r.get("project", ""),
            "text": r.get("text", "")[:2000],
            "cache_hit": False,
        } for r in items]
    except Exception as e:
        with _state_lock:
            _state["last_error"] = f"fallback failed: {e}"
        return []


# ── Tools ────────────────────────────────────────────────────────────────
@mcp.tool()
def search(query: str, top_k: int = 5, threshold: float = 0.0) -> Dict[str, Any]:
    """Semantic search over lab memory. FAISS in-memory; degrades to lab_search.py fallback."""
    t0 = time.time()
    with _state_lock:
        _state["requests"] += 1
    cache_key = f"{query}|{top_k}|{threshold}"
    with _state_lock:
        cached = _state["cache"].get(cache_key)
        if cached is not None:
            _state["cache_hits"] += 1
            cached = dict(cached)
            cached["cache_hit"] = True
            log_audit(query, time.time() - t0, True, len(cached.get("results", [])))
            return cached
        _state["cache_misses"] += 1
    results = search_faiss(query, top_k, threshold)
    degraded = False
    if results is None:
        degraded = True
        results = search_fallback(query, top_k)
    out = {
        "query": query,
        "count": len(results),
        "results": results,
        "cache_hit": False,
        "degraded": degraded,
    }
    with _state_lock:
        _state["cache"][cache_key] = out
        _state["latency_sum"] += time.time() - t0
    log_audit(query, time.time() - t0, False, len(results))
    return out


@mcp.tool()
def stats() -> Dict[str, Any]:
    """Index stats: size, cache hit ratio, avg latency, loaded state."""
    with _state_lock:
        req = _state["requests"]
        hits = _state["cache_hits"]
        misses = _state["cache_misses"]
        lat = _state["latency_sum"]
        idx = _state["index"]
        meta = _state["meta"]
        loaded = _state["loaded_at"]
        err = _state["last_error"]
    total_cache = hits + misses
    return {
        "index_loaded": idx is not None,
        "index_ntotal": idx.ntotal if idx else 0,
        "meta_loaded": meta is not None,
        "loaded_at": loaded,
        "requests": req,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_ratio": (hits / total_cache) if total_cache else 0.0,
        "avg_latency": (lat / req) if req else 0.0,
        "last_error": err,
    }


@mcp.tool()
def reload_index() -> Dict[str, Any]:
    """Hot-reload FAISS index + meta from disk (after reindex by Штрейкбрехер)."""
    ok = load_index()
    with _state_lock:
        idx = _state["index"]
    return {"reloaded": ok, "index_ntotal": idx.ntotal if idx else 0}


# ── Watchdog: wait for live index from Штрейкбрехер ───────────────────
def _watchdog():
    while True:
        time.sleep(60)
        with _state_lock:
            have = _state["index"] is not None
        if not have:
            load_index()


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Try load at startup
    load_index()
    threading.Thread(target=_watchdog, daemon=True).start()
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
