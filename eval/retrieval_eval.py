#!/usr/bin/env python3
"""Mesure le recall@k du retrieval sur les jeux d'évaluation.

Deux jeux complémentaires :

- `retrieval_eval.jsonl` (J1) : 16 requêtes dérivées des incidents
  capturés, écrites avant toute indexation. Elles proviennent des logs et
  contiennent les identifiants exacts du bac à sable
  (`db_pool_timeout`, `process_memory_rss_mb`). La tâche est donc
  lexicale avant d'être sémantique.

- `manual_queries.jsonl` (J4) : requêtes rédigées à la main sur le corpus
  public, en décrivant les symptômes avec d'autres mots que le document.
  Elles mesurent ce que le premier jeu ne peut pas : retrouver un
  document sans correspondance de termes.

Les deux ensemble évitent de conclure sur un seul profil de requête.

Usage:
    uv run python eval/retrieval_eval.py
    uv run python eval/retrieval_eval.py --config dense
    uv run python eval/retrieval_eval.py --only manual --k 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_FILE = ROOT / "eval" / "retrieval_eval.jsonl"
MANUAL_FILE = ROOT / "eval" / "manual_queries.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"


def load_queries(which: str) -> list[dict]:
    queries: list[dict] = []

    if which in ("incident", "all"):
        incident = [json.loads(l) for l in EVAL_FILE.open(encoding="utf-8")]
        queries.extend(incident)
        print(f"  {len(incident)} requêtes dérivées des incidents")

    if which in ("manual", "all"):
        if MANUAL_FILE.exists():
            manual = [json.loads(l) for l in MANUAL_FILE.open(encoding="utf-8")]
            queries.extend(manual)
            print(f"  {len(manual)} requêtes annotées à la main")
        elif which == "manual":
            raise SystemExit(f"absent : {MANUAL_FILE}")

    return queries


def evaluate(search_fn, queries: list[dict], k: int, label: str) -> dict:
    hits, ranks, latencies = 0, [], []
    per_scenario: dict[str, list[bool]] = {}
    per_origin: dict[str, list[bool]] = {}
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
            # que le simple fait qu'il se soit trompé.
            misses.append({
                "query_id": q["query_id"],
                "expected": sorted(expected),
                "got": found_docs[:3],
            })

        per_scenario.setdefault(q.get("scenario", "manual"), []).append(rank is not None)
        per_origin.setdefault(q.get("origin", "incident"), []).append(rank is not None)

    recall = hits / len(queries)
    # MRR : à recall égal, un système qui place le bon document en
    # première position vaut mieux qu'un système qui le place en
    # cinquième. Le reranker de J5 se juge surtout là-dessus.
    mrr = sum(1 / r for r in ranks) / len(queries) if ranks else 0.0
    sorted_lat = sorted(latencies)
    p95_idx = min(int(len(sorted_lat) * 0.95), len(sorted_lat) - 1)

    return {
        "config": label,
        "k": k,
        "queries": len(queries),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "mean_rank_when_found": round(statistics.mean(ranks), 2) if ranks else None,
        "latency_ms_mean": round(statistics.mean(latencies) * 1000, 1),
        "latency_ms_p95": round(sorted_lat[p95_idx] * 1000, 1),
        "per_scenario": {
            s: round(sum(v) / len(v), 3) for s, v in sorted(per_scenario.items())
        },
        "per_origin": {
            o: round(sum(v) / len(v), 3) for o, v in sorted(per_origin.items())
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

    if len(result["per_origin"]) > 1:
        print("\n  par type de requête :")
        for origin, score in result["per_origin"].items():
            print(f"    {origin:<18} {score:>5.0%}")

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


def compare(results: list[dict]) -> None:
    """Tableau comparatif et détail par scénario.

    Le recall global masque les compensations : une configuration peut
    gagner sur un scénario et perdre sur un autre à somme nulle. Le détail
    est ce qui dit si un ajout apporte vraiment quelque chose.
    """
    print(f"\n=== comparatif (k={results[0]['k']}) ===\n")
    print(f"  {'config':<16} {'recall':>8} {'MRR':>8} {'latence':>11}")
    for r in results:
        print(f"  {r['config']:<16} {r['recall_at_k']:>7.1%} "
              f"{r['mrr']:>8.4f} {r['latency_ms_mean']:>8.0f} ms")

    origins = sorted({o for r in results for o in r["per_origin"]})
    if len(origins) > 1:
        print(f"\n  {'type de requête':<18}", end="")
        for r in results:
            print(f"{r['config'][:12]:>14}", end="")
        print()
        for origin in origins:
            print(f"  {origin:<18}", end="")
            for r in results:
                print(f"{r['per_origin'].get(origin, 0):>13.0%} ", end="")
            print()

    scenarios = sorted({s for r in results for s in r["per_scenario"]})
    print(f"\n  {'scénario':<18}", end="")
    for r in results:
        print(f"{r['config'][:12]:>14}", end="")
    print()
    for scenario in scenarios:
        print(f"  {scenario:<18}", end="")
        for r in results:
            print(f"{r['per_scenario'].get(scenario, 0):>13.0%} ", end="")
        print()


def build_configs(selected: str) -> dict:
    """Les imports sont faits ici plutôt qu'en tête de fichier : charger
    DenseIndex instancie le modèle d'embedding, ce qui prend plusieurs
    secondes même quand on ne mesure que BM25."""
    configs = {}

    if selected in ("dense", "all"):
        from aegisops.rag.dense import DenseIndex
        configs["dense seul"] = DenseIndex().search

    if selected in ("sparse", "all"):
        from aegisops.rag.sparse import SparseIndex
        sparse = SparseIndex()
        sparse.load()
        configs["BM25 seul"] = sparse.search

    if selected in ("hybrid", "all"):
        from aegisops.rag.hybrid import HybridIndex
        hybrid = HybridIndex()
        weights = "/".join(f"{k}:{v}" for k, v in hybrid.weights.items())
        configs[f"hybride RRF"] = hybrid.search
        print(f"  poids de fusion : {weights}")

    return configs


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--config", choices=["dense", "sparse", "hybrid", "all"],
                   default="all")
    p.add_argument("--only", choices=["incident", "manual", "all"],
                   default="all", help="quel jeu de requêtes évaluer")
    args = p.parse_args()

    print("jeux d'évaluation :")
    queries = load_queries(args.only)
    if not queries:
        raise SystemExit("aucune requête")
    print(f"  total : {len(queries)}\n")

    configs = build_configs(args.config)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for label, fn in configs.items():
        result = evaluate(fn, queries, args.k, label)
        report(result)
        results.append(result)
        slug = label.split()[0].lower()
        suffix = "" if args.only == "all" else f"_{args.only}"
        (RESULTS_DIR / f"{slug}{suffix}_k{args.k}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False)
        )

    if len(results) > 1:
        compare(results)


if __name__ == "__main__":
    main()