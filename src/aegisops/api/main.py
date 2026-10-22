"""API d'investigation : lancer, consulter, approuver.

Trois endpoints, et une propriété qui les relie : **l'API n'exécute
jamais une action de remédiation**. Elle reprend un graphe suspendu avec
une décision humaine, et le graphe enregistre cette décision. Ce qui se
passe ensuite — redémarrer un service, déployer un correctif — reste
entre les mains de la personne qui a approuvé.

Cette limite est délibérée et doit le rester. Ajouter l'exécution
demanderait d'écrire un exécuteur, de lui donner des accès, et de
décider ce qu'il fait d'un échec à mi-parcours. Trois décisions qui ne
doivent pas être prises par inadvertance.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from langgraph.types import Command

from aegisops.agents.graph import (
    CHECKPOINT_DB,
    build_graph,
    make_checkpointer,
)
from aegisops.agents.state import Alert, ApprovalOutcome

app = FastAPI(title="AegisOps", version="0.1.0")


def _runtime():
    """Ouvre une connexion et compile le graphe.

    Une connexion par requête : SQLite n'aime pas être partagé entre
    threads, et FastAPI en utilise plusieurs. Le coût est négligeable
    devant celui d'un appel LLM.
    """
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    return conn, build_graph(make_checkpointer(conn))


@app.post("/incidents")
def create_incident(alert: Alert, background: BackgroundTasks):
    """Lance une investigation.

    Le graphe tourne en arrière-plan : une investigation complète prend
    plusieurs secondes avec un fournisseur distant et plusieurs minutes
    en local. Faire attendre l'appelant obligerait à choisir un timeout,
    et tout timeout serait soit trop court en local, soit trop long pour
    un client HTTP.
    """
    thread_id = alert.labels.get("incident_id") or alert.fired_at.isoformat()

    def run():
        conn, graph = _runtime()
        try:
            graph.invoke(
                {"alert": alert},
                config={"configurable": {"thread_id": thread_id}},
            )
        finally:
            conn.close()

    background.add_task(run)
    return {"incident_id": thread_id, "status": "running"}


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Consulte l'état d'une investigation.

    L'état vient du checkpointer, pas d'une mémoire de processus : une
    investigation lancée hier reste consultable aujourd'hui, et
    redémarrer l'API ne perd rien.
    """
    conn, graph = _runtime()
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": incident_id}})
    finally:
        conn.close()

    if not snapshot.values:
        raise HTTPException(404, f"incident inconnu : {incident_id}")

    values = snapshot.values
    context = values.get("context")
    hypothesis = values.get("hypothesis")
    policy = values.get("policy")

    return {
        "incident_id": incident_id,
        # `next` vide signifie terminé ; sinon il nomme le nœud où le
        # graphe attend.
        "status": "awaiting_approval" if snapshot.next else "complete",
        "waiting_at": snapshot.next[0] if snapshot.next else None,
        "root_cause": hypothesis.root_cause if hypothesis else None,
        "confidence": hypothesis.confidence if hypothesis else None,
        "summary": context.summary() if context else None,
        "policy": policy.summary() if policy else None,
        "pending": [
            {"order": d.order, "action": d.action, "risk": d.risk,
             "reasons": d.reasons}
            for d in (policy.pending if policy else [])
        ],
        "trace": [s for s in values.get("trace", []) if "ms" in s],
        "errors": values.get("errors", []),
    }


@app.post("/incidents/{incident_id}/approve")
def approve(incident_id: str, outcome: ApprovalOutcome):
    """Reprend le graphe avec une décision humaine.

    Ne déclenche aucune exécution. La décision est enregistrée dans
    l'état, et c'est la personne qui a approuvé qui agit.
    """
    return _resume(incident_id, outcome)


@app.post("/incidents/{incident_id}/reject")
def reject(incident_id: str, outcome: ApprovalOutcome):
    """Reprend le graphe en refusant toutes les étapes en attente."""
    conn, graph = _runtime()
    config = {"configurable": {"thread_id": incident_id}}
    try:
        snapshot = graph.get_state(config)
        policy = snapshot.values.get("policy")
        pending = [d.order for d in policy.pending] if policy else []
    finally:
        conn.close()

    outcome.approved_orders = []
    outcome.rejected_orders = pending
    return _resume(incident_id, outcome)


def _resume(incident_id: str, outcome: ApprovalOutcome):
    conn, graph = _runtime()
    config = {"configurable": {"thread_id": incident_id}}
    try:
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise HTTPException(404, f"incident inconnu : {incident_id}")
        if not snapshot.next:
            raise HTTPException(
                409, "cette investigation n'attend pas d'approbation"
            )

        graph.invoke(Command(resume=outcome.model_dump()), config=config)
    finally:
        conn.close()

    return {
        "incident_id": incident_id,
        "approved": outcome.approved_orders,
        "rejected": outcome.rejected_orders,
        "decided_by": outcome.decided_by,
    }
