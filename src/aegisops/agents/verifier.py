"""Verification Agent : contrôle des preuves citées.

Il ne raisonne pas et n'appelle aucun modèle. Il compare deux textes :
la `source` de chaque preuve citée, et l'ensemble des observations
réellement présentes dans l'IncidentContext.

Deux raisons de ne pas employer un LLM ici.

La vérification doit être **déterministe** : le même rapport doit
recevoir le même verdict à chaque exécution, sinon la mesure de J6 ne
veut rien dire. Un LLM, même à température basse, ne le garantit pas —
mesuré en J1, deux exécutions sur le même incident ont donné 0,86 et
0,78 de confiance, six puis cinq preuves.

Et un modèle qui en contrôle un autre introduit une circularité : en S6,
les deux seront le même modèle affiné. Celui qui se trompe validerait sa
propre erreur.
"""
from __future__ import annotations

import re

from aegisops.agents.state import (
    IncidentContext,
    RootCauseHypothesis,
    VerificationReport,
    VerifiedClaim,
)

# Une preuve cite sa source sous la forme "metric:<nom>" ou "log:<event>".
# Le nom peut porter un label entre crochets, repris de l'affichage du
# collecteur : metric:p95_by_dependency[payment-service].
SOURCE_RE = re.compile(r"^(metric|log):([A-Za-z0-9_.]+)(?:\[([^\]]+)\])?")

NUMBER_RE = re.compile(r"-?\d+\.?\d*")

# Les qualificatifs sont retirés avant l'extraction des nombres. Un
# `version=v2.7`, un `pool_size=10` ou un `status=409` est du contexte
# que le modèle reprend du log, pas une mesure qu'il prétend citer.
#
# Mesuré : « 10 leak events (version=v2.7, pool_size=10) » était marqué
# `misquoted` parce que 2.7 ne figure dans aucune série — une accusation
# fausse portée contre une preuve exacte.
QUALIFIER_RE = re.compile(
    r"\b(?:version|pool_size|status|timeout_s|port|code|severity)"
    r"\s*[=:]\s*v?[\w.]+",
    re.IGNORECASE,
)

# Tolérance relative sur les valeurs citées. Un modèle qui écrit 0,778
# pour 0,7782 ne ment pas ; un qui écrit 0,9 ment. Le seuil est large
# parce qu'on cherche la fabrication, pas l'imprécision.
VALUE_TOLERANCE = 0.05


def _cited_numbers(text: str) -> list[float]:
    """Extrait les valeurs qui prétendent citer une mesure."""
    return [float(n) for n in NUMBER_RE.findall(QUALIFIER_RE.sub("", text))]


def _known_sources(context: IncidentContext) -> dict[str, list[float]]:
    """Index des observations disponibles et de leurs valeurs.

    La clé normalise ce que le Root Cause Agent est susceptible d'écrire :
    le nom de métrique seul, et le nom avec son label. Un modèle citant
    `metric:db_pool_usage` sur une observation portant le label
    `[checkout-service]` ne doit pas être accusé d'invention.
    """
    index: dict[str, list[float]] = {}

    for o in context.metrics:
        values = [v for v in (o.baseline, o.peak, o.ratio) if v is not None]
        label = (
            o.labels.get("dependency")
            or o.labels.get("reason")
            or o.labels.get("service")
            or ""
        )
        index.setdefault(f"metric:{o.metric}", []).extend(values)
        if label:
            index.setdefault(f"metric:{o.metric}[{label}]", []).extend(values)

    for o in context.logs:
        index.setdefault(f"log:{o.event}", []).append(float(o.count))
        index.setdefault(f"log:{o.service}:{o.event}", []).append(float(o.count))
        # Les valeurs des champs qualifiants d'un log sont citables : un
        # modèle qui écrit « pool_size=10 » reprend une donnée réelle.
        for value in o.sample.values():
            try:
                index[f"log:{o.event}"].append(float(value))
            except (TypeError, ValueError):
                continue

    return index


def _values_match(cited: list[float], observed: list[float]) -> bool:
    """Vérifie que chaque nombre cité correspond à une valeur observée.

    Les entiers courants — 0, 1, 2 — sont ignorés : ils apparaissent dans
    des formulations comme « from zero » ou « version 2 » sans prétendre
    citer une mesure, et les compter produirait des faux positifs.
    """
    for value in cited:
        if value in (0.0, 1.0, 2.0):
            continue
        tolerance = max(abs(value) * VALUE_TOLERANCE, 1e-6)
        if not any(abs(value - o) <= tolerance for o in observed):
            return False
    return True


class Verifier:
    def verify(
        self, hypothesis: RootCauseHypothesis, context: IncidentContext
    ) -> VerificationReport:
        index = _known_sources(context)
        claims: list[VerifiedClaim] = []

        for evidence in hypothesis.evidence:
            match = SOURCE_RE.match(evidence.source.strip())
            if not match:
                claims.append(VerifiedClaim(
                    source=evidence.source,
                    status="unsupported",
                    detail="source illisible : ni metric: ni log:",
                ))
                continue

            kind, name, label = match.groups()
            keys = [f"{kind}:{name}"]
            if label:
                keys.insert(0, f"{kind}:{name}[{label}]")

            observed = next((index[k] for k in keys if k in index), None)
            if observed is None:
                claims.append(VerifiedClaim(
                    source=evidence.source,
                    status="unsupported",
                    detail=f"aucune observation nommée {kind}:{name}",
                ))
                continue

            cited = _cited_numbers(evidence.observation)
            if cited and not _values_match(cited, observed):
                claims.append(VerifiedClaim(
                    source=evidence.source,
                    status="misquoted",
                    detail=f"valeurs citées {cited} absentes de {observed}",
                ))
                continue

            claims.append(VerifiedClaim(source=evidence.source, status="verified"))

        # Les hypothèses écartées sont du texte libre : on ne peut pas
        # leur appliquer le même contrôle. On vérifie seulement qu'elles
        # mentionnent au moins un nom d'observation connu.
        #
        # Ce contrôle s'arrête à la présence du nom. Il laisse passer une
        # justification dont le nom est correct mais dont l'affirmation
        # ne l'est pas — observé : « db_pool_usage did not stay at 1.0 »
        # cite une observation réelle, mais ce n'est pas le critère qui
        # sépare bad_release d'une fuite préexistante (c'est l'absence
        # d'événement `deployment`).
        #
        # Aller plus loin demanderait d'interpréter l'affirmation, donc
        # de raisonner — et de réintroduire le modèle qu'on contrôle.
        names = {k.split(":", 1)[1].split("[")[0] for k in index}
        unsupported_rules = [
            text for text in hypothesis.ruled_out
            if not any(n in text for n in names)
        ]

        return VerificationReport(
            claims=claims,
            unsupported_count=sum(1 for c in claims if c.status == "unsupported"),
            misquoted_count=sum(1 for c in claims if c.status == "misquoted"),
            ruled_out_unsupported=unsupported_rules,
        )


def verify(state) -> dict:
    """Nœud du graphe."""
    hypothesis = state.get("hypothesis")
    context = state.get("context")
    if hypothesis is None or context is None:
        return {"verification": VerificationReport()}

    return {"verification": Verifier().verify(hypothesis, context)}