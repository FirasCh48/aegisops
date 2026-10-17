#!/usr/bin/env python3
"""Mesure la précision du détecteur de fenêtre.

Chaque incident capturé en semaine 2 porte sa fenêtre réelle :
`injected_at` et `cleared_at`. Le détecteur peut donc être évalué sur
une vérité connue, et non sur une appréciation.

Deux erreurs à distinguer, parce qu'elles n'ont pas les mêmes
conséquences :

- une fenêtre qui commence trop tard rate le début de l'incident, donc
  la cause. C'est l'erreur grave.
- une fenêtre trop large inclut du bruit. C'est coûteux en contexte,
  mais récupérable.

Usage:
    uv run python eval/detector_eval.py
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

from aegisops.agents.detector import WindowDetector
from aegisops.agents.state import Alert

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"


def evaluate() -> None:
    files = sorted(INCIDENTS.glob("INC-*.json"))
    if not files:
        print("aucun incident")
        return

    detector = WindowDetector()
    rows, offsets = [], []

    for path in files:
        record = json.loads(path.read_text())
        true_start = datetime.fromisoformat(record["window"]["injected_at"])
        true_end = datetime.fromisoformat(record["window"]["cleared_at"])

        # L'alerte est simulée au moment où elle se serait déclenchée :
        # une minute après l'injection, le temps que les métriques
        # franchissent un seuil.
        alert = Alert(
            service=record["injected_service"],
            summary="simulated alert",
            fired_at=true_start + (true_end - true_start) / 2,
        )

        window, changepoints = detector.detect(alert)

        # Écart au début réel. Négatif = le détecteur a commencé avant
        # l'injection, ce qui est souhaitable.
        offset = (window.start - true_start).total_seconds()
        offsets.append(offset)

        covers = window.start <= true_start and window.end >= true_start
        rows.append({
            "scenario": record["scenario"],
            "offset_s": round(offset, 1),
            "duration_s": round(window.duration_s, 0),
            "true_duration_s": round((true_end - true_start).total_seconds(), 0),
            "covers_start": covers,
            "signals": len(changepoints),
            "reason": window.reason[:60],
        })

    print(f"{len(rows)} incidents\n")
    print(f"  {'scénario':<18} {'écart':>8} {'durée':>8} {'réelle':>8} "
          f"{'couvre':>7} {'signaux':>8}")
    for r in rows:
        mark = "oui" if r["covers_start"] else "NON"
        print(f"  {r['scenario']:<18} {r['offset_s']:>7.0f}s "
              f"{r['duration_s']:>7.0f}s {r['true_duration_s']:>7.0f}s "
              f"{mark:>7} {r['signals']:>8}")

    covered = sum(1 for r in rows if r["covers_start"])
    print(f"\n  couverture du début : {covered}/{len(rows)} "
          f"({covered / len(rows):.0%})")
    print(f"  écart médian        : {statistics.median(offsets):.0f}s")

    failures = [r for r in rows if not r["covers_start"]]
    if failures:
        print(f"\n  échecs ({len(failures)}) :")
        for r in failures:
            print(f"    {r['scenario']:<18} {r['reason']}")


if __name__ == "__main__":
    evaluate()