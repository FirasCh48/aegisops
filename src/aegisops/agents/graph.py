"""Graphe d'investigation : assemblage des nœuds et de l'état.

Dix nœuds. Les six premiers observent et rassemblent sans conclure, le
septième raisonne, les trois derniers contrôlent : vérification des
preuves, politique d'exécution, approbation humaine.

Les nœuds sont produits par des fabriques qui reçoivent les clients à
utiliser. Cette indirection existe pour une raison précise : la rétention
de Prometheus est de six heures, et sans elle le pipeline ne serait
mesurable que sur une capture faite le jour même. Avec `clients`, les
mêmes nœuds tournent sur des clients de rejeu lisant une capture stockée
— ce qui est mesuré hors ligne est donc le pipeline de production, pas
une variante écrite pour le test.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

# LangGraph sérialise l'état en msgpack et refuse de désérialiser les
# types définis hors de ses modules connus.
#
# La variable d'environnement doit être posée avant l'import de
# langgraph : le sérialiseur la lit à l'initialisation du module.
os.environ.setdefault(
    "LANGGRAPH_ALLOWED_MSGPACK_MODULES", "aegisops.agents.state"
)

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.errors import GraphInterrupt  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from aegisops.agents.state import (  # noqa: E402
    Alert,
    IncidentContext,
    IncidentState,
)

ROOT = Path(__file__).resolve().parents[3]
CHECKPOINT_DB = ROOT / "data" / "checkpoints.sqlite"


def _allowed_msgpack_modules() -> list[tuple[str, str]]:
    """Déclare tous les modèles Pydantic de `state.py`.

    Dérivée du module plutôt que maintenue à la main.

    La liste écrite à la main avait été oubliée trois fois en deux
    sessions, et l'oubli ne se voit pas à l'écriture : la sérialisation
    réussit. C'est à la **relecture** d'un checkpoint que LangGraph
    refuse de désérialiser le type et renvoie un dictionnaire brut —
    l'appelant reçoit alors un `dict` là où il attend un modèle, et
    échoue sur le premier accès à une méthode.

    Le décalage entre la faute et son symptôme est ce qui rend cette
    erreur coûteuse : elle se manifeste dans l'API, pas dans le graphe.
    """
    from pydantic import BaseModel

    from aegisops.agents import state as state_module

    return [
        ("aegisops.agents.state", name)
        for name, obj in inspect.getmembers(state_module, inspect.isclass)
        if issubclass(obj, BaseModel) and obj is not BaseModel
    ]


ALLOWED_MSGPACK_MODULES = _allowed_msgpack_modules()

# Champs produits par les nœuds. Chacun doit être déclaré dans
# `IncidentState` : LangGraph écarte sans erreur ce qu'il ne connaît pas,
# et le nœud s'exécute pour rien.
PRODUCED_FIELDS = {
    "window", "metrics", "logs", "search_query", "passages", "context",
    "hypothesis", "verification", "plan", "policy", "approval",
}


def make_checkpointer(conn: sqlite3.Connection) -> SqliteSaver:
    """Construit le checkpointer en déclarant les types autorisés.

    L'API du sérialiseur a changé récemment et le nom du paramètre n'est
    pas stable d'une version à l'autre. Plutôt que de le supposer, on
    inspecte la signature : si le paramètre existe, on l'utilise ; sinon
    la variable d'environnement posée en tête de module prend le relais.
    """
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        params = inspect.signature(JsonPlusSerializer.__init__).parameters
        if "allowed_msgpack_modules" in params:
            serde = JsonPlusSerializer(
                allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES
            )
            return SqliteSaver(conn, serde=serde)
    except (ImportError, TypeError):
        pass

    return SqliteSaver(conn)


def timed(name: str, fn: Callable[[IncidentState], dict]) -> Callable:
    """Enveloppe un nœud pour journaliser sa durée et capturer ses erreurs.

    Une exception non interceptée interromprait le graphe entier. Ici
    elle est enregistrée dans `errors` et l'investigation continue avec
    ce qu'elle a : un agent qui échoue à lire les métriques peut encore
    produire un rapport à partir des logs, en le signalant. C'est le
    comportement attendu d'un système d'astreinte — une panne partielle
    vaut mieux qu'un silence.

    `GraphInterrupt` fait exception et doit remonter intacte. Ce n'est
    pas une erreur : c'est le mécanisme par lequel `interrupt()` suspend
    le graphe. L'attraper comme une panne de nœud la transformerait en
    échec silencieux — le graphe poursuivrait, et l'approbation humaine
    n'aurait jamais lieu.
    """

    def wrapper(state: IncidentState) -> dict:
        start = time.perf_counter()
        try:
            result = fn(state)
            elapsed = time.perf_counter() - start
            return {
                **result,
                "trace": [
                    *result.get("trace", []),
                    {"node": name, "ms": round(elapsed * 1000, 1), "status": "ok"},
                ],
            }
        except GraphInterrupt:
            # Signal de contrôle, pas erreur. LangGraph doit la recevoir
            # pour sérialiser l'état et rendre la main à l'appelant.
            raise
        except Exception as e:
            elapsed = time.perf_counter() - start
            return {
                "trace": [{"node": name, "ms": round(elapsed * 1000, 1),
                           "status": "error"}],
                "errors": [f"{name}: {type(e).__name__}: {e}"],
            }

    return wrapper


# --- Nœuds d'observation -------------------------------------------------

def _detect(clients: dict):
    """Délimite la fenêtre d'incident par recherche de changepoint."""

    def node(state: IncidentState) -> dict:
        from aegisops.agents.detector import WindowDetector

        detector = WindowDetector(client=clients.get("prometheus"))
        window, changepoints = detector.detect(state["alert"])
        return {
            "window": window,
            # Les changepoints bruts rejoignent la trace plutôt que
            # l'état : ils documentent une décision, ils ne servent pas
            # aux nœuds suivants.
            "trace": [{
                "node": "detect.signals",
                "found": [
                    {"signal": c.signal, "at": c.at.isoformat(), "z": c.z_score}
                    for c in changepoints
                ],
            }],
        }

    return node


