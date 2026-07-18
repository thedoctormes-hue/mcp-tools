"""Тесты MCP-хендлеров сервера (server.py): search_memory / get_document / gateway_health.

Покрываем счастливые пути и ветки деградации без реальных сетевых вызовов
(мокаем search.* и requests.get)."""
from unittest import mock

import pytest

import memory_gateway.server as server


@pytest.fixture(autouse=True)
def patch_search():
    """Подменяем зависимости search-слоя и сетевой вызов."""
    with mock.patch.object(server.search, "load_token", return_value="fake-token"), \
         mock.patch.object(server.search, "workspace_slugs", return_value=["ws1", "ws2"]), \
         mock.patch.object(server.search, "hybrid_search") as m_hybrid, \
         mock.patch.object(server.search, "get_document") as m_getdoc, \
         mock.patch("requests.get") as m_get:
        # по умолчанию — здоровый векторный слой
        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = {"vectorCount": 17270}
        m_get.return_value = resp
        ctx = {
            "hybrid": m_hybrid,
            "getdoc": m_getdoc,
            "get": m_get,
        }
        yield ctx


def test_gateway_health_ok():
    h = server.gateway_health()
    assert h["ok"] is True
    assert h["token"]["present"] is True
    assert h["lexical_db"]["exists"] is True
    assert h["workspaces"]["count"] == 2
    assert h["vector_layer"]["reachable"] is True
    assert h["vector_layer"]["vector_count"] == 17270
    assert h["message"].startswith("✅")
    assert "17270" in h["message"]


def test_gateway_health_vector_layer_down():
    # векторный слой недоступен -> ok=False, сообщение про деградацию
    with mock.patch("requests.get", side_effect=RuntimeError("boom")):
        h = server.gateway_health()
    assert h["ok"] is False
    assert h["vector_layer"]["reachable"] is False
    assert h["message"].startswith("⚠️")


def test_gateway_health_no_token():
    with mock.patch.object(server.search, "load_token", side_effect=RuntimeError("no token")):
        h = server.gateway_health()
    assert h["ok"] is False
    assert h["token"]["present"] is False
    assert h["message"].startswith("⚠️")


def test_search_memory_ok():
    server.search.hybrid_search.return_value = {
        "query": "q", "count": 1, "results": [{"doc_id": "x"}],
        "degraded": False,
    }
    out = server.search_memory("q", top_k=3, workspace="ws1")
    assert out["count"] == 1
    assert out["degraded"] is False
    assert "latency_ms" in out
    server.search.hybrid_search.assert_called_once()


def test_search_memory_error_isolated():
    server.search.hybrid_search.side_effect = RuntimeError("embed down")
    out = server.search_memory("q")
    assert out["degraded"] is True
    assert out["count"] == 0
    assert "error" in out


def test_get_document_ok():
    server.search.get_document.return_value = {
        "doc_id": "x", "found": True, "content": "hello",
    }
    out = server.get_document("x", max_chars=10)
    assert out["found"] is True
    assert out["content"] == "hello"
    server.search.get_document.assert_called_once()


def test_get_document_error_isolated():
    server.search.get_document.side_effect = RuntimeError("missing")
    out = server.get_document("x")
    assert out["found"] is False
    assert "error" in out
