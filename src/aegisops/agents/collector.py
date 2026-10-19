"""Evidence Collector : fusion des observations et construction de la requête.

Quatrième nœud. Il ne mesure rien et n'interroge rien — il assemble ce
que les deux agents précédents ont produit, et en dérive deux sorties de
formes différentes.

La distinction est délibérée.

`search_query` est destinée à BM25. Les mesures de S3 ont établi que le
lexical domine le dense sur ce corpus (100 % contre 75 % de recall@5),
parce que les postmortems contiennent les identifiants exacts du bac à
sable. La requête conserve donc ces identifiants bruts : `db_pool_timeout`
vaut mieux que « délai d'attente sur le pool ».

`summary` est destinée au prompt du Root Cause Agent en S5. Elle est
rédigée en phrases, groupée par type de preuve, et ne contient que ce qui
est anormal — le contexte d'un modèle 1.5B ne supporte pas quarante
observations dont trente-six sont nominales.

Les deux dérivent des mêmes données. Ce qui change est le lecteur.
"""
from __future__ import annotations

from aegisops.agents.state import (
    IncidentState,
    LogObservation,
    MetricObservation,
    TimeWindow,
)
from aegisops.agents.log_agent import NOMINAL_EVENTS, SANDBOX_EVENTS

# Un résumé plus long que cela sature le contexte utile d'un modèle de
# 1,5 milliard de paramètres, qui dispose de 32 k tokens mais dont la
# qualité de raisonnement chute bien avant.
MAX_SUMMARY_LINES = 24

# Nombre de motifs de logs retenus par service. Au-delà, on tombe dans la
# queue des événements rares, qui allongent la requête sans discriminer.
TOP_PATTERNS_PER_SERVICE = 5


def _dep(labels: dict) -> str:
    for key in ("dependency", "reason", "service"):
        if key in labels:
            return labels[key]
    return ""


def build_search_query(
    service: str,
    metrics: list[MetricObservation],
    logs: list[LogObservation],
) -> str:
    """Construit la requête de recherche documentaire.

    Format structuré et non descriptif : identifiants de métriques et
    noms d'événements tels quels, valeurs chiffrées, pas de phrases.

    C'est le format sur lequel BM25 atteint 100 % de recall@5 (S3-J6),
    parce que les postmortems du corpus emploient exactement ces termes.
    Reformuler en langue naturelle ferait perdre le seul signal
    réellement discriminant.
    """
    parts = [f"incident on {service}"]

    for o in sorted(metrics, key=lambda x: -(x.ratio or float("inf"))):
        if not o.anomalous:
            continue
        dep = _dep(o.labels)
        name = f"{o.metric}[{dep}]" if dep else o.metric
        parts.append(f"{name} {o.baseline} to {o.peak}")

        # Aucune répétition des termes du service en incident. Une version
        # précédente doublait ses métriques et ses événements de logs pour
        # les faire peser plus que le bruit des dépendances. Mesuré sur les
        # 19 incidents stockés : sur les 9 incidents de pool, l'ordre
        # `connection_leak` / `bad_release` est resté le même dans tous
        # les cas, avec ou sans répétition — `bad_release` devant 9 fois
        # sur 9. Et la répétition doublait `dependency_error`, émis par
        # checkout-service, ce qui faisait remonter `internal_error`.
        # Voir docs/benchmarks.md, S4.

    # Les métriques restées normales sont des preuves négatives et
    # méritent de figurer : un postmortem qui écarte explicitement une
    # piste contient souvent les mêmes termes.
       # Les métriques restées normales ne figurent pas dans la requête.
    #
    # L'intuition était qu'elles renforcent le diagnostic : un postmortem
    # qui écarte explicitement une piste emploie les mêmes termes. Mesuré,
    # l'effet est inverse — « unchanged: memory_mb p95_by_dependency »
    # contient des termes présents dans les six tableaux « Observed
    # symptoms » du corpus, et fait remonter `memory_leak` sur un
    # incident `connection_leak`.
    #
    # BM25 compte des occurrences sans distinguer une affirmation d'une
    # négation. Les preuves négatives appartiennent au résumé, que le
    # Root Cause Agent interprète.

    by_service: dict[str, list[LogObservation]] = {}
    for o in logs:
        if o.event in NOMINAL_EVENTS or o.event in SANDBOX_EVENTS:
            continue
        by_service.setdefault(o.service, []).append(o)

    for svc, items in by_service.items():
        top = sorted(items, key=lambda x: -x.count)[:TOP_PATTERNS_PER_SERVICE]
        for o in top:
            parts.append(f"{o.event} {o.count}")
            # Pas de répétition de l'événement : voir la boucle des
            # métriques ci-dessus.
            for k, v in o.sample.items():
                if k in ("dependency", "status", "error_type"):
                    parts.append(f"{k} {v}")
                # `version` est délibérément exclu. Il distingue bien une
                # régression d'une fuite préexistante — mais seulement si
                # on sait la *lire* : v2.7 signifie « pas de déploiement
                # récent », v2.8 l'inverse.
                #
                # BM25 compte des termes, il n'interprète pas. Inclure
                # « version v2.7 » fait remonter le postmortem de
                # `bad_release`, seul document du corpus à parler de
                # versions — c'est-à-dire exactement la mauvaise réponse.
                # Mesuré : les trois premiers passages renvoyés étaient
                # `bad_release` sur un incident `connection_leak`.
                #
                # L'information reste dans le résumé, que le Root Cause
                # Agent lit et interprète.
                

    # Un déploiement dans la fenêtre est une preuve positive : il doit
    # peser dans la recherche. Son absence ne doit rien peser — d'où la
    # dissymétrie avec le champ `version` ci-dessus.
    if any(o.event == "deployment" for o in logs):
        parts.append("deployment rollout release regression")

    return "\n".join(parts)


