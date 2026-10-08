#!/usr/bin/env python3
"""Affiche des extraits du corpus public pour rédiger des requêtes d'évaluation.

Les 16 requêtes dérivées des incidents partagent toutes la même forme :
elles proviennent des logs et contiennent les identifiants exacts du bac
à sable. Elles mesurent donc une tâche lexicale.

Ce second jeu, rédigé à la main en décrivant les symptômes avec d'autres
mots, mesure ce que le premier ne peut pas : la capacité à retrouver un
document à partir d'une description, sans correspondance de termes.

Usage:
    uv run python scripts/sample_corpus.py
    uv run python scripts/sample_corpus.py --doc PUB-012
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus" / "public_postmortems.jsonl"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--doc", help="affiche un document complet")
    p.add_argument("--chars", type=int, default=600)
    args = p.parse_args()

    docs = [json.loads(line) for line in CORPUS.open(encoding="utf-8")]

    if args.doc:
        doc = next((d for d in docs if d["doc_id"] == args.doc), None)
        if not doc:
            print(f"introuvable : {args.doc}")
            return
        print(f"=== {doc['doc_id']} — {doc['title']}")
        print(f"{doc['url']}\n")
        print(doc["text"][:4000])
        return

    for doc in docs:
        print(f"\n{'=' * 70}")
        print(f"{doc['doc_id']}  {doc['title']}")
        print(f"{doc['url']}")
        print(f"\n{doc['text'][:args.chars]}")


if __name__ == "__main__":
    main()