"""Human approval gate : le graphe s'arrête et attend.

Dernier nœud. Les quatre précédents produisaient des rapports ; celui-ci
produit un **blocage**.

La différence est celle entre un système qui recommande et un système
qui ne peut pas agir seul. Un rapport, aussi bien structuré soit-il,
n'empêche personne d'exécuter l'action qu'il décrit. Une interruption
du graphe, si.

Le mécanisme repose sur `interrupt()` de LangGraph et sur le
checkpointer SQLite écrit en S4-J1. L'état complet — contexte,
hypothèse, vérification, plan, politique — est sérialisé à l'arrêt, et
l'exécution reprend exactement au nœud suspendu, pas au début.
"""
from __future__ import annotations

from langgraph.types import interrupt

from aegisops.agents.state import (
    ApprovalOutcome,
    ApprovalRequest,
    IncidentState,
)


def build_request(state: IncidentState) -> ApprovalRequest:
    """Assemble ce que l'humain va lire.

    Les trois catégories sont présentées, pas seulement les étapes en
    attente. Savoir qu'une action a été refusée par la politique — et
    pourquoi — fait partie de ce qui permet de décider sur les autres.
    """
    policy = state["policy"]
    hypothesis = state.get("hypothesis")
    alert = state["alert"]

    return ApprovalRequest(
        incident_id=alert.labels.get("incident_id", alert.fired_at.isoformat()),
        root_cause=hypothesis.root_cause if hypothesis else "(non établie)",
        confidence=hypothesis.confidence if hypothesis else 0.0,
        pending=policy.pending,
        allowed=policy.allowed,
        denied=policy.denied,
    )


def request_approval(state: IncidentState) -> dict:
    """Nœud du graphe : suspend l'exécution si une étape attend.

    Sans étape en attente, le nœud passe sans interrompre. Réveiller
    quelqu'un pour approuver une lecture de tableau de bord ferait
    ignorer les interruptions qui comptent.
    """
    policy = state.get("policy")

    if policy is None or not policy.pending:
        return {
            "approval": ApprovalOutcome(
                decided_by="system",
                comment="aucune étape ne requiert d'approbation",
            )
        }

    # `interrupt()` lève une exception interne à LangGraph. Le
    # checkpointer sérialise l'état, `invoke` retourne, et le processus
    # peut s'arrêter : la reprise se fera depuis la base, éventuellement
    # dans un autre processus, des heures plus tard.
    #
    # La valeur passée ici est celle que l'appelant recevra dans
    # `__interrupt__` — c'est le contrat entre le graphe et l'API.
    response = interrupt(build_request(state).model_dump(mode="json"))

    # Reprise : `response` est ce que l'API a transmis via
    # Command(resume=...).
    if isinstance(response, dict):
        return {"approval": ApprovalOutcome(**response)}

    # Une reprise mal formée ne doit pas valoir approbation. Le défaut
    # est le refus, comme pour l'absence de politique en J4.
    return {
        "approval": ApprovalOutcome(
            decided_by="unknown",
            rejected_orders=[d.order for d in policy.pending],
            comment=f"reprise non interprétable : {response!r}",
        )
    }
