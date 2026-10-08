#!/usr/bin/env python3
"""Mesure le recall@k du retrieval sur le jeu d'évaluation de J1.

Le jeu a été écrit avant toute indexation, délibérément : annoter la
pertinence après avoir vu les résultats reviendrait à valider ce que le
moteur renvoie plutôt qu'à mesurer ce qu'il devrait renvoyer.

Usage:
    uv run python eval/retrieval_eval.py
    uv run python eval/retrieval_eval.py --k 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from aegisops.rag.dense import DenseIndex

ROOT = Path(__file__).resolve().parents[1]
EVAL_FILE = ROOT / "eval" / "retrieval_eval.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"


def evaluate(search_fn, queries: list[dict], k: int, label: str) -> dict:
    hits, ranks, latencies = 0, [], []
    per_scenario: dict[str, list[bool]] = {}
    misses: list[dict] = []

    for q in queries:
        start = time.perf_counter()
        results = search_fn(q["query"], k)
        latencies.append(time.perf_counter() - start)

        expected = set(q["relevant_docs"])
        found_docs = [r.doc_id for r in results]

        # Recall@k binaire : le document attendu est-il dans les k
        # premiers ? Avec un seul document pertinent par requête, c'est
        # équivalent au hit rate — et plus lisible qu'un recall moyenné.
        rank = next((i + 1 for i, d in enumerate(found_docs) if d in expected), None)
        if rank:
            hits += 1
            ranks.append(rank)
        else:
            # Ce que le moteur a renvoyé à la place est plus instructif
            # que le fait qu'il se soit trompé.
            misses.append({
                "query_id": q["query_id"],
                "expected": sorted(expected),
                "got": found_docs[:3],
            })

        per_scenario.setdefault(q["scenario"], []).append(rank is not None)

    recall = hits / len(queries)
    # MRR : à recall égal, un système qui place le bon document en
    # première position vaut mieux qu'un système qui le place en
    # cinquième. Le reranker de J5 se juge surtout là-dessus.
    mrr = sum(1 / r for r in ranks) / len(queries) if ranks else 0.0
    sorted_lat = sorted(latencies)

    return {
        "config": label,
        "k": k,
        "queries": len(queries),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "mean_rank_when_found": round(statistics.mean(ranks), 2) if ranks else None,
        "latency_ms_mean": round(statistics.mean(latencies) * 1000, 1),
        "latency_ms_p95": round(sorted_lat[min(int(len(sorted_lat) * 0.95),
                                               len(sorted_lat) - 1)] * 1000, 1),
        "per_scenario": {
            s: round(sum(v) / len(v), 3) for s, v in sorted(per_scenario.items())
        },
        "misses": misses,
    }


def report(result: dict) -> None:
    print(f"\n=== {result['config']} (k={result['k']}) ===")
    print(f"  recall@{result['k']} : {result['recall_at_k']:.1%}")
    print(f"  MRR            : {result['mrr']:.4f}")
    print(f"  rang moyen     : {result['mean_rank_when_found']}")
    print(f"  latence        : {result['latency_ms_mean']} ms  "
          f"(p95 {result['latency_ms_p95']} ms)")

    print("\n  par scénario :")
    for scenario, score in result["per_scenario"].items():
        bar = "#" * int(score * 20)
        print(f"    {scenario:<18} {score:>5.0%}  {bar}")

    if result["misses"]:
        print(f"\n  échecs ({len(result['misses'])}) :")
        for m in result["misses"]:
            print(f"    {m['query_id']}")
            print(f"      attendu : {m['expected'][0]}")
            print(f"      obtenu  : {', '.join(m['got'])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--k", type=int, default=5)
    args = p.parse_args()

    queries = [json.loads(line) for line in EVAL_FILE.open(encoding="utf-8")]
    print(f"{len(queries)} requêtes d'évaluation")

    index = DenseIndex()
    result = evaluate(index.search, queries, args.k, "dense seul")
    report(result)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"dense_k{args.k}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nécrit : {out}")


if __name__ == "__main__":
    main()