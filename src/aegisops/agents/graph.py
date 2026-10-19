"""Graphe d'investigation : assemblage des nœuds et de l'état.

Cette version ne contient aucun appel LLM. Les six nœuds sont câblés et
renvoient des données figées, pour vérifier que l'état circule, que les
réducteurs fusionnent correctement et que le checkpointer reprend bien.

Chaque nœud sera remplacé par sa version réelle au fil de la semaine.
Procéder dans l'ordre inverse — écrire les agents puis les câbler —
rendrait impossible de distinguer une erreur de graphe d'une erreur de
prompt, et chaque itération coûterait plusieurs minutes d'appels LLM.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable

# LangGraph sérialise l'état en msgpack et refuse de désérialiser les
# types définis hors de ses modules connus. Aujourd'hui c'est un
# avertissement ; la documentation annonce un blocage dans une version
# future, ce qui rendrait illisibles les checkpoints déjà écrits.
#
# La variable d'environnement doit être posée avant l'import de
# langgraph : le sérialiseur la lit à l'initialisation du module.
os.environ.setdefault(
    "LANGGRAPH_ALLOWED_MSGPACK_MODULES", "aegisops.agents.state"
)

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from aegisops.agents.state import (  # noqa: E402
    Alert,
    IncidentContext,
    IncidentState,
    TimeWindow,
)

ROOT = Path(__file__).resolve().parents[3]
CHECKPOINT_DB = ROOT / "data" / "checkpoints.sqlite"

# Les types Pydantic que le graphe fait transiter par l'état.
ALLOWED_MSGPACK_MODULES = [
    ("aegisops.agents.state", "Alert"),
    ("aegisops.agents.state", "TimeWindow"),
    ("aegisops.agents.state", "MetricObservation"),
    ("aegisops.agents.state", "LogObservation"),
    ("aegisops.agents.state", "RetrievedPassage"),
    ("aegisops.agents.state", "IncidentContext"),
]


def make_checkpointer(conn: sqlite3.Connection) -> SqliteSaver:
    """Construit le checkpointer en déclarant les types autorisés.

    L'API du sérialiseur a changé récemment et le nom du paramètre n'est
    pas stable d'une version à l'autre. Plutôt que de le supposer, on
    inspecte la signature : si le paramètre existe, on l'utilise ; sinon
    la variable d'environnement posée en tête de module prend le relais,
    et le checkpointer reste fonctionnel dans les deux cas.
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
    produire un rapport à partir des logs, en le signalant.
    """

    def wrapper(state: IncidentState) -> dict:
        start = time.perf_counter()
        try:
            result = fn(state)
            elapsed = time.perf_counter() - start
            return {
                **result,
                "trace": [{"node": name, "ms": round(elapsed * 1000, 1),
                           "status": "ok"}],
            }
        except Exception as e:
            elapsed = time.perf_counter() - start
            return {
                "trace": [{"node": name, "ms": round(elapsed * 1000, 1),
                           "status": "error"}],
                "errors": [f"{name}: {type(e).__name__}: {e}"],
            }

    return wrapper


# --- Nœuds provisoires ---------------------------------------------------
# Chacun sera remplacé par sa version réelle : detector en J2, metrics en
# J3, logs en J4, collector en J5.

def detect(state: IncidentState) -> dict:
    """Délimite la fenêtre d'incident par recherche de changepoint."""
    from aegisops.agents.detector import WindowDetector

    window, changepoints = WindowDetector().detect(state["alert"])
    return {
        "window": window,
        # Les changepoints bruts rejoignent la trace plutôt que l'état :
        # ils documentent une décision, ils ne servent pas aux nœuds
        # suivants.
        "trace": [{
            "node": "detect.signals",
            "found": [
                {"signal": c.signal, "at": c.at.isoformat(), "z": c.z_score}
                for c in changepoints
            ],
        }],
    }


def observe_metrics(state: IncidentState) -> dict:
    """Interroge Prometheus sur la fenêtre et qualifie les écarts."""
    from aegisops.agents.metrics_agent import MetricsAgent

    observations = MetricsAgent().observe(
        state["alert"].service, state["window"]
    )
    return {"metrics": observations}


