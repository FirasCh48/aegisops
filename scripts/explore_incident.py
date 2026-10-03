#!/usr/bin/env python3
"""Explore un incident capturé depuis la ligne de commande.

Sert à relire une capture sans ouvrir le JSON brut. C'est aussi un
premier contact avec les données telles que le Metrics Agent les verra
en semaine 4 : des résumés par phase, pas des séries de points.

Usage:
    uv run python scripts/explore_incident.py
    uv run python scripts/explore_incident.py --scenario slow_response
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"

PHASES = ("baseline", "incident", "recovery")


def show(record: dict) -> None:
    load = record.get("load", {})
    tag = "  [HOLDOUT]" if record["holdout"] else ""
    print(f"\n{'=' * 70}")
    print(f"{record['incident_id']}{tag}")
    print(f"  injecté sur {record['injected_service']} · "
          f"{load.get('rps')} req/s · {record['window']['duration_s']}s")

    print("\n  --- logs checkout-service")
    for phase in PHASES:
        counts = record["logs"]["checkout-service"][phase]["counts"]
        print(f"    {phase:<10} {counts}")

    print("\n  --- p95 par dépendance (pic)")
    for phase in PHASES:
        series = record["metrics"]["p95_by_dependency"][phase]
        if isinstance(series, dict):  # cas d'erreur
            print(f"    {phase:<10} {series}")
            continue
        parts = [
            f"{s['labels'].get('dependency', '?')}={s['peak']}"
            for s in series
        ]
        print(f"    {phase:<10} {'  '.join(parts) if parts else '(vide)'}")

    print("\n  --- autres signaux (pic par phase)")
    for key in ("error_rate", "p95_checkout", "db_pool_usage", "memory_mb"):
        line = []
        for phase in PHASES:
            series = record["metrics"][key][phase]
            peak = series[0]["peak"] if isinstance(series, list) and series else None
            line.append(f"{phase}={peak}")
        print(f"    {key:<18} {'  '.join(line)}")

    print(f"\n  --- cause réelle")
    print(f"    {record['ground_truth']['root_cause']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", help="ne montre que ce scénario")
    args = p.parse_args()

    files = sorted(INCIDENTS.glob("INC-*.json"))
    if not files:
        print("aucun incident")
        return

    for path in files:
        record = json.loads(path.read_text())
        if args.scenario and record["scenario"] != args.scenario:
            continue
        show(record)


if __name__ == "__main__":
    main()