def _metrics(clients: dict):
    """Interroge Prometheus sur la fenêtre et qualifie les écarts."""

    def node(state: IncidentState) -> dict:
        from aegisops.agents.metrics_agent import MetricsAgent

        agent = MetricsAgent(client=clients.get("prometheus"))
        return {
            "metrics": agent.observe(state["alert"].service, state["window"])
        }

    return node


def _logs(clients: dict):
    """Interroge Loki sur la fenêtre et extrait les motifs d'erreur."""

    def node(state: IncidentState) -> dict:
        from aegisops.agents.log_agent import LogAgent

        agent = LogAgent(client=clients.get("loki"))
        return {"logs": agent.observe(state["window"])}

    return node


def _collect(clients: dict):
    """Fusionne les observations et construit la requête de recherche."""

    def node(state: IncidentState) -> dict:
        from aegisops.agents.collector import collect

        return collect(state)

    return node


def _retrieve(clients: dict):
    """Recherche documentaire sur le corpus de postmortems.

    BM25 seul : mesuré en S3-J6 puis confirmé en S4-J5 sur corpus
    corrigé, il domine le dense et l'hybride sur les deux jeux
    d'évaluation (92,3 % contre 69,2 % et 80,8 %), pour 2,6 ms contre
    110 et 164 ms.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.state import RetrievedPassage
        from aegisops.rag.sparse import SparseIndex

        index = SparseIndex()
        index.load()
        search = getattr(index, "search_documents", index.search)
        hits = search(state["search_query"], limit=5)

        return {
            "passages": [
                RetrievedPassage(
                    chunk_id=h.chunk_id,
                    doc_id=h.doc_id,
                    score=h.score,
                    text=h.text,
                    origin=h.origin,
                )
                for h in hits
            ]
        }

    return node


def _assemble(clients: dict):
    """Rassemble le livrable d'observation. Ce nœud ne produit rien de
    nouveau : il fige ce que les précédents ont collecté."""

    def node(state: IncidentState) -> dict:
        return {
            "context": IncidentContext(
                alert=state["alert"],
                window=state["window"],
                metrics=state.get("metrics", []),
                logs=state.get("logs", []),
                search_query=state.get("search_query", ""),
                passages=state.get("passages", []),
            )
        }

    return node


# --- Nœud de raisonnement ------------------------------------------------

def _analyse(clients: dict):
    """Première inférence du pipeline.

    Les six nœuds précédents calculaient ; celui-ci raisonne. Sa sortie
    est contrainte — cause, confiance, preuves citées, hypothèses
    écartées — parce qu'une prose libre, même juste, ne serait pas
    vérifiable champ par champ.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.root_cause import RootCauseAgent

        return {"hypothesis": RootCauseAgent().analyse(state)}

    return node


# --- Nœuds de contrôle ---------------------------------------------------

def _verify(clients: dict):
    """Contrôle les preuves citées contre les observations collectées.

    Aucun appel LLM : la vérification doit être déterministe, et un
    modèle qui en contrôle un autre introduirait une circularité.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.verifier import verify

        return verify(state)

    return node


def _plan(clients: dict):
    """Propose un plan de remédiation à partir de l'hypothèse vérifiée.

    Ne reçoit que les preuves que le vérificateur a validées : fonder
    une action sur une preuve rejetée contournerait le contrôle.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.planner import plan

        return plan(state)

    return node


