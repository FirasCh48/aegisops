"""Root Cause Agent : première inférence du pipeline.

Tous les nœuds précédents calculaient. Celui-ci raisonne, et c'est
pourquoi sa sortie est contrainte : un champ `root_cause`, une
confiance, une liste de preuves citées, une liste d'hypothèses écartées.

La contrainte n'est pas cosmétique. Elle rend le rapport vérifiable
champ par champ en J2 : chaque preuve citée doit correspondre à une
observation réellement présente dans le contexte. Une prose libre, même
juste, ne permettrait pas ce contrôle.
"""
from __future__ import annotations

from aegisops.agents.collector import build_summary
from aegisops.agents.parsing import parse_structured
from aegisops.agents.state import IncidentState, RootCauseHypothesis
from aegisops.core.llm import LLMProvider
from aegisops.core.llm import LLMProvider, get_llm

SYSTEM = """You are an SRE incident analyst. You receive observations \
collected from metrics and logs, plus excerpts from past postmortems.

Your task is to name the root cause and cite the evidence for it.

Rules:
- Cite only observations present in the EVIDENCE section. Never invent a \
metric name, a log event, or a number.
- A signal that did NOT change is evidence too: it rules out hypotheses.
- If the evidence is insufficient, say so and give a low confidence. An \
honest "insufficient evidence" is more useful than a confident guess.
- Answer with a single JSON object, no prose before or after."""

TEMPLATE = """## EVIDENCE

{summary}

## SIMILAR PAST INCIDENTS

{passages}

## OUTPUT FORMAT

{{
  "root_cause": "one sentence naming the mechanism, not the symptom",
  "confidence": 0.0 to 1.0,
  "evidence": [
    {{"source": "metric:<name> or log:<event>",
      "observation": "what it shows",
      "supports": "which part of the root cause"}}
  ],
  "ruled_out": ["hypothesis and what eliminates it"]
}}"""

# Quatre passages suffisent : au-delà, le contexte d'un modèle de 1,5 Md
# de paramètres se dégrade avant d'être plein. Mesuré en S3 : le document
# attendu est au rang 1 ou 2 dans tous les cas utiles.
MAX_PASSAGES = 4
# Les passages sont tronqués : la section « Discriminating signal » d'un
# postmortem tient en quelques lignes, le reste est du contexte.
PASSAGE_CHARS = 700


def build_prompt(state: IncidentState) -> str:
    context = state["context"]
    summary = build_summary(
        context.alert.service, context.window, context.metrics, context.logs
    )

    blocks = []
    for p in context.passages[:MAX_PASSAGES]:
        label = p.doc_id.replace("sandbox/", "").replace(".md", "")
        blocks.append(f"### {label}\n{p.text[:PASSAGE_CHARS]}")

    return TEMPLATE.format(
        summary=summary,
        passages="\n\n".join(blocks) or "(no similar incident found)",
    )


class RootCauseAgent:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        # `get_llm` lit LLM_PROVIDER dans la configuration : local pour
        # Qwen2.5 via Ollama, groq ou gemini pour le développement.
        # L'injection reste possible pour les tests, où le provider est
        # remplacé par un double (J6, pytest avec LLM mocké).
        self.provider = provider or get_llm()

    def analyse(self, state: IncidentState) -> RootCauseHypothesis:
        prompt = build_prompt(state)
        raw = self.provider.complete(prompt, system=SYSTEM, max_tokens=1200)

        hypothesis, warning = parse_structured(raw, RootCauseHypothesis)

        if hypothesis is None:
            # Le graphe continue : une investigation sans hypothèse reste
            # exploitable — le contexte collecté est intact et un humain
            # peut le lire. Échouer silencieusement serait pire.
            return RootCauseHypothesis(
                root_cause="(aucune hypothèse exploitable produite)",
                confidence=0.0,
                parse_warning=f"{warning} | sortie brute : {raw[:200]}",
            )

        hypothesis.parse_warning = warning
        return hypothesis


def analyse(state: IncidentState) -> dict:
    """Nœud du graphe."""
    return {"hypothesis": RootCauseAgent().analyse(state)}