def observe_logs(state: IncidentState) -> dict:
    """Interroge Loki sur la fenêtre et extrait les motifs."""
    from aegisops.agents.log_agent import LogAgent

    return {"logs": LogAgent().observe(state["window"])}


def collect(state: IncidentState) -> dict:
    """J5 : fusionnera les observations et construira la requête RAG."""
    return {"search_query": ""}


def retrieve(state: IncidentState) -> dict:
    """J5 : appellera rag.search() avec la requête construite."""
    return {"passages": []}


def assemble(state: IncidentState) -> dict:
    """Assemble le livrable. Ce nœud restera inchangé : il ne fait que
    rassembler ce que les autres ont produit."""
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


# --- Assemblage ----------------------------------------------------------

def build_graph(checkpointer=None):
    """Construit le graphe. Séquentiel pour l'instant.

    Les nœuds métriques et logs sont indépendants et pourront être
    parallélisés — mais tant que leurs durées ne sont pas mesurées, le
    faire serait optimiser à l'aveugle.
    """
    graph = StateGraph(IncidentState)

    graph.add_node("detect", timed("detect", detect))
    graph.add_node("metrics", timed("metrics", observe_metrics))
    graph.add_node("logs", timed("logs", observe_logs))
    graph.add_node("collect", timed("collect", collect))
    graph.add_node("retrieve", timed("retrieve", retrieve))
    graph.add_node("assemble", timed("assemble", assemble))

    graph.add_edge(START, "detect")
    graph.add_edge("detect", "metrics")
    graph.add_edge("metrics", "logs")
    graph.add_edge("logs", "collect")
    graph.add_edge("collect", "retrieve")
    graph.add_edge("retrieve", "assemble")
    graph.add_edge("assemble", END)

    return graph.compile(checkpointer=checkpointer)


def investigate(alert: Alert, thread_id: str | None = None) -> IncidentState:
    """Point d'entrée. Chaque exécution est un thread distinct.

    Le checkpointer enregistre l'état après chaque nœud. Sur CPU, une
    investigation complète prendra plusieurs minutes à partir de S5 :
    pouvoir reprendre après le nœud qui a échoué, plutôt que tout
    relancer, change le coût d'une itération de développement.
    """
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)

    app = build_graph(make_checkpointer(conn))
    config = {"configurable": {"thread_id": thread_id or alert.fired_at.isoformat()}}

    try:
        return app.invoke({"alert": alert}, config=config)
    finally:
        conn.close()


def main() -> None:
    from datetime import datetime, timezone

    alert = Alert(
        service="checkout-service",
        summary="error rate above 15% for 2 minutes",
        fired_at=datetime.now(timezone.utc),
        labels={"severity": "critical", "team": "platform"},
    )

    print(f"alerte : {alert.service} — {alert.summary}\n")
    result = investigate(alert, thread_id="demo")

    print("trace :")
    total = 0.0
    for step in result["trace"]:
        total += step["ms"]
        flag = "" if step["status"] == "ok" else "  ÉCHEC"
        print(f"  {step['node']:<10} {step['ms']:>7.1f} ms{flag}")
    print(f"  {'total':<10} {total:>7.1f} ms")

    if result.get("errors"):
        print("\nerreurs :")
        for err in result["errors"]:
            print(f"  {err}")

        context = result["context"]
        print(f"\ncontexte : {context.summary()}")
        print(f"  fenêtre : {context.window.start.strftime('%H:%M:%S')} -> "
          f"{context.window.end.strftime('%H:%M:%S')}")
        print(f"  raison  : {context.window.reason}")

        anomalies = [m for m in context.metrics if m.anomalous]
        if anomalies:
            print(f"\nmétriques anormales ({len(anomalies)}/{len(context.metrics)}) :")
            for o in sorted(anomalies, key=lambda x: -(x.ratio or 0)):
                dep = o.labels.get("dependency") or o.labels.get("reason") or ""
                tag = f"[{dep}]" if dep else ""
                ratio = f"×{o.ratio}" if o.ratio else "depuis 0"
                print(f"  {o.metric}{tag:<22} {o.baseline} -> {o.peak}  ({ratio})")


if __name__ == "__main__":
    main()