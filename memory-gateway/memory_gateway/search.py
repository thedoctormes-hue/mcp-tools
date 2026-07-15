"""Гибридный retrieval: vector (AnythingLLM) + lexical (FTS5/BM25) + RRF.

Контракт: только сырые данные. Никаких /chat, никаких LLM-синтезов.
Оба слоя выполняются параллельно; результаты объединяются Reciprocal Rank
Fusion (RRF) с дедупликацией по канонической цели документа.
"""
import concurrent.futures
import os
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional

import requests

from . import config
from .logger import get_logger

log = get_logger()

# ── Секрет: кэшируем токен в памяти, читаем один раз ────────────────────
_token_cache: Optional[str] = None
_token_lock = threading.Lock()


def load_token() -> str:
    """Читает Bearer-токен из TOKEN_FILE (600). Кэширует в памяти."""
    global _token_cache
    with _token_lock:
        if _token_cache:
            return _token_cache
        path = config.TOKEN_FILE
        if not os.path.exists(path):
            raise RuntimeError(f"token file not found: {path}")
        # Мягкая проверка прав: секрет не должен быть мир-читаемым.
        try:
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                log.warning("token file %s has loose perms %o (expect 600)", path, mode)
        except OSError:
            pass
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if not tok:
            raise RuntimeError(f"token file empty: {path}")
        _token_cache = tok
        return tok


# ── Список слагов workspace ─────────────────────────────────────────────
def workspace_slugs() -> List[str]:
    """Локальный список слагов из MAP_FILE (быстро, без /workspaces).
    Fallback: официальный GET /workspaces."""
    slugs: List[str] = []
    if os.path.exists(config.MAP_FILE):
        try:
            with open(config.MAP_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # формат "src_slug real_slug" — берём РЕАЛЬНЫЙ (2-е поле).
                    parts = line.split()
                    slugs.append(parts[1] if len(parts) >= 2 else parts[0])
        except OSError as e:
            log.warning("map read failed: %s", e)
    if slugs:
        return sorted(set(slugs))
    # fallback: официальный API
    try:
        tok = load_token()
        r = requests.get(
            f"{config.ALM_BASE}/workspaces",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=config.LIST_TIMEOUT,
        )
        r.raise_for_status()
        return sorted({w["slug"] for w in r.json().get("workspaces", [])})
    except Exception as e:  # noqa: BLE001
        log.error("workspace list failed (map+api): %s", e)
        return []


# ── Vector-слой (AnythingLLM /vector-search) ────────────────────────────
def _vector_search_one(slug: str, query: str, top_k: int, threshold: float) -> List[Dict[str, Any]]:
    tok = load_token()
    try:
        r = requests.post(
            f"{config.ALM_BASE}/workspace/{slug}/vector-search",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={"query": query, "topN": top_k, "scoreThreshold": threshold},
            timeout=config.SEARCH_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("vector-search %s -> HTTP %s", slug, r.status_code)
            return []
        out = []
        for res in r.json().get("results", []):
            meta = res.get("metadata", {}) or {}
            title = meta.get("title") or meta.get("docSource") or "?"
            out.append({
                "source": "vector",
                "workspace": slug,
                "title": title,
                "doc_id": _doc_id_from_meta(meta, title),
                "text": (res.get("text") or "")[:2000],
                "vector_score": float(res.get("score", 0.0)),
            })
        return out
    except requests.Timeout:
        log.warning("vector-search %s timeout", slug)
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("vector-search %s error: %s", slug, e)
        return []


def _doc_id_from_meta(meta: Dict[str, Any], title: str) -> str:
    """Каноническая идентификация документа для дедупликации/get_document.
    Приоритет: relative path (если есть) -> title/basename."""
    for k in ("docpath", "path", "url", "chunkSource"):
        v = meta.get(k)
        if v:
            return str(v).replace("file://", "").lstrip("/")
    return title


def vector_search(query: str, top_k: int, threshold: float,
                  workspace: Optional[str] = None) -> List[Dict[str, Any]]:
    """Векторный поиск по одному или всем workspace (параллельно)."""
    slugs = [workspace] if workspace else workspace_slugs()
    if not slugs:
        return []
    hits: List[Dict[str, Any]] = []
    workers = max(1, min(len(slugs), config.VECTOR_MAX_WORKERS))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_vector_search_one, s, query, top_k, threshold) for s in slugs]
        for fut in concurrent.futures.as_completed(futs):
            try:
                hits.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                log.warning("vector future error: %s", e)
    hits.sort(key=lambda h: h["vector_score"], reverse=True)
    return hits[:top_k]


