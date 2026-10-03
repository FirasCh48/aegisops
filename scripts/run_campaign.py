#!/usr/bin/env python3
"""Enchaîne les captures d'incidents sans intervention.

Quinze captures représentent environ 95 minutes d'attente. Les lancer à
la main reviendrait à rester devant le terminal pendant tout ce temps —
et à commettre, statistiquement, les mêmes oublis qu'en J2 à J4.

Usage:
    uv run python scripts/run_campaign.py              # les 15
    uv run python scripts/run_campaign.py --plan       # affiche sans exécuter
    uv run python scripts/run_campaign.py --only slow_response
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Trois variantes par panne : intensités et durées différentes, pour que
# le modèle n'apprenne pas une valeur mais une forme. Le dernier champ
# marque les incidents réservés à l'évaluation.
#
# holdout=True : jamais utilisé comme graine de génération synthétique en
# S6. Sans cette séparation, le modèle serait entraîné sur des variantes
# dérivées des incidents sur lesquels on l'évalue — la métrique
# paraîtrait excellente et serait fausse.
CAMPAIGN = [
    # scénario,          durée, rps, holdout
    ("connection_leak",    180, 6.0, False),
    ("connection_leak",    180, 9.0, False),
    ("connection_leak",    240, 4.0, True),

    ("slow_response",      180, 6.0, False),
    ("slow_response",      150, 9.0, False),
    ("slow_response",      180, 4.0, True),

    ("bad_release",        180, 6.0, False),
    ("bad_release",        240, 8.0, False),
    ("bad_release",        180, 5.0, True),

    ("internal_error",     180, 6.0, False),
    ("internal_error",     180, 9.0, False),
    ("internal_error",     150, 5.0, True),

    ("cpu_saturation",     150, 6.0, False),
    ("cpu_saturation",     150, 9.0, True),

    ("memory_leak",        600, 8.0, False),
]


def estimate_minutes() -> float:
    # warmup 60 s + durée + 20 s de marge + ~30 s de collecte et de reset
    return sum(60 + d + 50 for _, d, _, _ in CAMPAIGN) / 60


def run_one(scenario: str, duration: int, rps: float, holdout: bool) -> bool:
    cmd = [
        sys.executable, str(ROOT / "scripts" / "capture_incident.py"),
        scenario,
        "--duration", str(duration),
        "--rps", str(rps),
    ]
    if holdout:
        cmd.append("--holdout")

    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode == 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", action="store_true", help="affiche le plan sans exécuter")
    p.add_argument("--only", help="ne joue que ce scénario")
    args = p.parse_args()

    plan = CAMPAIGN
    if args.only:
        plan = [c for c in CAMPAIGN if c[0] == args.only]
        if not plan:
            sys.exit(f"scénario inconnu : {args.only}")

    print(f"campagne : {len(plan)} captures, ~{estimate_minutes():.0f} min")
    for i, (scenario, duration, rps, holdout) in enumerate(plan, 1):
        tag = " [holdout]" if holdout else ""
        print(f"  {i:2}. {scenario:<18} {duration}s @ {rps} req/s{tag}")

    if args.plan:
        return

    print()
    failed = []
    for i, (scenario, duration, rps, holdout) in enumerate(plan, 1):
        print(f"\n########## {i}/{len(plan)} ##########")
        if not run_one(scenario, duration, rps, holdout):
            failed.append(f"{scenario}@{rps}")
            print(f"  ÉCHEC sur {scenario}")
        # Laisse le système revenir au repos : une capture qui démarre
        # pendant que la précédente se stabilise encore hérite de son bruit.
        time.sleep(15)

    print(f"\n=== campagne terminée ===")
    if failed:
        print(f"échecs : {', '.join(failed)}")


if __name__ == "__main__":
    main()