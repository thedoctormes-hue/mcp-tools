"""Sprint D (ideal): vector-response cache.

Проверяет, что повторный vector_search с теми же аргументами не бьёт ALM
повторно (кэш по (query, top_k, threshold, workspace) с TTL).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_gateway import search


def test_vector_cache_avoids_second_alm_call(monkeypatch):
    calls = {"n": 0}

    def fake_one(slug, query, top_k, threshold):
        calls["n"] += 1
        return [{"source": "vector", "doc_id": f"{slug}/x.md",
                 "text": "t", "vector_score": 0.9}]

    monkeypatch.setattr(search, "_vector_search_one", fake_one)
    with search._VECTOR_CACHE_LOCK:
        search._VECTOR_CACHE.clear()

    r1 = search.vector_search("q", 5, 0.0, workspace="lab")
    r2 = search.vector_search("q", 5, 0.0, workspace="lab")
    # второй раз — из кэша, ALM не дёргаем
    assert calls["n"] == 1
    assert len(r1) == 1 and len(r2) == 1
    assert r1[0]["doc_id"] == "lab/x.md"


def test_vector_cache_key_includes_top_k(monkeypatch):
    calls = {"n": 0}

    def fake_one(slug, query, top_k, threshold):
        calls["n"] += 1
        return [{"source": "vector", "doc_id": f"{slug}/x.md",
                 "text": "t", "vector_score": 0.9}]

    monkeypatch.setattr(search, "_vector_search_one", fake_one)
    with search._VECTOR_CACHE_LOCK:
        search._VECTOR_CACHE.clear()

    search.vector_search("q", 5, 0.0, workspace="lab")
    search.vector_search("q", 10, 0.0, workspace="lab")  # другой top_k
    # разные top_k -> разные ключи -> два удара по ALM
    assert calls["n"] == 2