def _policy(clients: dict):
    """Applique la politique déclarative au plan proposé.

    Aucun appel LLM : un garde-fou fondé sur un modèle pourrait être
    convaincu par un prompt bien tourné.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.policy import apply_policy

        return apply_policy(state)

    return node


def _approval(clients: dict):
    """Suspend le graphe tant qu'une étape attend une validation.

    Seul nœud qui ne produit pas un résultat mais un arrêt.
    """

    def node(state: IncidentState) -> dict:
        from aegisops.agents.approval import request_approval

        return request_approval(state)

    return node


# --- Assemblage ----------------------------------------------------------

def build_graph(checkpointer=None, clients: dict | None = None):
    """Construit le graphe. Séquentiel pour l'instant.

    `clients` accepte les clés "prometheus" et "loki". Absentes, chaque
    agent instancie son client réel.
    """
    # Un champ renvoyé par un nœud mais absent de IncidentState est
    # silencieusement écarté par LangGraph. L'erreur est passée trois
    # fois — hypothesis en J1, verification en J2, policy en J4 — et ne
    # se voit qu'à l'affichage, plusieurs minutes plus tard.
    missing = PRODUCED_FIELDS - set(IncidentState.__annotations__)
    if missing:
        raise RuntimeError(
            f"champs absents de IncidentState : {sorted(missing)} — "
            "ils seraient écartés sans erreur à l'exécution"
        )

    clients = clients or {}
    graph = StateGraph(IncidentState)

    graph.add_node("detect", timed("detect", _detect(clients)))
    graph.add_node("metrics", timed("metrics", _metrics(clients)))
    graph.add_node("logs", timed("logs", _logs(clients)))
    graph.add_node("collect", timed("collect", _collect(clients)))
    graph.add_node("retrieve", timed("retrieve", _retrieve(clients)))
    graph.add_node("assemble", timed("assemble", _assemble(clients)))
    graph.add_node("analyse", timed("analyse", _analyse(clients)))
    graph.add_node("verify", timed("verify", _verify(clients)))
    graph.add_node("plan", timed("plan", _plan(clients)))
    graph.add_node("policy", timed("policy", _policy(clients)))
    graph.add_node("approval", timed("approval", _approval(clients)))

    graph.add_edge(START, "detect")
    graph.add_edge("detect", "metrics")
    graph.add_edge("metrics", "logs")
    graph.add_edge("logs", "collect")
    graph.add_edge("collect", "retrieve")
    graph.add_edge("retrieve", "assemble")
    graph.add_edge("assemble", "analyse")
    graph.add_edge("analyse", "verify")
    graph.add_edge("verify", "plan")
    graph.add_edge("plan", "policy")
    graph.add_edge("policy", "approval")
    graph.add_edge("approval", END)

    return graph.compile(checkpointer=checkpointer)


def investigate(
    alert: Alert,
    thread_id: str | None = None,
    clients: dict | None = None,
) -> IncidentState:
    """Point d'entrée. Chaque exécution est un thread distinct.

    Le checkpointer enregistre l'état après chaque nœud. Depuis l'ajout
    du nœud `approval`, cette persistance est devenue structurelle et non
    plus confortable : le graphe s'arrête réellement, et sa reprise se
    fait depuis la base — éventuellement dans un autre processus, des
    heures plus tard.
    """
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)

    app = build_graph(make_checkpointer(conn), clients)
    config = {"configurable": {"thread_id": thread_id or alert.fired_at.isoformat()}}

    try:
        return app.invoke({"alert": alert}, config=config)
    finally:
        conn.close()


def replay(incident_path: str, thread_id: str | None = None) -> IncidentState:
    """Rejoue une investigation sur une capture stockée.

    Même graphe, mêmes nœuds, clients différents. Permet de mesurer le
    pipeline sur les captures à tout moment, sans dépendre de la
    rétention de six heures de Prometheus ni d'un bac à sable actif.
    """
    import json
    from datetime import datetime

    from aegisops.tools.replay import ReplayLoki, ReplayPrometheus

    record = json.loads(Path(incident_path).read_text())
    injected = datetime.fromisoformat(record["window"]["injected_at"])
    cleared = datetime.fromisoformat(record["window"]["cleared_at"])

    # L'alerte est simulée au milieu de l'incident : c'est le moment où
    # un seuil de supervision se serait déclenché, les métriques ayant eu
    # le temps de franchir leur limite.
    alert = Alert(
        service=record["injected_service"],
        summary="simulated alert from stored capture",
        fired_at=injected + (cleared - injected) / 2,
        labels={"incident_id": record["incident_id"]},
    )

    return investigate(
        alert,
        # Horodaté : rejouer le même incident deux fois dans le même
        # thread ferait s'accumuler les traces, le réducteur `append`
        # ajoutant les nouvelles étapes aux anciennes. Et un thread déjà
        # terminé ne réexécuterait rien.
        thread_id=thread_id or f"replay-{record['incident_id']}-{time.time():.0f}",
        clients={
            "prometheus": ReplayPrometheus(incident_path),
            "loki": ReplayLoki(incident_path),
        },
    )


def main() -> None:
    import sys
    from datetime import datetime, timezone

    if len(sys.argv) > 1:
        result = replay(sys.argv[1])
        print(f"rejeu : {Path(sys.argv[1]).name}\n")
    else:
        alert = Alert(
            service="checkout-service",
            summary="error rate above 15% for 2 minutes",
            fired_at=datetime.now(timezone.utc),
            labels={"severity": "critical", "team": "platform"},
        )
        print(f"alerte : {alert.service} — {alert.summary}\n")
        result = investigate(
            alert, thread_id=f"demo-{alert.fired_at.timestamp():.0f}"
        )

    print("trace :")
    total = 0.0
    for step in result["trace"]:
        if "ms" not in step:
            continue
        total += step["ms"]
        flag = "" if step["status"] == "ok" else "  ÉCHEC"
        print(f"  {step['node']:<10} {step['ms']:>8.1f} ms{flag}")
    print(f"  {'total':<10} {total:>8.1f} ms")

    if result.get("errors"):
        print("\nerreurs :")
        for err in result["errors"]:
            print(f"  {err}")

    context = result.get("context")
    if context:
        print(f"\ncontexte : {context.summary()}")
        print(f"  fenêtre : {context.window.start.strftime('%H:%M:%S')} -> "
              f"{context.window.end.strftime('%H:%M:%S')}")
        print(f"  raison  : {context.window.reason}")

        anomalies = [m for m in context.metrics if m.anomalous]
        if anomalies:
            print(f"\nmétriques anormales ({len(anomalies)}/{len(context.metrics)}) :")
            for o in sorted(anomalies, key=lambda x: -(x.ratio or float("inf"))):
                dep = o.labels.get("dependency") or o.labels.get("reason") or ""
                tag = f"[{dep}]" if dep else ""
                ratio = f"×{o.ratio}" if o.ratio else "depuis 0"
                print(f"  {o.metric}{tag:<24} {o.baseline} -> {o.peak}  ({ratio})")

        if context.passages:
            print("\npassages :")
            for p in context.passages:
                print(f"  {p.score:>7.2f}  {p.chunk_id}")

    hypothesis = result.get("hypothesis")
    if hypothesis:
        print(f"\nhypothèse (confiance {hypothesis.confidence}) :")
        print(f"  {hypothesis.root_cause}")
        if hypothesis.parse_warning:
            print(f"  avertissement : {hypothesis.parse_warning}")
        if hypothesis.evidence:
            print("\n  preuves citées :")
            for e in hypothesis.evidence:
                print(f"    {e.source:<34} {e.observation}")
        if hypothesis.ruled_out:
            print("\n  hypothèses écartées :")
            for x in hypothesis.ruled_out:
                print(f"    {x}")

    verification = result.get("verification")
    if verification and verification.claims:
        print(f"\nvérification : {verification.summary()}")
        for c in verification.claims:
            mark = {"verified": "ok ", "misquoted": "!! ",
                    "unsupported": "XX "}[c.status]
            print(f"  {mark} {c.source:<36} {c.detail}")
        for r in verification.ruled_out_unsupported:
            print(f"  XX  hypothèse écartée sans source : {r[:70]}")

    remediation = result.get("plan")
    if remediation and remediation.steps:
        print(f"\nplan : {remediation.summary()}")
        if remediation.parse_warning:
            print(f"  avertissement : {remediation.parse_warning}")
        for s in remediation.steps:
            badge = {"read_only": "[lecture]", "reversible": "[réversible]",
                     "destructive": "[DESTRUCTIF]"}[s.risk]
            print(f"\n  {s.order}. {badge} {s.action}")
            if s.rationale:
                print(f"     {s.rationale}")
            if s.based_on:
                print(f"     fondé sur : {', '.join(s.based_on)}")
        if remediation.not_addressed:
            print(f"\n  non traité : {remediation.not_addressed}")

    policy = result.get("policy")
    if policy and policy.decisions:
        print(f"\npolitique (v{policy.policy_version}) : {policy.summary()}")
        for d in policy.decisions:
            mark = {"allow": "   ", "require_approval": ">> ",
                    "deny": "XX "}[d.decision]
            print(f"  {mark}{d.order}. [{d.risk}] {d.action[:64]}")
            for r in d.reasons:
                print(f"       {r}")

    if "__interrupt__" in result:
        print("\n>> graphe suspendu : approbation humaine requise")
        print("   reprendre via POST /incidents/{id}/approve")


if __name__ == "__main__":
    main()