def build_summary(
    service: str,
    window: TimeWindow,
    metrics: list[MetricObservation],
    logs: list[LogObservation],
) -> str:
    """Rédige le résumé destiné au raisonnement.

    Groupé par nature de preuve plutôt que par source. Un analyste ne lit
    pas « voici les métriques, voici les logs » : il lit « voici ce qui a
    changé, voici ce qui n'a pas bougé, voici ce que disent les logs ».

    La section des signaux inchangés est la plus importante des trois.
    C'est elle qui permet d'écarter des hypothèses, et sans elle un
    modèle n'a aucun moyen de savoir qu'une piste a été examinée plutôt
    qu'oubliée.
    """
    lines = [
        f"Service: {service}",
        f"Window: {window.start.strftime('%H:%M:%S')} - "
        f"{window.end.strftime('%H:%M:%S')} ({window.duration_s:.0f}s)",
        f"Detected by: {window.reason}",
        "",
    ]

    anomalies = [o for o in metrics if o.anomalous]
    if anomalies:
        lines.append("Metrics that changed:")
        for o in sorted(anomalies, key=lambda x: -(x.ratio or float("inf"))):
            dep = _dep(o.labels)
            name = f"{o.metric}[{dep}]" if dep else o.metric
            change = f"×{o.ratio}" if o.ratio else "from zero"
            lines.append(f"  {name}: {o.baseline} -> {o.peak} ({change})")
    else:
        lines.append("Metrics that changed: none")

    normal = [o for o in metrics if not o.anomalous]
    if normal:
        lines.append("")
        lines.append("Metrics that stayed normal:")
        for o in normal:
            dep = _dep(o.labels)
            name = f"{o.metric}[{dep}]" if dep else o.metric
            lines.append(f"  {name}: {o.baseline} -> {o.peak}")

    by_service: dict[str, list[LogObservation]] = {}
    for o in logs:
        if o.event in NOMINAL_EVENTS or o.event in SANDBOX_EVENTS:
            continue
        by_service.setdefault(o.service, []).append(o)

    if by_service:
        lines.append("")
        lines.append("Log events:")
        for svc, items in by_service.items():
            lines.append(f"  {svc}:")
            for o in sorted(items, key=lambda x: -x.count)[:TOP_PATTERNS_PER_SERVICE]:
                detail = ""
                if o.sample:
                    detail = " (" + ", ".join(
                        f"{k}={v}" for k, v in o.sample.items()
                    ) + ")"
                lines.append(f"    {o.event} ×{o.count}{detail}")

    if len(lines) > MAX_SUMMARY_LINES * 2:
        lines = lines[: MAX_SUMMARY_LINES * 2]
        lines.append("  [truncated]")

    return "\n".join(lines)


def collect(state: IncidentState) -> dict:
    """Nœud du graphe : produit la requête et le résumé."""
    service = state["alert"].service
    metrics = state.get("metrics", [])
    logs = state.get("logs", [])

    return {
        "search_query": build_search_query(service, metrics, logs),
        "trace": [{
            "node": "collect.summary",
            "text": build_summary(service, state["window"], metrics, logs),
        }],
    }