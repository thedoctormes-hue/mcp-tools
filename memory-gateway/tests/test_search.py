"""Юнит-тесты memory-gateway. Изолированы от сети и реального AnythingLLM.

Проверяют чистую логику: RRF-слияние, дедуп, валидацию, lexical (на temp FTS5),
и деградацию при недоступном слое. Векторный слой мокается.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_gateway import config, search  # noqa: E402


# ── RRF / дедуп ─────────────────────────────────────────────────────────
def test_rrf_merge_dedup_by_basename():
    vector = [{"source": "vector", "workspace": "lab-memory", "title": "CHANGELOG.md",
               "doc_id": "CHANGELOG.md", "text": "vec text", "vector_score": 0.9}]
    lexical = [{"source": "lexical", "workspace": None, "title": "CHANGELOG.md",
                "doc_id": "projects/lab-memory/CHANGELOG.md", "text": "lex snippet longer text",
                "lexical_score": 3.2}]
    merged = search.rrf_merge(vector, lexical, top_k=5)
    assert len(merged) == 1  # дедуп по basename
    m = merged[0]
    assert set(m["sources"]) == {"vector", "lexical"}
    assert m["doc_id"] == "projects/lab-memory/CHANGELOG.md"  # предпочли путь
    assert m["text"] == "lex snippet longer text"             # более полный текст
    assert m["vector_score"] == 0.9 and m["lexical_score"] == 3.2
    assert m["rrf_score"] > 0


def test_rrf_ranking_order():
    vector = [
        {"source": "vector", "doc_id": "a.md", "title": "a", "text": "", "vector_score": 0.5},
        {"source": "vector", "doc_id": "b.md", "title": "b", "text": "", "vector_score": 0.4},
    ]
    lexical = [
        {"source": "lexical", "doc_id": "b.md", "title": "b", "text": "", "lexical_score": 9},
    ]
    merged = search.rrf_merge(vector, lexical, top_k=5)
    # b.md встречается в обоих списках -> выше по RRF
    assert merged[0]["doc_id"] == "b.md"


# ── Валидация запроса ───────────────────────────────────────────────────
def test_hybrid_empty_query():
    out = search.hybrid_search("   ", top_k=5)
    assert out["count"] == 0
    assert out["error"] == "empty query"


# ── Lexical на временном FTS5 ───────────────────────────────────────────
@pytest.fixture()
def temp_lexical(tmp_path, monkeypatch):
    db = tmp_path / "lexical.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts5(content, path UNINDEXED, title UNINDEXED)"
    )
    conn.execute(
        "INSERT INTO docs_fts (content, path, title) VALUES (?,?,?)",
        ("инцидент дедлок ThreadPoolExecutor исправлен", "projects/x/INC-050.md", "INC-050"),
    )
    conn.execute(
        "INSERT INTO docs_fts (content, path, title) VALUES (?,?,?)",
        ("обычный документ про погоду", "projects/y/weather.md", "weather"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "LEXICAL_DB", str(db))
    monkeypatch.setattr(config, "LEXICAL_MIN_SCORE", 0.0)  # фикстуры BM25 < 1.0
    return str(db)


def test_lexical_search_hit(temp_lexical):
    hits = search.lexical_search("дедлок", top_k=5)
    assert hits
    assert hits[0]["doc_id"] == "projects/x/INC-050.md"
    assert hits[0]["source"] == "lexical"


def test_lexical_search_no_match(temp_lexical):
    hits = search.lexical_search("квантовая криптография блокчейн", top_k=5)
    assert hits == []


def test_get_document_from_lexical(temp_lexical):
    doc = search.get_document("projects/x/INC-050.md")
    assert doc["found"] is True
    assert doc["source"] == "lexical"
    assert "дедлок" in doc["content"]


def test_get_document_by_basename(temp_lexical):
    doc = search.get_document("INC-050.md")
    assert doc["found"] is True
    assert doc["doc_id"] == "projects/x/INC-050.md"


def test_get_document_empty():
    doc = search.get_document("")
    assert doc["found"] is False


def test_clean_text_strips_metadata_and_prefix():
    raw = (
        'passage: <document_metadata>\n'
        'sourceDocument: foo.md\n'
        'published: 7/15/2026\n'
        '</document_metadata>\n\n'
        '# Заголовок\n\nТело документа.'
    )
    out = search._clean_text(raw)
    assert "<document_metadata>" not in out
    assert "passage:" not in out
    assert out.strip().startswith("# Заголовок")


def test_clean_text_strips_fts_ellipsis():
    raw = "…краткий фрагмент … ещё текст"
    out = search._clean_text(raw)
    assert "…" not in out          # FTS5 snippet-разделитель убран
    assert "краткий фрагмент" in out
    assert "ещё текст" in out


def test_expand_result_grows_window(temp_lexical):
    # многоабзачный документ; result.text — только первый абзац (чанк)
    content = (
        "Параграф один про дедлок и ThreadPoolExecutor.\n\n"
        "Параграф два про таймаут и backoff.\n\n"
        "Параграф три про ретрай и эмбеддинг."
    )
    conn = sqlite3.connect(temp_lexical)
    conn.execute(
        "INSERT INTO docs_fts (content, path, title) VALUES (?,?,?)",
        (content, "projects/x/expand.md", "expand"),
    )
    conn.commit()
    conn.close()
    res = {
        "doc_id": "projects/x/expand.md",
        "text": "Параграф один про дедлок и ThreadPoolExecutor.",
        "vector_score": 0.9,
    }
    search._expand_result(res)
    assert res.get("context_expanded") is True
    # окно выросло: попали соседние абзацы
    assert "Параграф два" in res["text"]
    assert "Параграф три" in res["text"]
    assert res["expanded_chars"] > res["original_chars"]


def test_expand_result_unknown_doc_stays_false(temp_lexical):
    res = {"doc_id": "projects/x/missing.md", "text": "нет такого документа", "vector_score": 0.9}
    search._expand_result(res)
    assert res.get("context_expanded") is False


# ── D1: workspace-routing (регресс на 'str' object has no attribute 'get') ──
def test_workspace_slugs_for_query_dict_map(monkeypatch):
    """workspace_map.json — dict {slug:{topics,source}}; итерация не должна падать
    и должна возвращать слаги по совпадению topics."""
    fake_map = {
        "lab-memory": {"topics": ["дедлок", "эмбеддинг"], "source": "x"},
        "weather": {"topics": ["погода"], "source": "x"},
    }
    monkeypatch.setattr(search, "_WORKSPACE_MAP", fake_map)
    slugs = search.workspace_slugs_for_query("вопрос про дедлок в очереди")
    assert slugs == ["lab-memory"]


def test_workspace_slugs_for_query_dict_no_match_falls_back(monkeypatch):
    fake_map = {"lab-memory": {"topics": ["дедлок"], "source": "x"}}
    monkeypatch.setattr(search, "_WORKSPACE_MAP", fake_map)
    monkeypatch.setattr(search, "workspace_slugs", lambda: ["all-a", "all-b"])
    slugs = search.workspace_slugs_for_query("совершенно посторонняя тема")
    assert slugs == ["all-a", "all-b"]


def test_workspace_slugs_for_query_legacy_list_map(monkeypatch):
    """Defensive: старый формат — список dict'ов."""
    fake_map = [{"slug": "lab-memory", "topics": ["дедлок"]}]
    monkeypatch.setattr(search, "_WORKSPACE_MAP", fake_map)
    slugs = search.workspace_slugs_for_query("про дедлок")
    assert slugs == ["lab-memory"]


