"""Doubles et fixtures partagés.

Le graphe contient trois appels LLM. Les laisser dans les tests aurait
trois conséquences : treize secondes par exécution, une dépendance au
réseau et à une clé d'API, et surtout une non-reproductibilité — mesuré
en S5-J1, deux appels sur la même entrée ont donné 0,86 puis 0,78 de
confiance.

Un double déterministe règle les trois. Et il déplace ce que le test
mesure : avec un vrai modèle, un échec peut venir du graphe comme du
modèle ; avec un double, il vient du graphe.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aegisops.agents.state import (
    Alert,
    IncidentContext,
    LogObservation,
    MetricObservation,
    RetrievedPassage,
    TimeWindow,
)
from aegisops.core.llm import LLMProvider


class ScriptedLLM(LLMProvider):
    """Fournisseur qui renvoie des réponses préparées, dans l'ordre.

    Enregistre les prompts reçus : plusieurs tests vérifient non pas la
    sortie du graphe mais ce qui a été transmis au modèle — par exemple
    qu'une preuve rejetée par le vérificateur n'atteint pas le prompt du
    planificateur.
    """

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[tuple[str, str]] = []

    def complete(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        self.prompts.append((system, prompt))
        if not self.responses:
            raise AssertionError(
                f"{len(self.prompts)} appels au modèle, "
                "mais plus de réponse préparée"
            )
        return self.responses.pop(0)


@pytest.fixture
def scripted():
    """Fabrique de fournisseurs scriptés.

    Exposée en fixture plutôt qu'importée depuis les tests : pytest
    charge `conftest.py` automatiquement, et l'importer exigerait de
    faire du dossier un package — complication sans contrepartie.
    """
    return ScriptedLLM


@pytest.fixture
def window() -> TimeWindow:
    start = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
    return TimeWindow(
        start=start,
        end=start + timedelta(minutes=5),
        reason="fenêtre de test",
    )


@pytest.fixture
def alert() -> Alert:
    return Alert(
        service="checkout-service",
        summary="error rate above 15%",
        fired_at=datetime(2026, 10, 19, 10, 2, tzinfo=timezone.utc),
    )


@pytest.fixture
def context(alert, window) -> IncidentContext:
    """Un contexte minimal mais réaliste.

    Une métrique anormale et une normale : la seconde permet de tester
    que les signaux inchangés restent disponibles, ce dont dépend la
    capacité à écarter des hypothèses.
    """
    return IncidentContext(
        alert=alert,
        window=window,
        metrics=[
            MetricObservation(
                metric="db_pool_usage",
                labels={"service": "checkout-service"},
                baseline=0.0, peak=0.87, ratio=None, anomalous=True,
            ),
            MetricObservation(
                metric="memory_mb",
                labels={"service": "checkout-service"},
                baseline=90.0, peak=93.0, ratio=1.03, anomalous=False,
            ),
        ],
        logs=[
            LogObservation(service="checkout-service", event="db_pool_timeout",
                           count=777, sample={"pool_size": 10}),
            LogObservation(service="checkout-service", event="order_created",
                           count=176),
        ],
        search_query="incident on checkout-service\ndb_pool_usage 0.0 to 0.87",
        passages=[
            RetrievedPassage(
                chunk_id="sandbox/connection_leak.md#3-0",
                doc_id="sandbox/connection_leak.md",
                score=29.7,
                text="Connection pool exhaustion caused by a leak.",
                origin="sandbox",
            )
        ],
    )


@pytest.fixture
def hypothesis_json() -> str:
    return json.dumps({
        "root_cause": "connection leak exhausting the pool",
        "confidence": 0.85,
        "evidence": [
            {"source": "metric:db_pool_usage",
             "observation": "rose from 0.0 to 0.87",
             "supports": "the pool is saturated"},
            {"source": "log:db_pool_timeout",
             "observation": "777 timeout events",
             "supports": "requests fail waiting for a connection"},
        ],
        "ruled_out": ["memory_leak: memory_mb stayed at 90.0 to 93.0"],
    })


@pytest.fixture
def plan_json() -> str:
    return json.dumps({
        "steps": [
            {"order": 1, "action": "Check db_pool_usage on the dashboard",
             "risk": "read_only", "rationale": "confirm it is ongoing",
             "based_on": ["metric:db_pool_usage"]},
            {"order": 2, "action": "Restart checkout-service",
             "risk": "reversible", "rationale": "release held connections",
             "based_on": ["metric:db_pool_usage", "log:db_pool_timeout"]},
        ],
        "not_addressed": "the underlying code path is not identified",
    })