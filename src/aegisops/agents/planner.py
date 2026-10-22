"""Remediation Planner : propose un plan, n'exécute rien.

Troisième nœud d'inférence. Il reçoit l'hypothèse vérifiée et produit
des étapes ordonnées, chacune classée par niveau de risque.

Ce classement n'est pas décoratif : il est le seul critère dont
disposera le policy engine en J4 pour décider ce qui passe et ce qui
demande une approbation. Un modèle qui classerait mal — « redémarrer le
service » annoncé en `read_only` — rendrait le garde-fou inopérant.

C'est pourquoi le prompt donne des exemples de chaque niveau plutôt
qu'une définition : un modèle de petite taille applique mieux une
analogie qu'une règle abstraite.
"""
from __future__ import annotations

from aegisops.agents.parsing import parse_structured
from aegisops.agents.state import IncidentState, RemediationPlan
from aegisops.core.llm import LLMProvider, get_llm

# Un plan de quatre à cinq étapes, chacune avec sa justification et ses
# sources, dépasse largement les 1 200 tokens accordés au Root Cause
# Agent. Mesuré : la sortie était tronquée en milieu de phrase, et le
# parseur refusait — correctement — un JSON incomplet.
MAX_TOKENS = 2500

SYSTEM = """You are an SRE writing a remediation plan for an incident \
whose root cause has been established.

You propose actions. You never execute anything, and you never write \
commands — a human reads your plan and decides.

Classify every step by risk:
- read_only: observing only. Reading logs, checking a dashboard, running \
a profiler, querying a read replica.
- reversible: changes state but can be undone. Restarting a service, \
rolling back a version, raising a limit, disabling a feature flag.
- destructive: data loss or no way back. Dropping data, purging a queue, \
failing over without a return path, deleting resources.

If unsure between two levels, choose the higher one.

Rules:
- Order steps so that the incident stops first, then the cause is fixed, \
then recurrence is prevented.
- Each step must cite which evidence justifies it, using the same source \
names as the hypothesis.
- Keep rationale to one short sentence. Four to five steps is enough.
- Name what your plan does not address. A plan that claims to cover \
everything is less useful than one that states its blind spots.
- Answer with a single JSON object, no prose before or after."""

TEMPLATE = """## ROOT CAUSE

{root_cause}

Confidence: {confidence}

## VERIFIED EVIDENCE

{evidence}

## RULED OUT

{ruled_out}

## OUTPUT FORMAT

{{
  "steps": [
    {{"order": 1,
      "action": "what to do, in one sentence",
      "risk": "read_only | reversible | destructive",
      "rationale": "one short sentence",
      "based_on": ["metric:<name>", "log:<event>"]}}
  ],
  "not_addressed": "what this plan leaves open"
}}"""


def build_prompt(state: IncidentState) -> str:
    hypothesis = state["hypothesis"]
    verification = state.get("verification")

    # Seules les preuves vérifiées sont transmises. Une preuve marquée
    # `unsupported` ou `misquoted` ne doit pas fonder une action : c'est
    # la raison d'être du nœud de vérification, et l'ignorer ici
    # reviendrait à la contourner.
    verified = set()
    if verification:
        verified = {
            c.source for c in verification.claims if c.status == "verified"
        }

    lines = [
        f"- {e.source}: {e.observation}"
        for e in hypothesis.evidence
        if not verified or e.source in verified
    ]

    return TEMPLATE.format(
        root_cause=hypothesis.root_cause,
        confidence=hypothesis.confidence,
        evidence="\n".join(lines) or "(no verified evidence)",
        ruled_out="\n".join(f"- {r}" for r in hypothesis.ruled_out) or "(none)",
    )


class RemediationPlanner:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_llm()

    def plan(self, state: IncidentState) -> RemediationPlan:
        hypothesis = state.get("hypothesis")

        # Sans cause établie, aucun plan n'a de sens. Renvoyer un plan
        # vide est plus honnête que d'en inventer un à partir de rien.
        if hypothesis is None or hypothesis.confidence == 0.0:
            return RemediationPlan(
                not_addressed="aucune cause racine établie : pas de plan",
            )

        raw = self.provider.complete(
            build_prompt(state), system=SYSTEM, max_tokens=MAX_TOKENS
        )
        plan, warning = parse_structured(raw, RemediationPlan)

        if plan is None:
            return RemediationPlan(
                not_addressed="(plan illisible)",
                parse_warning=f"{warning} | sortie brute : {raw[:300]}",
            )

        plan.parse_warning = warning
        return _normalise(plan)


def _normalise(plan: RemediationPlan) -> RemediationPlan:
    """Corrige les écarts de forme les plus fréquents.

    Un niveau de risque inconnu est ramené à `destructive`, pas ignoré :
    une étape que le planificateur n'a pas su classer doit passer par
    l'approbation humaine, pas la contourner par défaut. C'est le même
    principe que « en cas de doute, choisis le niveau supérieur » donné
    au modèle — appliqué ici au cas où il ne l'aurait pas suivi.
    """
    valid = {"read_only", "reversible", "destructive"}

    for step in plan.steps:
        level = step.risk.strip().lower().replace("-", "_").replace(" ", "_")
        if level not in valid:
            step.rationale = (
                f"{step.rationale} [risque « {step.risk} » non reconnu, "
                "classé destructive par défaut]"
            ).strip()
            level = "destructive"
        step.risk = level

    plan.steps.sort(key=lambda s: s.order)
    return plan


def plan(state: IncidentState) -> dict:
    """Nœud du graphe."""
    return {"plan": RemediationPlanner().plan(state)}