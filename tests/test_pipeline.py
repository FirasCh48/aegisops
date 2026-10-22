"""Tests du pipeline de raisonnement.

Chaque test vérifie une garantie que le code prétend offrir, pas un
comportement incidentel. L'ordre suit la chaîne : parsing, vérification,
politique, et les liens entre eux.

Plusieurs tests portent le nom de la session où le défaut qu'ils
couvrent a été découvert — un test écrit après coup protège d'une
régression, mais c'est son origine qui dit pourquoi il existe.
"""
from __future__ import annotations

import pytest

from aegisops.agents.parsing import parse_structured
from aegisops.agents.planner import RemediationPlanner, build_prompt
from aegisops.agents.policy import PolicyEngine
from aegisops.agents.root_cause import RootCauseAgent
from aegisops.agents.state import (
    Evidence,
    RemediationPlan,
    RemediationStep,
    RootCauseHypothesis,
    VerificationReport,
    VerifiedClaim,
)
from aegisops.agents.verifier import Verifier


# --- Parsing -------------------------------------------------------------

@pytest.mark.parametrize("wrapper", [
    "{body}",
    "```json\n{body}\n```",
    "Here is my analysis:\n\n{body}\n\nLet me know if you need more.",
])
def test_parser_recovers_from_common_wrappers(wrapper, hypothesis_json):
    """Les trois formes observées chez les modèles de petite taille."""
    parsed, warning = parse_structured(
        wrapper.format(body=hypothesis_json), RootCauseHypothesis
    )

    assert parsed is not None
    assert parsed.confidence == 0.85
    assert len(parsed.evidence) == 2


