#!/usr/bin/env python3
"""Offline evaluation-harness для memory-gateway.

Считывает gold-сет (query -> {doc_id: graded_relevance}) и прогоняет
любой retrieval-бэкенд, реализующий `search(query, top_k) -> list[doc_id]`.
Метрики: NDCG@k, MRR, Hit@k. Бэкенд подменяется через --backend
(current_rrf | weighted) для A/B сравнения fusion-стратегий.

НЕ трогает прод: читает тот же lexical.db + (опц.) ALM, что и шлюз,
но изолированно и детерминированно.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import memory_gateway.search as search  # memory_gateway.search


def dcg(rels, k):
    s = 0.0
    for i, r in enumerate(rels[:k], start=1):
        s += (2 ** r - 1) / (i + 1)  # i+1 → 2..(k+1) (1-based rank)
    return s


def ndcg(rels, k):
    ideal = sorted(rels, reverse=True)[:k]
    idcg = dcg(ideal, k)
    return dcg(rels, k) / idcg if idcg > 0 else 0.0


def mrr(ranks):
    ranks = [r for r in ranks if r]
    return sum(1.0 / r for r in ranks) / len(ranks) if ranks else 0.0


def hit_at(rels, k, min_rel=2):
    return 1.0 if any(r >= min_rel for r in rels[:k]) else 0.0


def backends(name):
    """Возвращает функцию search(query, top_k) -> list[doc_id]."""
    if name == "current_rrf":
        def f(q, top_k):
            out = search.hybrid_search(
                q, top_k, expand_context=False, fusion="rrf"
            )
            return [r["doc_id"] for r in out["results"]]
        return f
    if name == "weighted":
        def f(q, top_k):
            # делегирует в новый score-weighted fusion (P1)
            out = search.hybrid_search(
                q, top_k, expand_context=False, fusion="weighted"
            )
            return [r["doc_id"] for r in out["results"]]
        return f
    raise SystemExit(f"unknown backend: {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=os.path.join(HERE, "gold_set.json"))
    ap.add_argument("--backend", default="current_rrf")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        gold = json.load(f)["queries"]

    fn = backends(args.backend)
    k = args.k

    ndcgs, mrrs, hits = [], [], []
    for item in gold:
        q = item["q"]
        rel = item["rel"]
        try:
            ids = fn(q, k)
        except Exception as e:  # noqa: BLE001
            print(f"  ! backend error on {q!r}: {e}")
            ids = []
        rels = [rel.get(d, 0) for d in ids]
        ranks = [i + 1 for i, d in enumerate(ids) if rel.get(d, 0) >= 2]
        ndcgs.append(ndcg(rels, k))
        mrrs.append(mrr(ranks))
        hits.append(hit_at(rels, k))

    n = len(gold)
    print(f"backend={args.backend}  k={k}  n_queries={n}")
    print(f"  NDCG@{k} = {sum(ndcgs)/n:.4f}")
    print(f"  MRR@{k}   = {sum(mrrs)/n:.4f}")
    print(f"  Hit@{k}   = {sum(hits)/n:.4f}")


if __name__ == "__main__":
    main()
