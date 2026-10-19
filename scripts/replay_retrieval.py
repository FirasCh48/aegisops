#!/usr/bin/env python3
"""Rejoue le retrieval du pipeline sur les incidents stockés, hors ligne.

Les observations sont reconstruites depuis `data/incidents/INC-*.json`
sans Prometheus ni Loki, puis passent par le vrai `build_search_query` et
le vrai index BM25. C'est la seule façon de mesurer sur plus d'un
incident : la rétention de Prometheus est de six heures.

Deux approximations, à garder en tête en lisant les chiffres :

- Métriques. Seules les captures récentes ont `metrics_raw` ; elles
  passent par `compare()` tel quel. Les autres n'ont que pic, moyenne et
  dernière valeur, et `compare()` reçoit ces trois points. Sur les trois
  captures qui ont les deux, l'ensemble des métriques anormales est
  identique.
- Logs. Les comptes et premiers échantillons de la phase `incident`,
  pas ceux de la fenêtre que délimiterait le détecteur.

Usage:
    uv run python scripts/replay_retrieval.py
    uv run python scripts/replay_retrieval.py --verbose
"""
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path

from aegisops.agents.collector import build_search_query
from aegisops.agents.log_agent import QUALIFYING_FIELDS
from aegisops.agents.metrics_agent import SIGNALS, _label, compare
from aegisops.agents.state import LogObservation
from aegisops.rag.sparse import SparseIndex
from aegisops.tools.prometheus import Series

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = sorted(glob.glob(str(ROOT / "data" / "incidents" / "INC-*.json")))

# Les deux scénarios que seul l'événement `deployment` sépare.
POOL = ("connection_leak", "bad_release")


def _series_raw(entry: dict) -> Series:
    return Series(
        labels=entry["labels"],
        points=[(datetime.fromisoformat(t), v) for t, v in entry["points"]],
    )


def _series_summary(entry: dict) -> Series:
    pts = [entry[k] for k in ("mean", "last", "peak") if entry.get(k) is not None]
    return Series(labels=entry["labels"], points=[(None, v) for v in pts])


def metric_observations(incident: dict) -> list:
    raw = bool(incident.get("metrics_raw"))
    src = incident["metrics_raw"] if raw else incident["metrics"]
    make = _series_raw if raw else _series_summary
    out = []
    for metric, spec in SIGNALS.items():
        if metric not in src:
            continue
        base = {_label(s): s for s in map(make, src[metric].get("baseline", []))}
        for inc in map(make, src[metric].get("incident", [])):
            if inc.points:
                out.append(compare(
                    base.get(_label(inc)), inc, metric, spec["discriminates"],
                    spec.get("compare_on", "peak"),
                    spec.get("saturation_threshold", 0.9),
                ))
    return out


def log_observations(incident: dict) -> list[LogObservation]:
    out = []
    for service, phases in incident["logs"].items():
        phase = phases.get("incident", {})
        samples = phase.get("samples", {})
        for event, count in sorted(phase.get("counts", {}).items(), key=lambda x: -x[1]):
            s = samples.get(event) or {}
            s = s[0] if isinstance(s, list) else s
            out.append(LogObservation(
                service=service, event=event, count=count,
                sample={k: s[k] for k in QUALIFYING_FIELDS if k in s},
            ))
    return out


def rank_of(hits, doc_id: str) -> int | None:
    return next((i for i, h in enumerate(hits, 1) if h.doc_id == doc_id), None)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    index = SparseIndex()
    index.load()

    configs = {
        "chunks (search)": lambda q: index.search(q, limit=40),
        "documents (search_documents)": lambda q: index.search_documents(q, limit=40),
    }

    print(f"{len(INCIDENTS)} incidents\n")
    print(f"  {'config':30} {'@1':>4} {'@3':>4} {'@5':>4} {'MRR':>6}  pool pair in top 5")
    for label, search in configs.items():
        at1 = at3 = at5 = pair = n_pool = 0
        mrr = 0.0
        rows = []
        for path in INCIDENTS:
            inc = json.loads(Path(path).read_text(encoding="utf-8"))
            scenario = inc["scenario"]
            query = build_search_query(
                inc["injected_service"], metric_observations(inc), log_observations(inc)
            )
            hits = search(query)
            r = rank_of(hits, f"sandbox/{scenario}.md")
            at1 += r == 1
            at3 += bool(r and r <= 3)
            at5 += bool(r and r <= 5)
            mrr += 1 / r if r else 0.0
            if scenario in POOL:
                n_pool += 1
                top5 = {h.doc_id for h in hits[:5]}
                pair += all(f"sandbox/{s}.md" in top5 for s in POOL)
            rows.append((inc["incident_id"], scenario, r, [h.chunk_id for h in hits[:3]]))

        n = len(INCIDENTS)
        print(f"  {label:30} {at1:>4} {at3:>4} {at5:>4} {mrr / n:>6.3f}  {pair}/{n_pool}")
        if args.verbose:
            for iid, scenario, r, top in rows:
                print(f"      {iid:40} {scenario:15} rank={r}  {top}")


if __name__ == "__main__":
    main()