def test_vector_search_one_skips_non_dict_result(monkeypatch):
    """Если ALM вернёт строку в results[], слой не падает, а пропускает её."""
    class FakeResp:
        status_code = 200
        def json(self):
            return {"results": [
                "битая строка вместо dict",
                {"text": "ok", "score": 0.7, "metadata": {"title": "T.md"}},
            ]}
    monkeypatch.setattr(search.requests, "post", lambda *a, **k: FakeResp())
    monkeypatch.setattr(search, "load_token", lambda: "tok")
    out = search._vector_search_one("lab-memory", "q", 5, 0.0)
    assert len(out) == 1
    assert out[0]["title"] == "T.md"
    assert out[0]["vector_score"] == 0.7


# ── Гибрид с моком векторного слоя (сеть не трогаем) ─────────────────────
def test_hybrid_degraded_when_vector_fails(temp_lexical, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vector down")
    monkeypatch.setattr(search, "vector_search", boom)
    out = search.hybrid_search("дедлок", top_k=5, fusion="rrf")
    assert out["degraded"] is True
    assert out["count"] >= 1            # lexical всё равно вернул
    assert out["layers"]["lexical"] >= 1


def test_hybrid_merges_both(temp_lexical, monkeypatch):
    def fake_vector(query, top_k, threshold, workspace=None):
        return [{"source": "vector", "workspace": "x", "title": "INC-050",
                 "doc_id": "projects/x/INC-050.md", "text": "vector ctx", "vector_score": 0.8}]
    monkeypatch.setattr(search, "vector_search", fake_vector)
    out = search.hybrid_search("дедлок", top_k=5, fusion="rrf")
    assert out["degraded"] is False
    assert out["layers"]["vector"] == 1 and out["layers"]["lexical"] >= 1
    top = out["results"][0]
    assert "vector" in top["sources"] and "lexical" in top["sources"]
