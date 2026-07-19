"""Тесты searxng-gateway — unit + integration."""
from unittest.mock import MagicMock, patch

import pytest


# ── Unit tests (без реального SearXNG) ──────────────────────────────

class TestSearchWeb:
    """Тесты search_web с мокнутым HTTP."""

    def _mock_response(self, status=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data or {
            "query": "test",
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                    "content": "Example content",
                    "engine": "google",
                    "engines": ["google", "bing"],
                    "score": 1.5,
                    "category": "general",
                }
            ],
            "answers": [],
            "infoboxes": [],
            "suggestions": [],
            "unresponsive_engines": [],
        }
        resp.raise_for_status = MagicMock()
        return resp

    @patch("searxng_gateway.server._requests.get")
    def test_search_web_basic(self, mock_get):
        mock_get.return_value = self._mock_response()
        from searxng_gateway.server import search_web

        result = search_web("test query")
        assert result["query"] == "test query"
        assert result["count"] == 1
        assert result["results"][0]["title"] == "Example"
        assert result["results"][0]["url"] == "https://example.com"
        assert "latency_ms" in result

    @patch("searxng_gateway.server._requests.get")
    def test_search_web_max_results(self, mock_get):
        many_results = [
            {"title": f"Result {i}", "url": f"https://{i}.com", "content": "...",
             "engine": "google", "engines": ["google"], "score": 1.0, "category": "general"}
            for i in range(20)
        ]
        resp = self._mock_response(json_data={
            "query": "test", "results": many_results,
            "answers": [], "infoboxes": [], "suggestions": [], "unresponsive_engines": [],
        })
        mock_get.return_value = resp
        from searxng_gateway.server import search_web

        result = search_web("test", max_results=5)
        assert result["count"] == 5

    @patch("searxng_gateway.server._requests.get")
    def test_search_web_empty(self, mock_get):
        resp = self._mock_response(json_data={
            "query": "xyznonexistent", "results": [],
            "answers": [], "infoboxes": [], "suggestions": [], "unresponsive_engines": [],
        })
        mock_get.return_value = resp
        from searxng_gateway.server import search_web

        result = search_web("xyznonexistent")
        assert result["count"] == 0
        assert result["results"] == []

    @patch("searxng_gateway.server._requests.get")
    def test_search_web_error_returns_degraded(self, mock_get):
        mock_get.side_effect = ConnectionError("refused")
        from searxng_gateway.server import search_web

        result = search_web("test")
        assert result["degraded"] is True
        assert result["count"] == 0
        assert "ConnectionError" in result["error"]

    @patch("searxng_gateway.server._requests.get")
    def test_search_web_published_date(self, mock_get):
        resp = self._mock_response(json_data={
            "query": "test",
            "results": [{
                "title": "Dated", "url": "https://dated.com", "content": "...",
                "engine": "google", "engines": ["google"], "score": 1.0,
                "category": "general", "publishedDate": "2026-01-15",
            }],
            "answers": [], "infoboxes": [], "suggestions": [], "unresponsive_engines": [],
        })
        mock_get.return_value = resp
        from searxng_gateway.server import search_web

        result = search_web("test")
        assert result["results"][0]["published_date"] == "2026-01-15"


class TestSearxngHealth:
    """Тесты searxng_health с мокнутым HTTP."""

    @patch("searxng_gateway.server._requests.get")
    def test_health_ok(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "results": [{"title": "ok"}],
            "unresponsive_engines": [],
        }
        mock_get.return_value = resp

        from searxng_gateway.server import searxng_health
        result = searxng_health()
        assert result["status"] == "ok"
        assert result["reachable"] is True

    @patch("searxng_gateway.server._requests.get")
    def test_health_down(self, mock_get):
        mock_get.side_effect = ConnectionError("refused")
        from searxng_gateway.server import searxng_health

        result = searxng_health()
        assert result["status"] == "down"
        assert result["reachable"] is False


# ── Integration test (реальный SearXNG) ────────────────────────────

@pytest.mark.integration
class TestLiveSearxng:
    """Интеграционные тесты — требуют работающий SearXNG на localhost:8889."""

    @patch("searxng_gateway.server.config.SEARXNG_URL", "http://localhost:8889")
    def test_live_search(self):
        import requests
        try:
            r = requests.get("http://localhost:8889/search", params={"q": "test", "format": "json"}, timeout=3)
            if r.status_code != 200:
                pytest.skip("SearXNG not available")
        except Exception:
            pytest.skip("SearXNG not available")

        from searxng_gateway.server import search_web
        result = search_web("python programming")
        assert result["count"] > 0
        assert result["latency_ms"] > 0
        assert result["results"][0]["url"].startswith("http")

    @patch("searxng_gateway.server.config.SEARXNG_URL", "http://localhost:8889")
    def test_live_health(self):
        import requests
        try:
            r = requests.get("http://localhost:8889/search", params={"q": "test", "format": "json"}, timeout=3)
            if r.status_code != 200:
                pytest.skip("SearXNG not available")
        except Exception:
            pytest.skip("SearXNG not available")

        from searxng_gateway.server import searxng_health
        result = searxng_health()
        assert result["reachable"] is True


class TestEnginesParam:
    """engines-фильтр (distilled из lab-research)."""

    @patch("searxng_gateway.server._requests.get")
    def test_engines_passed_to_params(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "results": [], "answers": [], "infoboxes": [],
            "suggestions": [], "unresponsive_engines": [],
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        from searxng_gateway.server import search_web

        search_web("test", engines="google,bing")
        _, kwargs = mock_get.call_args
        assert "engines" in kwargs["params"]
        assert kwargs["params"]["engines"] == "google,bing"


class TestDeepResearch:
    """deep_research тул (distilled из lab-research, adapted)."""

    @patch("searxng_gateway.server.subprocess.run")
    @patch("searxng_gateway.server.os.path.exists", return_value=True)
    @patch("searxng_gateway.server.config.DEEP_RESEARCH_ORCHESTRATOR", "/fake/orchestrator.sh")
    def test_deep_research_ok(self, mock_exists, mock_run):
        proc = MagicMock()
        proc.stdout = "Synthesized answer here"
        proc.stderr = ""
        proc.returncode = 0
        mock_run.return_value = proc
        from searxng_gateway.server import deep_research

        result = deep_research("test query", 5)
        assert result["answer"] == "Synthesized answer here"
        assert result["degraded"] is False
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["/fake/orchestrator.sh", "test query", "deep_research", "5"]

    @patch("searxng_gateway.server.subprocess.run")
    @patch("searxng_gateway.server.os.path.exists", return_value=False)
    @patch("searxng_gateway.server.config.DEEP_RESEARCH_ORCHESTRATOR", "/fake/orchestrator.sh")
    def test_deep_research_missing_orchestrator(self, mock_exists, mock_run):
        from searxng_gateway.server import deep_research
        result = deep_research("test")
        assert result["degraded"] is True
        assert "not configured" in result["error"]
        mock_run.assert_not_called()

    @patch("searxng_gateway.server.subprocess.run")
    @patch("searxng_gateway.server.os.path.exists", return_value=True)
    @patch("searxng_gateway.server.config.DEEP_RESEARCH_ORCHESTRATOR", "/fake/orchestrator.sh")
    def test_deep_research_error_degraded(self, mock_exists, mock_run):
        mock_run.side_effect = TimeoutError("orchestrator hung")
        from searxng_gateway.server import deep_research
        result = deep_research("test")
        assert result["degraded"] is True
        assert "TimeoutError" in result["error"]
