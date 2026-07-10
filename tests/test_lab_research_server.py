"""Tests for the lab-search MCP server (smoke + logic, no live gateway needed)."""
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

# Load the server module directly (it lives in ../bin relative to this file).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PATH = os.path.join(REPO, "bin", "lab-research-server.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("lab_research_server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLabSearchServer(unittest.TestCase):
    def test_module_loads_and_names_server(self):
        mod = _load_module()
        self.assertEqual(mod.mcp.name, "lab-research")

    def test_web_search_normalizes_results(self):
        mod = _load_module()
        fake = {
            "results": [
                {"title": "T1", "url": "http://a", "content": "c" * 500},
                {"title": "T2", "url": "http://b", "content": "d"},
            ]
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(fake).encode()

        with mock.patch.object(urllib_request(), "urlopen", return_value=_Resp()):
            res = mod.web_search("test query", max_results=10, engines="general")
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["results"][0]["title"], "T1")
        # content truncated to 400 chars
        self.assertEqual(len(res["results"][0]["content"]), 400)

    def test_web_search_surfaces_gateway_errors(self):
        mod = _load_module()

        class _Resp:
            def __enter__(self):
                raise RuntimeError("gateway down")

            def __exit__(self, *a):
                return False

        with mock.patch.object(urllib_request(), "urlopen", return_value=_Resp()):
            res = mod.web_search("q")
        self.assertIn("error", res)
        self.assertEqual(res["count"], 0)


def urllib_request():
    # Import lazily to avoid top-level import side effects in test collection.
    import urllib.request as u

    return u


if __name__ == "__main__":
    unittest.main()
