"""Contrôles de santé d'une capture, en un seul endroit.

Écrit après S6-J1, où deux captures sur seize se sont révélées
inutilisables sans jamais lever d'erreur : l'une portait la panne d'une
capture concurrente dans sa baseline, l'autre avait une fenêtre de
2 h 55 sans un seul événement applicatif. Les deux se rejouaient sans
se plaindre, et l'une d'elles avait servi à la mesure de S5-J6.

Le contrôle est ici et non dans le script d'audit pour que le harnais
de dataset lise la même définition. Une liste d'exclusions écrite à la
main aurait été oubliée — c'est déjà arrivé trois fois sur la liste des
types msgpack.

Le chevauchement de fenêtres n'est pas ici : il se juge entre captures
successives, pas sur une capture seule. Il reste dans l'audit.
"""
from __future__ import annotations

from datetime import datetime

NOMINAL = {"order_created", "stock_reserved", "payment_authorized", "service_started"}
CONTEXT = {"deployment", "stock_reset"}
SANDBOX = {"fault_injected", "fault_cleared"}
BENIGN = NOMINAL | CONTEXT | SANDBOX

# Le détecteur prend huit points de référence à 15 s de pas. En dessous
# de 120 s, sa « baseline » contient déjà l'incident : mesuré en S6-J1,
# une capture à 45 s donne « aucun changement détecté » puis un contexte
# vide, et une à 44 s ancre la fenêtre sur p95_by_dependency au lieu de
# db_pool_usage — une conséquence au lieu de la cause.
MIN_BASELINE_S = 120

# memory_leak observe 600 s, le reste bien moins. Au-delà de 1200 s la
# capture a été interrompue ou bloquée.
MAX_TOTAL_S = 1200


def health(record: dict) -> list[str]:
    """Défauts d'une capture. Liste vide = saine."""
    if not record.get("metrics_raw"):
        return ["sans metrics_raw (capture antérieure à S4-J2)"]

    w = record["window"]
    service = record["injected_service"]
    logs = record.get("logs", {}).get(service, {})
    baseline = logs.get("baseline", {}).get("counts", {})
    incident = logs.get("incident", {}).get("counts", {})

    start = datetime.fromisoformat(w["baseline_from"])
    injected = datetime.fromisoformat(w["injected_at"])
    end = datetime.fromisoformat(w["end"])

    base_s = (injected - start).total_seconds()
    total_s = (end - start).total_seconds()

    defects: list[str] = []

    if base_s < MIN_BASELINE_S:
        defects.append(f"baseline courte ({base_s:.0f}s < {MIN_BASELINE_S}s)")

    if total_s > MAX_TOTAL_S:
        defects.append(f"fenêtre de {total_s / 60:.0f} min")

    dirty = {k: v for k, v in baseline.items() if k not in BENIGN}
    if dirty:
        defects.append(f"baseline salie par {dirty}")

    # Tous les événements, pas seulement les nominaux : sur
    # slow_response, zéro commande aboutie est le symptôme attendu.
    if sum(v for k, v in incident.items() if k not in SANDBOX) == 0:
        defects.append("aucun événement pendant l'incident")

    return defects