# ── Lexical-слой (FTS5 / BM25 по lexical.db, read-only) ──────────────────
def _lexical_connect() -> sqlite3.Connection:
    """Read-only соединение к lexical.db (защита от записи/повреждения)."""
    uri = f"file:{config.LEXICAL_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=config.SEARCH_TIMEOUT)


def lexical_search(query: str, top_k: int) -> List[Dict[str, Any]]:
    """FTS5/BM25 полнотекстовый поиск. OR по токенам (recall), BM25 ранжирует."""
    if not os.path.exists(config.LEXICAL_DB):
        log.warning("lexical.db missing: %s", config.LEXICAL_DB)
        return []
    tokens = [t for t in re.split(r"\W+", query) if len(t) >= 2]
    if not tokens:
        return []
    match = " OR ".join(tokens)
    try:
        conn = _lexical_connect()
        try:
            sql = (
                "SELECT path, title, bm25(docs_fts) AS rank, "
                "snippet(docs_fts, 0, '\u27e8b\u27e9', '\u27e8/b\u27e9', '\u2026', 12) "
                "FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?"
            )
            rows = conn.execute(sql, (match, top_k)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.error("lexical query failed: %s", e)
        return []
    out = []
    for path, title, rank, snip in rows:
        out.append({
            "source": "lexical",
            "workspace": None,
            "title": title,
            "doc_id": path,               # rel path — каноничный id для get_document
            "text": (snip or "").strip(),
            "lexical_score": round(-float(rank), 4),  # bm25 negative -> higher=better
        })
    return out


# ── Слияние: Reciprocal Rank Fusion + дедуп ─────────────────────────────
def _dedup_key(item: Dict[str, Any]) -> str:
    """Ключ дедупликации: базовое имя документа, регистронезависимо.
    Сводит vector(title=CHANGELOG.md) и lexical(path=.../CHANGELOG.md)."""
    did = item.get("doc_id") or item.get("title") or ""
    return os.path.basename(str(did)).lower()


def rrf_merge(vector_hits: List[Dict[str, Any]],
              lexical_hits: List[Dict[str, Any]],
              top_k: int) -> List[Dict[str, Any]]:
    """RRF: score = sum(1/(rank+K)) по спискам. Дедуп по имени документа.
    При дубле сохраняем самый информативный вариант (с непустым text/workspace)."""
    k = config.RRF_K
    merged: Dict[str, Dict[str, Any]] = {}
    for hits in (vector_hits, lexical_hits):
        for rank, item in enumerate(hits, start=1):
            key = _dedup_key(item)
            if not key:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "doc_id": item.get("doc_id"),
                    "title": item.get("title"),
                    "workspace": item.get("workspace"),
                    "text": item.get("text", ""),
                    "sources": [],
                    "vector_score": None,
                    "lexical_score": None,
                    "rrf_score": 0.0,
                }
                merged[key] = entry
            entry["rrf_score"] += 1.0 / (rank + k)
            src = item.get("source")
            if src and src not in entry["sources"]:
                entry["sources"].append(src)
            if item.get("vector_score") is not None:
                entry["vector_score"] = item["vector_score"]
            if item.get("lexical_score") is not None:
                entry["lexical_score"] = item["lexical_score"]
            # предпочесть более полный контекст и реальный doc_id-путь
            if len(item.get("text", "")) > len(entry["text"]):
                entry["text"] = item["text"]
            if item.get("workspace") and not entry["workspace"]:
                entry["workspace"] = item["workspace"]
            if "/" in str(item.get("doc_id", "")) and "/" not in str(entry["doc_id"] or ""):
                entry["doc_id"] = item["doc_id"]
    ordered = sorted(merged.values(), key=lambda e: e["rrf_score"], reverse=True)
    for e in ordered:
        e["rrf_score"] = round(e["rrf_score"], 6)
    return ordered[:top_k]


