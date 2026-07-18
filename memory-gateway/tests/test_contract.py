"""P5-1: Contract-test ALM vector-search API.

Проверяет, что memory-gateway держит контракт AnythingLLM
(``/api/v1/workspace/:slug/vector-search``):
  - схема успешного ответа (results[].{id,text,metadata.url,score});
  - корректная деградация при 401 / 500 (не падает, возвращает []);
  - live-probe к реальному ALM (помечен @pytest.mark.live, скипается без сети).

Цель: будущий апгрейд AnythingLLM не сломает семпамять молча — тест
упадёт, если схема ответа изменится.
"""
import json
import threading
import http.server
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_gateway import config, search

# Эталонная схема ответа ALM (снята живьём 2026-07-18, v1.15.0)
GOOD_RESPONSE = {
    "results": [
        {
            "id": "a1ff5608-0000-0000-0000-000000000001",
            "text": (
                "passage: <document_metadata>\n"
                "sourceDocument: SKILL-TEMPLATE.md\n"
                "</document_metadata>\n\n"
                "## Границы применимости\nтекст документа"
            ),
            "metadata": {
                "url": "file:///app/collector/hotdir/SKILL-TEMPLATE.md",
                "title": "SKILL-TEMPLATE.md",
                "author": "Unknown",
                "description": "Unknown",
            },
            "score": 0.85,
        }
    ]
}


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    body = json.dumps(GOOD_RESPONSE).encode("utf-8")

    def do_POST(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


def _start_server(status=200, body=None):
    attrs = {"status": status}
    if body is not None:
        attrs["body"] = body
    handler = type("H", (_Handler,), attrs)
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_contract_good_schema(monkeypatch):
    srv, port = _start_server()
    monkeypatch.setattr(config, "ALM_BASE", f"http://127.0.0.1:{port}/api/v1")
    try:
        results = search._vector_search_one("test", "paperless", 5, 0.0)
    finally:
        srv.shutdown()
    assert len(results) == 1
    r = results[0]
    assert r["source"] == "vector"
    assert r["vector_score"] == 0.85
    # doc_id — полный путь из metadata.url (file:// префикс срезается в search.py)
    assert r["doc_id"] == "app/collector/hotdir/SKILL-TEMPLATE.md"
    assert "Границы" in r["text"]


def test_contract_401_returns_empty(monkeypatch):
    srv, port = _start_server(status=401, body=b'{"error":"unauthorized"}')
    monkeypatch.setattr(config, "ALM_BASE", f"http://127.0.0.1:{port}/api/v1")
    try:
        results = search._vector_search_one("test", "q", 5, 0.0)
    finally:
        srv.shutdown()
    # не падает, возвращает пусто (деградация слойя)
    assert results == []


def test_contract_500_returns_empty(monkeypatch):
    srv, port = _start_server(status=500, body=b'{"error":"boom"}')
    monkeypatch.setattr(config, "ALM_BASE", f"http://127.0.0.1:{port}/api/v1")
    try:
        results = search._vector_search_one("test", "q", 5, 0.0)
    finally:
        srv.shutdown()
    assert results == []


@pytest.mark.live
def test_contract_live_probe():
    """Живой probe к реальному ALM. Скипается, если недоступен."""
    tok = search.load_token()
    if not tok:
        pytest.skip("no ALM token available")
    import requests

    try:
        r = requests.post(
            f"{config.ALM_BASE}/workspace/skills-canon/vector-search",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={"query": "paperless", "topN": 1, "scoreThreshold": 0.0},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"ALM unreachable: {e}")
    if r.status_code != 200:
        pytest.skip(f"ALM returned HTTP {r.status_code}")
    data = r.json()
    assert "results" in data
    if data["results"]:
        first = data["results"][0]
        # контракт: score + metadata.url обязательны
        assert "score" in first
        assert "metadata" in first and "url" in first["metadata"]