def test_parser_refuses_truncated_json(hypothesis_json):
    """Un JSON coupé doit être refusé, pas complété.

    Observé en S5-J3 : la sortie du planificateur était tronquée en
    milieu de phrase, faute de tokens. Un parseur permissif aurait livré
    un plan amputé de deux étapes sans que rien ne le signale.
    """
    truncated = hypothesis_json[: len(hypothesis_json) // 2]
    parsed, warning = parse_structured(truncated, RootCauseHypothesis)

    assert parsed is None
    assert "exploitable" in warning


def test_parser_repairs_trailing_comma():
    """Défaut syntaxique fréquent, récupérable sans ambiguïté."""
    text = '{"root_cause": "x", "confidence": 0.5, "evidence": [],}'
    parsed, warning = parse_structured(text, RootCauseHypothesis)

    assert parsed is not None
    assert "réparation" in warning


# --- Vérification --------------------------------------------------------

def test_verifier_accepts_real_evidence(context):
    hypothesis = RootCauseHypothesis(
        root_cause="x", confidence=0.8,
        evidence=[Evidence(source="metric:db_pool_usage",
                           observation="rose to 0.87")],
    )
    report = Verifier().verify(hypothesis, context)

    assert report.is_clean
    assert report.claims[0].status == "verified"


def test_verifier_flags_invented_source(context):
    """Une source absente du contexte est une hallucination."""
    hypothesis = RootCauseHypothesis(
        root_cause="x", confidence=0.9,
        evidence=[Evidence(source="metric:gc_pause_seconds",
                           observation="rose to 4.2")],
    )
    report = Verifier().verify(hypothesis, context)

    assert report.unsupported_count == 1
    assert report.claims[0].status == "unsupported"


def test_verifier_flags_wrong_values(context):
    """Une source réelle citée avec des valeurs fausses."""
    hypothesis = RootCauseHypothesis(
        root_cause="x", confidence=0.9,
        evidence=[Evidence(source="log:db_pool_timeout",
                           observation="9999 events")],
    )
    report = Verifier().verify(hypothesis, context)

    assert report.misquoted_count == 1


def test_verifier_ignores_qualifier_values(context):
    """Régression de S5-J3 : « pool_size=10 » n'est pas une mesure.

    Le vérificateur accusait à tort une preuve exacte parce qu'il
    comptait les valeurs des champs qualifiants comme des citations de
    métriques. Deux preuves sur six étaient signalées à tort, et un
    contrôle qui crie à tort perd sa valeur d'alerte.
    """
    hypothesis = RootCauseHypothesis(
        root_cause="x", confidence=0.9,
        evidence=[Evidence(source="log:db_pool_timeout",
                           observation="777 events (pool_size=10, version=v2.7)")],
    )
    report = Verifier().verify(hypothesis, context)

    assert report.is_clean, report.claims[0].detail


# --- Politique -----------------------------------------------------------

def test_policy_allows_read_only_with_evidence():
    plan = RemediationPlan(steps=[
        RemediationStep(order=1, action="Check the dashboard",
                        risk="read_only", based_on=["metric:db_pool_usage"]),
    ])
    verification = VerificationReport(claims=[
        VerifiedClaim(source="metric:db_pool_usage", status="verified"),
    ])
    report = PolicyEngine().evaluate(
        plan, RootCauseHypothesis(root_cause="x", confidence=0.9), verification
    )

    assert report.decisions[0].decision == "allow"


def test_policy_denies_action_without_evidence():
    plan = RemediationPlan(steps=[
        RemediationStep(order=1, action="Restart the service",
                        risk="reversible", based_on=[]),
    ])
    report = PolicyEngine().evaluate(
        plan, RootCauseHypothesis(root_cause="x", confidence=0.9),
        VerificationReport(),
    )

    assert report.decisions[0].decision == "deny"


def test_policy_denies_action_on_rejected_evidence():
    """La chaîne complète : le vérificateur rejette, la politique refuse.

    C'est la garantie que les trois nœuds forment un contrôle de bout en
    bout et non trois rapports indépendants.
    """
    plan = RemediationPlan(steps=[
        RemediationStep(order=1, action="Scale up the pool",
                        risk="reversible",
                        based_on=["metric:gc_pause_seconds"]),
    ])
    verification = VerificationReport(claims=[
        VerifiedClaim(source="metric:gc_pause_seconds", status="unsupported"),
    ])
    report = PolicyEngine().evaluate(
        plan, RootCauseHypothesis(root_cause="x", confidence=0.9), verification
    )

    assert report.decisions[0].decision == "deny"


def test_policy_requalifies_destructive_keyword():
    """Un mot-clé destructif requalifie une étape annoncée réversible."""
    plan = RemediationPlan(steps=[
        RemediationStep(order=1, action="Drop the stale connection table",
                        risk="reversible", based_on=["metric:db_pool_usage"]),
    ])
    verification = VerificationReport(claims=[
        VerifiedClaim(source="metric:db_pool_usage", status="verified"),
    ])
    report = PolicyEngine().evaluate(
        plan, RootCauseHypothesis(root_cause="x", confidence=0.9), verification
    )

    assert report.decisions[0].risk == "destructive"
    assert "drop" in report.decisions[0].reasons[0]


def test_policy_rules_only_harden():
    """Propriété structurelle : aucune règle ne peut autoriser.

    Une étape `destructive` sans preuve ne doit pas finir `allow`, quel
    que soit l'ordre d'évaluation des règles.
    """
    plan = RemediationPlan(steps=[
        RemediationStep(order=1, action="Purge the queue",
                        risk="destructive", based_on=[]),
    ])
    report = PolicyEngine().evaluate(
        plan, RootCauseHypothesis(root_cause="x", confidence=0.95),
        VerificationReport(),
    )

    assert report.decisions[0].decision == "deny"


# --- Liens entre nœuds ---------------------------------------------------

def test_planner_receives_only_verified_evidence(context):
    """Une preuve rejetée ne doit pas atteindre le prompt du planificateur.

    Sans cette garantie, le nœud de vérification n'aurait qu'un effet
    décoratif : le planificateur fonderait une action sur ce qui vient
    d'être rejeté.
    """
    state = {
        "context": context,
        "hypothesis": RootCauseHypothesis(
            root_cause="x", confidence=0.8,
            evidence=[
                Evidence(source="metric:db_pool_usage", observation="rose"),
                Evidence(source="metric:gc_pause_seconds", observation="rose"),
            ],
        ),
        "verification": VerificationReport(claims=[
            VerifiedClaim(source="metric:db_pool_usage", status="verified"),
            VerifiedClaim(source="metric:gc_pause_seconds",
                          status="unsupported"),
        ]),
    }

    prompt = build_prompt(state)

    assert "db_pool_usage" in prompt
    assert "gc_pause_seconds" not in prompt


def test_root_cause_agent_degrades_without_crashing(context, scripted):
    """Une sortie illisible produit une hypothèse vide, pas une exception.

    Le contexte collecté reste exploitable par un humain ; échouer
    silencieusement ou lever serait pire.
    """
    agent = RootCauseAgent(provider=scripted(["I cannot answer that."]))
    hypothesis = agent.analyse({"context": context})

    assert hypothesis.confidence == 0.0
    assert hypothesis.parse_warning


def test_planner_refuses_without_root_cause(scripted):
    """Sans cause établie, aucun plan n'a de sens.

    Et le modèle ne doit pas être appelé : une requête inutile coûte
    plusieurs secondes et des tokens, pour une sortie qu'on écartera.
    """
    planner = RemediationPlanner(provider=scripted([]))
    plan = planner.plan({
        "hypothesis": RootCauseHypothesis(root_cause="", confidence=0.0),
    })

    assert plan.steps == []
    assert planner.provider.prompts == []