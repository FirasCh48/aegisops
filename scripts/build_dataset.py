#!/usr/bin/env python3
"""Construit les exemples graines du dataset de fine-tuning.

La partie « entrée » est générée : c'est l'invite de production, bâtie
sur le contexte réel de la capture. Le graphe tourne jusqu'à `assemble`
et s'arrête avant `analyse`, ce qui donne le contexte tel que l'agent de
raisonnement le reçoit, sans dépenser un token.

La partie « sortie » n'est pas générée. Elle est écrite à la main depuis
le postmortem. Si elle venait du modèle, le fine-tuning apprendrait les
erreurs du modèle lui-même — et la mesure de S5-J6 a montré qu'il y en a
(25/32 causes nommées, `cpu_saturation` 0/4).

Trois fichiers, pour que rien d'écrit à la main ne soit écrasé :

    data/dataset/seeds_input.jsonl   généré    (l'invite)
    data/dataset/outputs/*.json      à la main (la sortie attendue)
    data/dataset/seeds.jsonl         généré    (les deux, assemblés)

Usage:
    uv run python scripts/build_dataset.py --seeds     # (re)génère les invites
    uv run python scripts/build_dataset.py --show 1    # affiche une invite
    uv run python scripts/build_dataset.py --verify    # contrôle et assemble
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "data" / "incidents"
DATASET = ROOT / "data" / "dataset"
INPUTS = DATASET / "seeds_input.jsonl"
OUTPUTS = DATASET / "outputs"
MERGED = DATASET / "seeds.jsonl"


# --- Sélection des captures ---------------------------------------------

def candidates() -> tuple[list[dict], list[dict], list[tuple[str, list[str]]]]:
    """Captures réparties en graines / test / écartées.

    L'écart se fonde sur `capture_health.health`, le même contrôle que
    l'audit. Une liste d'exclusions écrite ici à la main aurait fini par
    diverger de l'audit — c'est déjà arrivé trois fois sur la liste des
    types msgpack, et chaque fois l'oubli ne s'est vu que bien plus tard.

    Une capture défectueuse entrée comme graine serait recopiée dans
    chacune de ses variantes synthétiques en J2.

    Le découpage se fait sur la graine, pas sur l'exemple : si une
    capture sert de graine, ses variantes entrent à l'entraînement, et si
    elle est aussi dans le test, l'entraînement a vu le test de façon
    indirecte.
    """
    from aegisops.tools.capture_health import health

    seeds: list[dict] = []
    test: list[dict] = []
    rejected: list[tuple[str, list[str]]] = []

    for path in sorted(INCIDENTS.glob("INC-*.json")):
        record = json.loads(path.read_text())
        defects = health(record)
        if defects:
            rejected.append((record["incident_id"], defects))
            continue
        row = {
            "incident_id": record["incident_id"],
            "scenario": record["scenario"],
            "path": str(path.relative_to(ROOT)),
        }
        (test if record.get("holdout") else seeds).append(row)

    return seeds, test, rejected


# --- Sources citables ----------------------------------------------------

def available_sources(context) -> list[str]:
    """Noms de sources que le vérificateur acceptera.

    Posé dans le squelette de sortie pour que les preuves soient
    choisies dans une liste au lieu d'être écrites de mémoire. Une
    preuve inventée serait rejetée au contrôle, et un exemple
    d'entraînement qui cite une source absente enseignerait exactement
    ce que le vérificateur existe pour attraper.

    Indicatif : la forme exacte reste celle que le vérificateur lit.
    """
    sources: list[str] = []

    for m in getattr(context, "metrics", []):
        name = getattr(m, "metric", None)
        if not name:
            continue
        labels = getattr(m, "labels", {}) or {}
        dep = labels.get("dependency") or labels.get("reason") or ""
        tag = f"[{dep}]" if dep else ""
        flag = "" if getattr(m, "anomalous", False) else "   (non signalée)"
        sources.append(f"metric:{name}{tag}{flag}")

    for entry in getattr(context, "logs", []):
        event = getattr(entry, "event", None) or getattr(entry, "template", None)
        if event:
            count = getattr(entry, "count", "")
            suffix = f"   ({count})" if count != "" else ""
            sources.append(f"log:{event}{suffix}")

    return sources



# Événements qu'un injecteur de panne émet, et les scénarios où ils ont
# leur place. Un `connection_leaked` dans une capture `slow_response`
# n'est pas une conséquence : c'est la panne d'une autre capture, celle
# qui tournait en parallèle en S5.
FAULT_SIGNATURES = {
    "connection_leaked": {"connection_leak", "bad_release"},
    "inventory_internal_error": {"internal_error"},
    "deployment": {"bad_release"},
}


def seed_quality(state, scenario: str) -> list[str]:
    """Défauts visibles seulement après le rejeu.

    `capture_health` juge la capture ; ceci juge le contexte qu'elle
    produit. Deux choses n'apparaissent qu'ici :

    - Une fenêtre par défaut. Le détecteur n'a rien trouvé et retombe sur
      dix minutes avant l'alerte, ce qui déborde du début de la capture
      et ramène les données de la précédente. Les métriques sortent alors
      « from zero » sans l'être.
    - Un événement de panne étranger au scénario.
    """
    defects: list[str] = []

    if "aucun changement" in (state["window"].reason or ""):
        defects.append("fenêtre par défaut : le détecteur n'a rien trouvé")

    for entry in state["context"].logs:
        event = getattr(entry, "event", None) or ""
        owners = FAULT_SIGNATURES.get(event)
        if owners and scenario not in owners:
            defects.append(f"événement {event} étranger au scénario")

    return defects


SKELETON = {
    "root_cause": "",
    "confidence": 0.0,
    "evidence": [
        {"source": "", "observation": "", "supports": ""},
    ],
    "ruled_out": [
        "",
    ],
}


# --- Génération des invites ---------------------------------------------

def build_seeds() -> None:
    from aegisops.agents.graph import replay
    from aegisops.agents.root_cause import PROMPT_VERSION, SYSTEM, build_prompt

    seeds, test, rejected = candidates()

    print(f"graines : {len(seeds)}   test : {len(test)}   écartées : {len(rejected)}")
    for inc, defects in rejected:
        print(f"  - {inc}: {' · '.join(defects)}")
    print()
    print(f"graines par scénario : {dict(Counter(s['scenario'] for s in seeds))}")
    print(f"test    par scénario : {dict(Counter(s['scenario'] for s in test))}")
    print(f"\nprompt version : {PROMPT_VERSION}\n")

    DATASET.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)

    rows = []
    created = 0

    for seed in seeds:
        example_id = f"SEED-{seed['incident_id']}"

        state = replay(
            ROOT / seed["path"],
            thread_id=f"dataset-{seed['incident_id']}",
            stop_before="analyse",
        )
        context = state.get("context")
        if context is None:
            print(f"  ÉCHEC {seed['incident_id']:<42} {state.get('errors')}")
            continue

        bad = seed_quality(state, seed["scenario"])
        if bad:
            print(f"  ÉCART {seed['incident_id']:<42} {' · '.join(bad)}")
            continue

        rows.append({
            "id": example_id,
            "scenario": seed["scenario"],
            "source_incident": seed["incident_id"],
            "holdout": False,
            "prompt_version": PROMPT_VERSION,
            # `SYSTEM` est recopié dans chaque exemple. Les règles
            # passent par un canal distinct de l'invite, et un exemple
            # qui ne les porterait pas apprendrait la tâche sans ses
            # contraintes. Le fichier reste ainsi autonome : le notebook
            # Colab ne lit que lui.
            "system": SYSTEM,
            "input": build_prompt(state),
        })

        # Le squelette n'est jamais écrasé : c'est le seul fichier écrit
        # à la main, et le regénérer effacerait le travail de la session.
        out_path = OUTPUTS / f"{example_id}.json"
        if not out_path.exists():
            skeleton = dict(SKELETON)
            skeleton["_available_sources"] = available_sources(context)
            skeleton["_scenario"] = seed["scenario"]
            skeleton["_postmortem"] = f"data/corpus/sandbox/{seed['scenario']}.md"
            out_path.write_text(
                json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            created += 1

        filled = out_path.exists() and bool(
            json.loads(out_path.read_text(encoding="utf-8")).get("root_cause")
        )
        mark = "·" if filled else " "
        print(f"  {mark} {seed['incident_id']:<42} "
              f"{len(rows[-1]['input']):>6} car, "
              f"{len(available_sources(context)):>2} sources")

    with INPUTS.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nécrit : {INPUTS}  ({len(rows)} invites)")
    print(f"        {OUTPUTS}/  ({created} squelettes créés)")
    print("\nà faire : remplir chaque fichier de outputs/, puis --verify")


# --- Affichage ------------------------------------------------------------

def show(which: str) -> None:
    rows = [json.loads(l) for l in INPUTS.open(encoding="utf-8") if l.strip()]

    if which.isdigit():
        row = rows[int(which) - 1]
    else:
        row = next(r for r in rows if which in r["id"])

    print("=" * 72)
    print(f"{row['id']}   scénario : {row['scenario']}")
    print("=" * 72)
    print(row["input"])
    print("=" * 72)
    print(f"postmortem : data/corpus/sandbox/{row['scenario']}.md")
    print(f"sortie     : {OUTPUTS.relative_to(ROOT)}/{row['id']}.json")


# --- Contrôle et assemblage ---------------------------------------------

def verify_and_merge() -> None:
    """Contrôle chaque sortie écrite à la main, puis assemble.

    Le contrôle est le nœud `verify` de production, appelé tel quel. Le
    même filtre servira en S6-J3 sur les exemples synthétiques — il sera
    alors déjà éprouvé sur les graines.

    L'assemblage n'a lieu que si tout passe : un dataset à moitié propre
    est plus dangereux qu'un dataset absent, parce qu'il se mesure.
    """
    from aegisops.agents.graph import replay
    from aegisops.agents.state import RootCauseHypothesis
    from aegisops.agents.verifier import verify

    rows = [json.loads(l) for l in INPUTS.open(encoding="utf-8") if l.strip()]
    print(f"{len(rows)} invites\n")

    merged = []
    clean_count = 0
    empty = []

    for row in rows:
        out_path = OUTPUTS / f"{row['id']}.json"
        if not out_path.exists():
            empty.append(row["id"])
            continue

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        # Les clés d'aide commencent par un souligné et ne font pas
        # partie de l'exemple.
        payload = {k: v for k, v in payload.items() if not k.startswith("_")}

        if not payload.get("root_cause"):
            empty.append(row["id"])
            continue

        try:
            hypothesis = RootCauseHypothesis.model_validate(payload)
        except Exception as e:
            print(f"  XX  {row['id']:<46} JSON invalide : {e}")
            continue

        state = replay(
            INCIDENTS / f"{row['source_incident']}.json",
            thread_id=f"verify-{row['source_incident']}",
            stop_before="analyse",
        )
        state["hypothesis"] = hypothesis
        report = verify(state)["verification"]

        unsupported = report.unsupported_count
        misquoted = report.misquoted_count
        orphans = len(getattr(report, "ruled_out_unsupported", []) or [])
        clean = unsupported == 0 and misquoted == 0 and orphans == 0
        clean_count += clean

        print(f"  {'ok ' if clean else 'XX '} {row['id']:<46} "
              f"{len(hypothesis.evidence)} preuves, "
              f"{unsupported} sans source, {misquoted} mal citées, "
              f"{orphans} écartées sans source")

        if not clean:
            for claim in report.claims:
                if claim.status != "verified":
                    print(f"          {claim.status:<12} {claim.source}  {claim.detail}")
            for orphan in getattr(report, "ruled_out_unsupported", []) or []:
                print(f"          écartée sans source : {orphan[:60]}")

        merged.append({
            **row,
            "output": json.dumps(payload, ensure_ascii=False),
            "verified": clean,
        })

    if empty:
        print(f"\n{len(empty)} sorties encore vides :")
        for e in empty:
            print(f"  - {e}")

    total = len(merged)
    print(f"\n{clean_count}/{total} graines propres")

    if total and clean_count == total and not empty:
        with MERGED.open("w", encoding="utf-8") as f:
            for row in merged:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"écrit : {MERGED}  ({total} exemples)")
    else:
        print("rien assemblé : corriger les preuves rejetées et les sorties "
              "vides d'abord — une graine fautive sera recopiée dans chacune "
              "de ses variantes en J2")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seeds", action="store_true",
                   help="génère les invites et les squelettes de sortie")
    p.add_argument("--show", metavar="N|ID",
                   help="affiche une invite (numéro ou fragment d'identifiant)")
    p.add_argument("--verify", action="store_true",
                   help="contrôle les sorties écrites à la main, puis assemble")
    args = p.parse_args()

    if args.show:
        show(args.show)
    elif args.verify:
        verify_and_merge()
    else:
        build_seeds()


if __name__ == "__main__":
    main()
