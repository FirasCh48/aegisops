#!/usr/bin/env python3
"""Contrôle de santé des captures rejouables.

Écrit après S6-J1, où deux des seize captures se sont révélées
inutilisables : l'une avait la panne d'une capture concurrente dans sa
baseline, l'autre une fenêtre de 2 h 55 sans aucun événement
applicatif. Aucune des deux ne levait d'erreur — elles se rejouaient
sans se plaindre.

Quatre contrôles, et le premier est celui qui manquait :

  BASELINE COURTE   moins de 120 s avant l'injection. Le détecteur prend
                    huit points de référence à 15 s de pas ; en dessous,
                    la « baseline » contient déjà l'incident.
  BASELINE SALE     un événement de panne avant l'injection.
  SANS TRAFIC       aucun événement du tout pendant l'incident. Compter
                    les événements nominaux ne suffit pas : sur
                    slow_response, zéro commande aboutie est le symptôme
                    attendu, pas un défaut.
  CHEVAUCHEMENT     la fenêtre empiète sur celle de la capture
                    précédente.
"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NOMINAL = {"order_created", "stock_reserved", "payment_authorized", "service_started"}
CONTEXT = {"deployment", "stock_reset"}
SANDBOX = {"fault_injected", "fault_cleared"}
BENIGN = NOMINAL | CONTEXT | SANDBOX

MIN_BASELINE_S = 120   # 8 points de référence à 15 s de pas
MAX_TOTAL_S = 1200     # memory_leak observe 600 s, le reste bien moins


def ts(s):
    return datetime.fromisoformat(s)


rows = []
for p in sorted((ROOT / "data" / "incidents").glob("INC-*.json")):
    r = json.loads(p.read_text())
    if not r.get("metrics_raw"):
        continue
    w, svc = r["window"], r["injected_service"]
    logs = r["logs"].get(svc, {})
    base = logs.get("baseline", {}).get("counts", {})
    inc = logs.get("incident", {}).get("counts", {})
    rows.append({
        "id": r["incident_id"],
        "svc": svc,
        "holdout": bool(r.get("holdout")),
        "from": ts(w["baseline_from"]),
        "inj": ts(w["injected_at"]),
        "clr": ts(w["cleared_at"]),
        "end": ts(w["end"]),
        "dirty": {k: v for k, v in base.items() if k not in BENIGN},
        "inc_total": sum(v for k, v in inc.items() if k not in SANDBOX),
    })

print(f"{'incident':<40} {'svc':<18} {'h':>2} {'base':>6} {'inc':>6} "
      f"{'tot':>7} {'evts':>6}  defauts")

prev_end = None
ok_seeds, ok_test = 0, 0

for x in rows:
    base_s = (x["inj"] - x["from"]).total_seconds()
    inc_s = (x["clr"] - x["inj"]).total_seconds()
    tot_s = (x["end"] - x["from"]).total_seconds()

    flags = []
    if base_s < MIN_BASELINE_S:
        flags.append(f"BASELINE COURTE ({base_s:.0f}s)")
    if tot_s > MAX_TOTAL_S:
        flags.append(f"FENETRE ({tot_s/60:.0f}min)")
    if x["dirty"]:
        flags.append(f"BASELINE SALE {x['dirty']}")
    if x["inc_total"] == 0:
        flags.append("SANS TRAFIC")
    if prev_end and x["from"] < prev_end:
        flags.append("CHEVAUCHEMENT")
    prev_end = x["end"]

    if not flags:
        if x["holdout"]:
            ok_test += 1
        else:
            ok_seeds += 1

    note = "  " + " | ".join(flags) if flags else "  ok"
    print(f"{x['id']:<40} {x['svc']:<18} {'H' if x['holdout'] else '':>2} "
          f"{base_s:>6.0f} {inc_s:>6.0f} {tot_s:>7.0f} {x['inc_total']:>6}{note}")

print(f"\n{len(rows)} rejouables — {ok_seeds} graines saines, {ok_test} test saines")