# ── Публичный гибридный поиск ───────────────────────────────────────────
def hybrid_search(query: str, top_k: int,
                  workspace: Optional[str] = None) -> Dict[str, Any]:
    """Гибрид: vector + lexical ПАРАЛЛЕЛЬНО, слияние RRF. Возвращает чистый JSON.

    degraded=True, если один из слоёв недоступен (второй всё равно вернёт данные).
    """
    query = (query or "").strip()[: config.QUERY_MAX_LEN]
    if not query:
        return {"query": query, "count": 0, "results": [], "degraded": False,
                "layers": {"vector": 0, "lexical": 0}, "error": "empty query"}

    top_k = max(1, min(int(top_k or config.DEFAULT_TOP_K), config.MAX_TOP_K))
    cand = top_k * config.CANDIDATE_MULT

    vector_hits: List[Dict[str, Any]] = []
    lexical_hits: List[Dict[str, Any]] = []
    vec_ok = lex_ok = True

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_vec = ex.submit(vector_search, query, cand, config.VECTOR_SCORE_THRESHOLD, workspace)
        f_lex = ex.submit(lexical_search, query, cand)
        try:
            vector_hits = f_vec.result()
        except Exception as e:  # noqa: BLE001
            vec_ok = False
            log.error("vector layer failed: %s", e)
        try:
            lexical_hits = f_lex.result()
        except Exception as e:  # noqa: BLE001
            lex_ok = False
            log.error("lexical layer failed: %s", e)

    results = rrf_merge(vector_hits, lexical_hits, top_k)
    return {
        "query": query,
        "count": len(results),
        "results": results,
        "degraded": not (vec_ok and lex_ok),
        "layers": {"vector": len(vector_hits), "lexical": len(lexical_hits)},
    }


# ── get_document: полный сырой текст документа ──────────────────────────
def get_document(doc_id: str, max_chars: int = 20000) -> Dict[str, Any]:
    """Полный сырой текст документа по doc_id.

    Стратегия (raw retrieval, без LLM):
    1) lexical.db (полный content) по точному path, затем по basename;
    2) fallback — метаданные официального API AnythingLLM (/document/:name).
    """
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return {"doc_id": doc_id, "found": False, "error": "empty doc_id"}

    # 1) полный текст из lexical.db
    if os.path.exists(config.LEXICAL_DB):
        try:
            conn = _lexical_connect()
            try:
                row = conn.execute(
                    "SELECT path, title, content FROM docs_fts WHERE path = ? LIMIT 1",
                    (doc_id,),
                ).fetchone()
                if row is None:
                    base = os.path.basename(doc_id)
                    row = conn.execute(
                        "SELECT path, title, content FROM docs_fts "
                        "WHERE path LIKE ? LIMIT 1",
                        (f"%/{base}",),
                    ).fetchone()
            finally:
                conn.close()
            if row is not None:
                path, title, content = row
                content = content or ""
                return {
                    "doc_id": path,
                    "found": True,
                    "source": "lexical",
                    "title": title,
                    "chars": len(content),
                    "truncated": len(content) > max_chars,
                    "content": content[:max_chars],
                }
        except sqlite3.Error as e:
            log.error("get_document lexical error: %s", e)

    # 2) fallback: метаданные официального API (без /chat)
    try:
        tok = load_token()
        r = requests.get(
            f"{config.ALM_BASE}/document/{doc_id}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=config.LIST_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json().get("document", {})
            return {
                "doc_id": doc_id,
                "found": True,
                "source": "anythingllm-metadata",
                "title": d.get("title"),
                "chars": 0,
                "truncated": False,
                "content": "",
                "metadata": d,
                "note": "AnythingLLM API returns metadata only; full text не найден в lexical.db",
            }
    except Exception as e:  # noqa: BLE001
        log.warning("get_document api fallback failed: %s", e)

    return {"doc_id": doc_id, "found": False, "error": "document not found"}
