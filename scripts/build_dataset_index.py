#!/usr/bin/env python3
"""Indexe les incidents capturés et vérifie leur exploitabilité.

Un incident sans phase baseline, ou dont la phase incident ne diffère pas
du fond, n'apprend rien au modèle. Mieux vaut le détecter ici qu'en
semaine 7, quand il faudrait tout recapturer.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"

# Événements qui ne sont pas des symptômes : trafic nominal et traces de
# l'injection elle-même. Les compter comme signal ferait passer pour
# « nouvelle » une panne qui n'a rien produit.
NON_SYMPTOM_EVENTS = {"order_created", "fault_injected", "fault_cleared", "deployment"}

# Deux scénarios ne produisent aucun événement nouveau : ils ne se
# détectent que par une tendance métrique. C'est leur propriété
# caractéristique, pas un défaut de capture — un agent qui cherche des
# erreurs ne les voit pas, et c'est précisément ce qu'ils doivent lui
# apprendre.
METRIC_ONLY_SCENARIOS = {"memory_leak", "cpu_saturation"}


def check(record: dict) -> list[str]:
    problems = []
    logs = record["logs"]["checkout-service"]

    baseline = logs["baseline"]["counts"]
    incident = logs["incident"]["counts"]

    if not baseline:
        problems.append("phase baseline vide")
    elif sum(baseline.values()) < 50:
        problems.append(f"baseline trop courte ({sum(baseline.values())} événements)")

    if not incident:
        problems.append("phase incident vide")

    # Une baseline contenant déjà des erreurs invalide la comparaison :
    # l'agent ne peut pas juger qu'un état est anormal sans avoir vu
    # l'état normal.
    baseline_errors = {k: v for k, v in baseline.items() if k not in NON_SYMPTOM_EVENTS}
    if baseline_errors:
        problems.append(f"baseline polluée : {baseline_errors}")

    # Un incident doit se distinguer de son fond, sauf pour les scénarios
    # qui ne se manifestent que dans les métriques.
    incident_events = {k for k in incident if k not in NON_SYMPTOM_EVENTS}
    baseline_events = set(baseline_errors)
    if (
        incident_events == baseline_events
        and record["scenario"] not in METRIC_ONLY_SCENARIOS
    ):
        problems.append("aucun événement nouveau pendant l'incident")

    return problems


def metric_signature(record: dict) -> dict:
    """Extrait les trois signaux fiables identifiés dans docs/dataset.md.

    error_rate et p95_checkout sont écartés : le premier est contaminé par
    la fenêtre de rate() en phase baseline, le second sature à 1,0 à cause
    des buckets par défaut de l'instrumentateur HTTP.
    """

    def peak(key: str, phase: str) -> float | None:
        series = record["metrics"].get(key, {}).get(phase)
        if isinstance(series, list) and series:
            return series[0].get("peak")
        return None

    deps = {}
    for s in record["metrics"]["p95_by_dependency"].get("incident", []):
        if isinstance(s, dict):
            deps[s["labels"].get("dependency", "?")] = s.get("peak")

    recovery = record["logs"]["checkout-service"]["recovery"]["counts"]
    recovered = recovery.get("order_created", 0) > 0

    return {
        "db_pool_usage_incident": peak("db_pool_usage", "incident"),
        "memory_mb_baseline": peak("memory_mb", "baseline"),
        "memory_mb_incident": peak("memory_mb", "incident"),
        "p95_by_dependency_incident": deps,
        "recovered_without_restart": recovered,
    }


def main() -> None:
    files = sorted(INCIDENTS.glob("INC-*.json"))
    if not files:
        print("aucun incident trouvé")
        return

    index, by_scenario, holdouts, flagged = [], Counter(), [], []

    for path in files:
        record = json.loads(path.read_text())
        problems = check(record)
        by_scenario[record["scenario"]] += 1
        if record["holdout"]:
            holdouts.append(record["incident_id"])
        if problems:
            flagged.append((path.name, problems))

        index.append({
            "incident_id": record["incident_id"],
            "file": path.name,
            "scenario": record["scenario"],
            "injected_service": record["injected_service"],
            "duration_s": record["window"]["duration_s"],
            "rps": record.get("load", {}).get("rps"),
            "holdout": record["holdout"],
            "usable": not problems,
            "problems": problems,
            "signature": metric_signature(record),
        })

    (INCIDENTS / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False)
    )

    print(f"{len(files)} incidents\n")
    for scenario, n in sorted(by_scenario.items()):
        seeds = sum(
            1 for i in index if i["scenario"] == scenario and not i["holdout"]
        )
        held = n - seeds
        print(f"  {scenario:<18} {n}   ({seeds} graine(s), {held} holdout)")

    print(f"\nholdout : {len(holdouts)} / {len(files)}")
    for h in holdouts:
        print(f"  {h}")

    if flagged:
        print(f"\nÀ VÉRIFIER ({len(flagged)}) :")
        for name, problems in flagged:
            print(f"  {name}")
            for p in problems:
                print(f"    - {p}")
    else:
        print("\ntous les incidents sont exploitables")

    print(f"\nindex écrit : {INCIDENTS / 'index.json'}")


if __name__ == "__main__":
    main()