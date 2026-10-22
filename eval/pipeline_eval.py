#!/usr/bin/env python3
"""Mesure le pipeline complet sur les captures rejouables.

Trois précautions établies dans les sessions précédentes.

**La sortie du modèle n'est pas déterministe.** Mesuré en S5-J1, deux
exécutions sur le même incident ont donné 0,86 puis 0,78 de confiance,
six puis cinq preuves. Chaque incident est donc rejoué `--runs` fois et
le résultat rapporté avec sa dispersion.

**Un nœud peut échouer sans arrêter le graphe.** `timed` enregistre
l'exception dans `errors` et l'investigation continue avec ce qu'elle a.
Une mesure qui suppose que tous les nœuds ont produit leur sortie plante
sur le premier échec — et perd les résultats déjà collectés.

**Seules les captures postérieures à S4-J2 contiennent `metrics_raw`.**
Les autres ne sont pas rejouables et sont écartées ici plutôt que de
lever au milieu de la mesure.

Usage:
    uv run python eval/pipeline_eval.py
    uv run python eval/pipeline_eval.py --runs 3 --pause 2
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"
RESULTS = ROOT / "eval" / "results"

# Un scénario est « nommé » si la cause contient les termes qui le
# caractérisent. Une correspondance exacte du nom échoue sur des
# formulations pourtant correctes : le modèle écrit « regression in the
# v2.8 deployment » là où le scénario s'appelle `bad_release`, et
# « connection pool leak » pour `connection_leak`.
#
# Le critère reste grossier. Le mesurer proprement demanderait un juge —
# LLM ou humain — et le premier serait à son tour non déterministe, ce
# qui ramènerait le problème que cette mesure cherche à contourner.
SCENARIO_TERMS = {
    "connection_leak": {"all": ["connection", "leak"]},
    "memory_leak": {"all": ["memory", "leak"]},
    "slow_response": {"any": ["slow", "latenc", "timeout", "dependency"]},
    "bad_release": {"any": ["deploy", "release", "regression", "v2.8"]},
    "cpu_saturation": {"any": ["cpu", "event loop", "saturation", "block"]},
    "internal_error": {"any": ["internal error", "ledger", "corrupt"]},
}


def names_scenario(cause: str, scenario: str) -> bool:
    cause = cause.lower()
    spec = SCENARIO_TERMS.get(scenario)
    if not spec:
        return False
    if "all" in spec:
        return all(t in cause for t in spec["all"])
    return any(t in cause for t in spec["any"])


def replayable() -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(INCIDENTS.glob("INC-*.json")):
        record = json.loads(path.read_text())
        if record.get("metrics_raw"):
            out.append((path, record))
    return out


def blank_row(scenario: str, run: int, state: dict, error: str) -> dict:
    """Ligne pour une exécution dont le raisonnement a échoué.

    Comptée comme un échec plutôt qu'écartée : une mesure qui ignore ses
    exécutions ratées surestime le système qu'elle mesure.
    """
    return {
        "scenario": scenario, "run": run, "named": False, "confidence": 0.0,
        "evidence": 0, "unsupported": 0, "misquoted": 0, "steps": 0,
        "read_only": 0, "pending": 0, "denied": 0, "failed": True,
        "error": error,
        "ms": sum(s["ms"] for s in state.get("trace", []) if "ms" in s),
    }


def main() -> None:
    from aegisops.agents.graph import replay

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--pause", type=float, default=1.0,
                   help="secondes entre deux exécutions, contre les quotas")
    p.add_argument("--scenario", help="ne mesure que ce scénario")
    args = p.parse_args()

    captures = replayable()
    if args.scenario:
        captures = [(p_, r) for p_, r in captures
                    if r["scenario"] == args.scenario]

    if not captures:
        print("aucune capture rejouable — relancer une campagne")
        return

    total = len(captures) * args.runs
    print(f"{len(captures)} captures, {args.runs} exécutions chacune "
          f"({total} au total)\n")

    rows: list[dict] = []

    for path, record in captures:
        truth = record["scenario"]

        for run in range(args.runs):
            state = replay(str(path))
            hypothesis = state.get("hypothesis")

            if hypothesis is None:
                error = (state.get("errors") or ["(sans message)"])[0]
                rows.append(blank_row(truth, run, state, error))
                print(f"  {truth:<18} run {run}  ÉCHEC  {error[:70]}")
                time.sleep(args.pause)
                continue

            verification = state.get("verification")
            plan = state.get("plan")
            policy = state.get("policy")

            row = {
                "scenario": truth,
                "run": run,
                "named": names_scenario(hypothesis.root_cause, truth),
                "confidence": hypothesis.confidence,
                "evidence": len(hypothesis.evidence),
                "unsupported": verification.unsupported_count if verification else 0,
                "misquoted": verification.misquoted_count if verification else 0,
                "steps": len(plan.steps) if plan else 0,
                "read_only": len(plan.by_risk("read_only")) if plan else 0,
                "pending": len(policy.pending) if policy else 0,
                "denied": len(policy.denied) if policy else 0,
                "failed": False,
                "error": "",
                "ms": sum(s["ms"] for s in state["trace"] if "ms" in s),
            }
            rows.append(row)

            print(f"  {truth:<18} run {run}  "
                  f"nommé={'oui' if row['named'] else 'NON'}  "
                  f"conf={row['confidence']:.2f}  "
                  f"{row['ms']:.0f} ms")

            # Pause entre deux appels : les fournisseurs distants
            # appliquent des quotas par minute, et une mesure qui les
            # franchit produit des échecs qu'on prendrait pour des
            # défauts du pipeline.
            time.sleep(args.pause)

    report(rows, args.runs)


def report(rows: list[dict], runs: int) -> None:
    ok = [r for r in rows if not r["failed"]]
    failed = [r for r in rows if r["failed"]]

    print(f"\n=== synthèse ({len(rows)} exécutions) ===\n")

    if failed:
        print(f"  exécutions en échec : {len(failed)}/{len(rows)}")
        causes: dict[str, int] = defaultdict(int)
        for r in failed:
            causes[r["error"][:60]] += 1
        for cause, n in causes.items():
            print(f"    ×{n}  {cause}")
        print()

    named = sum(1 for r in rows if r["named"])
    print(f"  cause nommée        : {named}/{len(rows)} "
          f"({named / len(rows):.0%})")

    if ok:
        conf = [r["confidence"] for r in ok]
        print(f"  confiance           : {statistics.mean(conf):.2f} "
              f"± {statistics.pstdev(conf):.2f}")
        print(f"  preuves inventées   : {sum(r['unsupported'] for r in ok)}")
        print(f"  preuves mal citées  : {sum(r['misquoted'] for r in ok)}")
        steps = sum(r["steps"] for r in ok)
        print(f"  étapes lecture seule: {sum(r['read_only'] for r in ok)}/{steps}")
        print(f"  latence moyenne     : {statistics.mean(r['ms'] for r in ok):.0f} ms")

    # Le détail par scénario est ce qui dit si un taux global masque une
    # compensation : 34 % peut être « un tiers partout » ou « parfait sur
    # deux scénarios, nul sur quatre ».
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_scenario[r["scenario"]].append(r)

    print(f"\n  {'scénario':<18} {'nommé':>8} {'confiance':>11} {'échecs':>8}")
    for scenario, items in sorted(by_scenario.items()):
        hits = sum(1 for r in items if r["named"])
        good = [r for r in items if not r["failed"]]
        conf = statistics.mean(r["confidence"] for r in good) if good else 0.0
        errs = sum(1 for r in items if r["failed"])
        print(f"  {scenario:<18} {hits:>4}/{len(items):<3} "
              f"{conf:>10.2f} {errs:>8}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"pipeline_runs{runs}.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"\nécrit : {out}")


if __name__ == "__main__":
    main()