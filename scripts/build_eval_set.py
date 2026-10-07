
#!/usr/bin/env python3
"""Construit le jeu d'évaluation du retrieval.

Écrit AVANT toute indexation, délibérément. Annoter la pertinence après
avoir vu les résultats du moteur reviendrait à valider ce qu'il renvoie
plutôt qu'à mesurer ce qu'il devrait renvoyer.

Les requêtes sont dérivées des 16 incidents capturés. Chacune est
construite comme le fera l'Evidence Collector en semaine 4 : symptômes et
métriques résumés, jamais une question en langue naturelle. Le document
attendu est le postmortem du même scénario — l'étiquetage est donc
automatique et non sujet à interprétation.

Usage:
    uv run python scripts/build_eval_set.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"
EVAL_DIR = ROOT / "eval"

# Ces événements nomment la cause ou l'injection : les laisser dans la
# requête donnerait la réponse au moteur de recherche. BM25 retrouverait
# le bon document par simple correspondance lexicale, et la mesure
# n'aurait plus de sens.
LEAKY_EVENTS = {"fault_injected", "fault_cleared", "connection_leaked", "deployment"}


def summarize_metrics(record: dict) -> list[str]:
    """Réduit les métriques à des écarts baseline → incident.

    C'est la forme que l'agent produira : une comparaison, pas une valeur
    isolée dont il faudrait deviner si elle est anormale.
    """
    lines = []
    m = record["metrics"]

    baseline_deps = {
        s["labels"].get("dependency"): s["peak"]
        for s in m["p95_by_dependency"].get("baseline", [])
        if isinstance(s, dict)
    }
    for s in m["p95_by_dependency"].get("incident", []):
        if not isinstance(s, dict):
            continue
        dep = s["labels"].get("dependency", "?")
        base = baseline_deps.get(dep)
        if base is not None and s.get("peak") is not None:
            lines.append(f"p95 {dep}: {base:.3f}s -> {s['peak']:.3f}s")

    for key, label in (
        ("db_pool_usage", "db pool usage"),
        ("memory_mb", "process memory MB"),
        ("stock_total", "inventory stock total"),
    ):
        series = m.get(key, {})
        base = series.get("baseline") or []
        inc = series.get("incident") or []
        if base and inc and isinstance(base[0], dict) and isinstance(inc[0], dict):
            lines.append(f"{label}: {base[0]['peak']} -> {inc[0]['peak']}")

    return lines


def clean_counts(counts: dict) -> dict:
    return {k: v for k, v in counts.items() if k not in LEAKY_EVENTS}


def build_query(record: dict) -> str:
    """Assemble une requête à partir d'un incident, sans nommer la cause."""
    logs = record["logs"]["checkout-service"]
    parts = [
        f"Incident observed on {record['injected_service']}.",
        f"Baseline events: {clean_counts(logs['baseline']['counts'])}",
        f"Incident events: {clean_counts(logs['incident']['counts'])}",
        f"After the cause was removed: {clean_counts(logs['recovery']['counts'])}",
    ]
    parts.extend(summarize_metrics(record))
    return "\n".join(parts)


def main() -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(INCIDENTS.glob("INC-*.json"))
    if not files:
        print("aucun incident — lancer la campagne d'abord")
        return

    queries = []
    for path in files:
        record = json.loads(path.read_text())
        scenario = record["scenario"]
        queries.append({
            "query_id": f"Q-{record['incident_id']}",
            "origin": "incident",
            "scenario": scenario,
            "holdout": record["holdout"],
            "rps": record.get("load", {}).get("rps"),
            "query": build_query(record),
            # Étiquetage automatique : le document attendu est le
            # postmortem du scénario injecté.
            "relevant_docs": [f"sandbox/{scenario}.md"],
        })

    out = EVAL_DIR / "retrieval_eval.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for q in queries:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    print(f"{len(queries)} requêtes dérivées des incidents")
    by_scenario: dict[str, int] = {}
    for q in queries:
        by_scenario[q["scenario"]] = by_scenario.get(q["scenario"], 0) + 1
    for scenario, n in sorted(by_scenario.items()):
        print(f"  {scenario:<18} {n}")

    print(f"\nécrit : {out}")
    print("\n--- exemple de requête ---\n")
    print(queries[0]["query"])
    print(f"\n  document attendu : {queries[0]['relevant_docs']}")

    # Contrôle : aucun terme de la requête ne doit nommer la cause.
    suspects = ("leak", "saturation", "deployment", "corruption", "release")
    flagged = [
        q["query_id"] for q in queries
        if any(s in q["query"].lower() for s in suspects)
    ]
    if flagged:
        print(f"\nATTENTION : {len(flagged)} requête(s) contiennent un terme "
              f"nommant la cause :")
        for qid in flagged:
            print(f"  {qid}")


if __name__ == "__main__":
    main()