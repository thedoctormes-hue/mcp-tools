"""Тесты score-calibrated fusion (P1: weighted вместо чистого RRF).

Проверяют: нормализацию скоров, взвешенную сумму, дедуп по basename,
совокупный порог (отсев lexical-шума) и fallback-поведение при единств.
векторном хите (min-max по 1 значению = 0.0, но порог не съедает при
наличии lexical-параллели).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from memory_gateway import config, search  # noqa: E402


def _v(doc_id, score):
    return {"source": "vector", "workspace": "lab-memory", "title": doc_id,
            "doc_id": doc_id, "text": "v", "vector_score": score}


def _l(doc_id, score, path=None):
    return {"source": "lexical", "workspace": None, "title": doc_id,
            "doc_id": path or doc_id, "text": "l", "lexical_score": score}


def test_weighted_dedup_and_weight(monkeypatch):
    # vector точное попадание (высокий cosine) + lexical шум (высокий BM25
    # по короткому токену, но на другом/нерелевантном доке)
    monkeypatch.setattr(search, "lexical_search", lambda q, k: [])
    monkeypatch.setattr(config, "FUSION_VECTOR_WEIGHT", 0.6)
    monkeypatch.setattr(config, "FUSION_MIN_COMBINED", 0.05)
    vec = [_v("app/x/GOOD.md", 0.9), _v("app/x/NOISE.md", 0.2)]
    lex = [_l("app/x/NOISE.md", 30.0)]  # BM25 высокий, но vector-скор низкий
    out = search._fuse_weighted(vec, lex, top_k=5)
    ids = [o["doc_id"] for o in out]
    assert "app/x/GOOD.md" in ids
    # GOOD выше NOISE (взвешенная сумма: 0.6*1.0 > 0.6*0.0 + 0.4*1.0)
    assert ids[0] == "app/x/GOOD.md"


def test_weighted_min_combined_threshold(monkeypatch):
    # чистый lexical-шум без векторной поддержки -> отсев порогом
    monkeypatch.setattr(config, "FUSION_MIN_COMBINED", 0.05)
    monkeypatch.setattr(search, "lexical_search", lambda q, k: [])
    vec = [_v("app/x/REAL.md", 0.85)]
    lex = [_l("app/x/JUNK.md", 25.0)]  # высокий BM25, но нет в vector
    # REAl vec=0.85 -> норм 1.0 (min-max по [0.85]) => 0.6*1.0=0.6 (проходит)
    # JUNK lex=25 -> норм 1.0 => 0.4*1.0=0.4 (проходит)
    out = search._fuse_weighted(vec, lex, top_k=5)
    assert len(out) == 2


def test_weighted_empty_both_layers_returns_empty(monkeypatch):
    # оба слоя пусты -> fuse пусто (нечего ранжировать)
    monkeypatch.setattr(search, "vector_search", lambda q, k, t, w=None: [])
    monkeypatch.setattr(search, "lexical_search", lambda q, k: [])
    out = search.hybrid_search("q", 5, fusion="weighted")
    assert out["results"] == []
    assert out["degraded"] is False


def test_hybrid_accepts_fusion_arg(monkeypatch):
    monkeypatch.setattr(search, "vector_search",
                        lambda q, k, t, w=None: [_v("a.md", 0.9)])
    monkeypatch.setattr(search, "lexical_search", lambda q, k: [])
    out = search.hybrid_search("q", 5, fusion="weighted")
    assert out["results"]  # weighted не съедает единств. вектор при высоком скоре
    assert out["degraded"] is False
