"""Тесты спайки семантической памяти в deep_research.

Проверяем контракт комбайна: один вызов deep_research возвращает и веб
(answer), и семпамять лабы (semantic_memory), с грациозной деградацией
каждого слоя. hybrid_search мокается — тест не зависит от живого ALM.
"""
from unittest import mock

from searxng_gateway import server


FAKE_SEM = {
    "query": "test",
    "count": 2,
    "results": [
        {"doc_id": "a/b.md", "title": "B", "workspace": "lab-infra",
         "text": "ctx", "sources": ["vector"], "vector_score": 0.9,
         "rrf_score": 0.5}
    ],
    "degraded": False,
    "layers": {"vector": 1, "lexical": 1},
}


def _patch_all(sem_return=FAKE_SEM, sem_available=True, web_stdout="WEB OUTPUT",
               web_rc=0):
    """Контекстный менеджер: мокает семпамять + веб-оркестратор."""
    return mock.patch.multiple(
        server,
        _mg_hybrid_search=mock.Mock(return_value=sem_return),
        _MG_AVAILABLE=sem_available,
    ), mock.patch.object(server.config, "SEMANTIC_ENABLED", True), \
       mock.patch("subprocess.run", return_value=mock.Mock(
           returncode=web_rc, stdout=web_stdout, stderr=""))


def test_deep_research_fusion_both_layers():
    p_sem, p_cfg, p_sub = _patch_all()
    with p_sem, p_cfg, p_sub:
        out = server.deep_research("test query", 10)
    assert out["query"] == "test query"
    assert out["answer"] == "WEB OUTPUT"
    assert out["semantic_memory"] == FAKE_SEM
    assert out["degraded"] is False


def test_deep_research_semantic_degraded_does_not_break_web():
    p_sem, p_cfg, p_sub = _patch_all(
        sem_return={"query": "q", "count": 0, "results": [], "degraded": True,
                    "error": "boom"})
    with p_sem, p_cfg, p_sub:
        out = server.deep_research("test query", 10)
    assert out["answer"] == "WEB OUTPUT"
    assert out["semantic_memory"]["degraded"] is True
    assert out["semantic_memory"]["error"] == "boom"
    # веб ок -> общий degraded True (из-за семпамяти)
    assert out["degraded"] is True


def test_deep_research_semantic_unavailable_marks_degraded():
    p_sem, p_cfg, p_sub = _patch_all(sem_available=False)
    with p_sem, p_cfg, p_sub:
        out = server.deep_research("test query", 10)
    assert out["answer"] == "WEB OUTPUT"
    assert out["semantic_memory"]["degraded"] is True
    assert "unavailable" in out["semantic_memory"]["error"]


def test_deep_research_web_failure_still_returns_semantic():
    p_sem, p_cfg, p_sub = _patch_all(web_stdout="", web_rc=1)
    with p_sem, p_cfg, p_sub:
        out = server.deep_research("test query", 10)
    assert out["answer"] == "No research output." or out["answer"] == ""
    assert out["semantic_memory"] == FAKE_SEM
    assert out["degraded"] is True
