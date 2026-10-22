"""Policy engine : applique une politique déclarative au plan proposé.

Quatrième et dernier contrôle avant qu'un plan n'atteigne un humain. Il
ne raisonne pas et n'appelle aucun modèle : il compare le niveau de
risque déclaré par le planificateur aux seuils d'un fichier YAML.

Un garde-fou fondé sur un modèle pourrait être convaincu par un prompt
bien tourné. Celui-ci ne peut pas : il lit un fichier versionné dont
l'historique montre qui a assoupli quoi, et quand.

Les règles ne peuvent que **durcir** une décision. Une action autorisée
par son niveau de risque peut être bloquée par une règle ; l'inverse est
impossible. Cette asymétrie est ce qui rend la politique lisible : il
suffit de lire les niveaux pour connaître le plafond, les règles ne
peuvent que l'abaisser.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from aegisops.agents.state import (
    PolicyDecision,
    PolicyReport,
    RemediationPlan,
    RootCauseHypothesis,
    VerificationReport,
)

ROOT = Path(__file__).resolve().parents[3]
POLICY_FILE = ROOT / "config" / "policy.yaml"

# Ordre de sévérité : une règle ne peut déplacer une décision que vers la
# droite. Encodé plutôt que laissé implicite — c'est la garantie que les
# règles ne peuvent pas assouplir.
SEVERITY = {"allow": 0, "require_approval": 1, "deny": 2}


def _harden(current: str, proposed: str) -> str:
    return proposed if SEVERITY[proposed] > SEVERITY[current] else current


class PolicyEngine:
    def __init__(self, policy_path: Path | None = None) -> None:
        path = policy_path or POLICY_FILE
        if not path.exists():
            # Pas de politique, pas d'exécution. L'absence du fichier ne
            # doit pas valoir autorisation — c'est le défaut le plus
            # dangereux qu'un moteur de politique puisse avoir.
            raise RuntimeError(
                f"politique absente : {path}. Aucune action ne peut être "
                "évaluée sans règles explicites."
            )
        self.policy = yaml.safe_load(path.read_text())

    def evaluate(
        self,
        plan: RemediationPlan,
        hypothesis: RootCauseHypothesis | None,
        verification: VerificationReport | None,
    ) -> PolicyReport:
        levels = self.policy["risk_levels"]
        rules = self.policy.get("rules", [])
        keywords = [k.lower() for k in self.policy.get("destructive_keywords", [])]

        verified = set()
        if verification:
            verified = {
                c.source for c in verification.claims if c.status == "verified"
            }

        confidence = hypothesis.confidence if hypothesis else 0.0
        decisions: list[PolicyDecision] = []

        for step in plan.steps:
            risk = step.risk
            reasons: list[str] = []

            # Filet de sécurité : un mot-clé destructif dans l'action
            # requalifie l'étape avant toute autre évaluation.
            hit = next((k for k in keywords if k in step.action.lower()), None)
            if hit and risk != "destructive":
                reasons.append(
                    f"requalifiée destructive : l'action contient « {hit} » "
                    f"alors qu'elle était annoncée {risk}"
                )
                risk = "destructive"

            decision = levels.get(risk, "deny")
            if risk not in levels:
                reasons.append(f"niveau « {risk} » absent de la politique")

            for rule in rules:
                if risk not in rule.get("applies_to", []):
                    continue
                if not self._matches(rule, step, plan, verified, confidence):
                    continue
                before = decision
                decision = _harden(decision, rule["decision"])
                if decision != before:
                    reasons.append(f"{rule['id']} : {rule['decision']}")

            decisions.append(PolicyDecision(
                order=step.order,
                action=step.action,
                risk=risk,
                decision=decision,
                reasons=reasons,
            ))

        return PolicyReport(
            decisions=decisions,
            policy_version=self.policy.get("version", 0),
        )

    @staticmethod
    def _matches(
        rule: dict,
        step,
        plan: RemediationPlan,
        verified: set[str],
        confidence: float,
    ) -> bool:
        condition = rule["condition"]

        if condition == "based_on_empty":
            return not step.based_on

        if condition == "no_verified_evidence":
            # Une étape sans source citée relève de `based_on_empty` ;
            # celle-ci vise les étapes qui citent uniquement des preuves
            # que le vérificateur a rejetées.
            return bool(step.based_on) and not any(
                source in verified for source in step.based_on
            )

        if condition == "confidence_below":
            return confidence < rule.get("threshold", 0.5)

        if condition == "plan_has_parse_warning":
            return bool(plan.parse_warning)

        # Une condition inconnue ne doit pas être ignorée en silence :
        # elle signale une politique écrite pour une version du moteur
        # qui n'est pas celle en place.
        raise RuntimeError(
            f"condition inconnue dans la règle {rule['id']} : {condition}"
        )


def apply_policy(state) -> dict:
    """Nœud du graphe."""
    plan = state.get("plan")
    if plan is None or not plan.steps:
        return {"policy": PolicyReport()}

    report = PolicyEngine().evaluate(
        plan, state.get("hypothesis"), state.get("verification")
    )
    return {"policy": report}