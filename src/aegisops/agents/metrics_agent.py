"""Metrics Agent : observation des signaux sur la fenêtre d'incident.

Deuxième nœud réel du graphe. Comme le détecteur, il calcule plutôt
qu'il ne raisonne — mais pour une raison différente.

Le détecteur devait être déterministe parce que tout le pipeline dépend
de sa sortie. Ici, c'est une question de division du travail : comparer
des nombres est exactement ce qu'un LLM fait mal et qu'une fonction fait
bien. L'agent produit des constats chiffrés ; l'interprétation — « c'est
une fuite de connexions » — appartient au Root Cause Agent en S5.

Cette séparation a une conséquence vérifiable : chaque affirmation du
rapport final pourra être rattachée à une `MetricObservation` précise,
ce qui est la condition pour que le Verification Agent puisse rejeter ce
qui n'est pas étayé.
"""
from __future__ import annotations

import statistics
from datetime import timedelta

from aegisops.agents.state import MetricObservation, TimeWindow
from aegisops.tools.prometheus import PrometheusClient, Series

# Durée de référence prise avant la fenêtre d'incident. Le détecteur a
# déjà reculé de 60 s ; on remonte plus haut pour disposer d'un régime
# sain non contaminé — leçon de J2, où une baseline trop courte avait
# absorbé l'incident.
BASELINE_LOOKBACK = timedelta(minutes=5)

# Un écart doit franchir les deux seuils pour être retenu : le ratio
# seul manque les dérives, le z-score seul explose sur les signaux plats.
Z_THRESHOLD = 3.0
RATIO_THRESHOLD = 1.5

# Seuils propres au mode "saturation" : une proportion issue d'une
# vingtaine de points ne supporte pas un test statistique, on retient un
# niveau élevé et un écart absolu franc.
SATURATION_LEVEL = 0.8
SATURATION_DELTA = 0.3

# Les signaux et ce qu'ils discriminent. L'ordre suit la hiérarchie de
# fiabilité établie en S2-J6 : gauges d'abord (non dérivées, donc non
# affectées par la fenêtre de rate()), métriques calculées ensuite.
#
# `compare_on` choisit comment juger la période d'incident :
#
#   "peak"       — signal nul au repos. Le pic est l'événement.
#   "median"     — signal qui oscille. Son pic est élevé même en régime
#                  sain ; la médiane capte le changement de niveau.
#   "saturation" — signal borné dont ce qui compte est la *proportion de
#                  temps* passée au plafond, pas le niveau atteint.
SIGNALS = {
    "db_pool_usage": {
        "query": "db_pool_connections_in_use / db_pool_size",
        "discriminates": "saturation ou fuite du pool de connexions",
        # Cette gauge ne se juge ni au pic ni à la médiane. Elle est
        # relevée à chaque requête et vaut 0, 1 ou 2 selon l'instant :
        # son pic atteint 1,0 en régime sain, et sa variance est telle
        # qu'un z-score ne distingue plus rien (mesuré : z = 2,02 pour un
        # pool pourtant saturé en permanence).
        #
        # Ce qui sépare un pool sain d'un pool épuisé, c'est la
        # proportion de temps passée à saturation. Un pool normal y
        # touche brièvement ; un pool fuité n'en redescend plus.
        "compare_on": "saturation",
        "saturation_threshold": 0.9,
    },
    "memory_mb": {
        "query": 'process_memory_rss_mb{service="%(svc)s"}',
        "discriminates": "fuite mémoire",
        # La mémoire fluctue au rythme du ramasse-miettes. La médiane
        # capte le changement de niveau, pas les oscillations.
        "compare_on": "median",
    },
    "cache_entries": {
        "query": "app_cache_entries",
        "discriminates": "croissance d'un cache non borné",
        "compare_on": "median",
    },
    "stock_total": {
        "query": "inventory_stock_total",
        "discriminates": "épuisement de stock (vraie rupture)",
        "compare_on": "median",
    },
    "p95_by_dependency": {
        "query": "histogram_quantile(0.95, sum(rate("
                 "dependency_call_duration_seconds_bucket[1m])) by (dependency, le))",
        "discriminates": "dépendance lente, ou saturation locale si toutes montent",
    },
    "dependency_failures": {
        "query": "sum(rate(dependency_failures_total[1m])) by (dependency, reason)",
        "discriminates": "mode de défaillance : timeout, unreachable, http_error",
    },
    "error_rate": {
        "query": 'sum(rate(http_requests_total{service="%(svc)s",status=~"5.."}[1m]))'
                 ' / sum(rate(http_requests_total{service="%(svc)s"}[1m]))',
        "discriminates": "ampleur de l'incident",
    },
}


def _label(series: Series) -> str:
    """Identifie une série par ses labels les plus parlants.

    Sert de clé d'appariement entre baseline et incident. Sans elle, la
    latence de payment serait comparée à la baseline d'inventory.
    """
    for key in ("dependency", "reason", "service"):
        if key in series.labels:
            parts = [series.labels[key]]
            if key == "dependency" and "reason" in series.labels:
                parts.append(series.labels["reason"])
            return "/".join(parts)
    return ""


def _label_from_dict(labels: dict) -> str:
    for key in ("dependency", "reason", "service"):
        if key in labels:
            return f"[{labels[key]}]"
    return ""


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _fraction_above(values: list[float], threshold: float) -> float:
    """Proportion de points au-dessus d'un seuil.

    Mesure de régime, pas de niveau : elle répond à « combien de temps le
    signal est-il resté haut », question à laquelle ni le pic ni la
    médiane ne répondent.
    """
    if not values:
        return 0.0
    return sum(1 for v in values if v >= threshold) / len(values)


def compare(
    baseline: Series | None,
    incident: Series,
    metric: str,
    note: str,
    compare_on: str = "peak",
    saturation_threshold: float = 0.9,
) -> MetricObservation:
    """Compare deux périodes d'une même série et qualifie l'écart.

    La référence est toujours calculée sur la baseline ; ce qui change
    d'un signal à l'autre, c'est la statistique comparée. Voir le
    commentaire de `SIGNALS` pour le choix de chaque mode.
    """
    base_values = [v for _, v in baseline.points] if baseline else []
    inc_values = [v for _, v in incident.points]

    if compare_on == "saturation":
        # On ne compare plus des niveaux mais des proportions de temps
        # au-dessus du seuil. Les deux valeurs sont dans [0, 1], ce qui
        # rend la lecture directe : 0,1 -> 0,95 signifie « le pool était
        # saturé 10 % du temps, il l'est maintenant 95 % ».
        base = _fraction_above(base_values, saturation_threshold)
        value = _fraction_above(inc_values, saturation_threshold)
        return MetricObservation(
            metric=metric,
            labels=incident.labels,
            baseline=round(base, 3),
            peak=round(value, 3),
            ratio=round(value / base, 2) if base else None,
            anomalous=(
                value >= SATURATION_LEVEL and (value - base) >= SATURATION_DELTA
            ),
            note=f"{note} — fraction du temps au-dessus de {saturation_threshold}",
        )

    base = _median(base_values)

    if compare_on == "median":
        value = _median(inc_values)
    else:
        # Un signal nul au repos se juge au pic : une panne brève noyée
        # dans une fenêtre longue aurait une médiane proche du normal.
        value = incident.peak if incident.peak is not None else 0.0

    if base:
        ratio = value / base
    else:
        # Base nulle : tout mouvement est un changement d'ordre de
        # grandeur. Cas fréquent et significatif — le taux d'erreur et
        # les compteurs d'échec valent zéro en régime sain.
        ratio = float("inf") if value > 0 else 1.0

    spread = statistics.pstdev(base_values) if len(base_values) > 1 else 0.0
    if spread > 0:
        z = abs(value - base) / spread
    else:
        z = float("inf") if value != base else 0.0

    return MetricObservation(
        metric=metric,
        labels=incident.labels,
        baseline=round(base, 4),
        peak=round(value, 4),
        ratio=round(ratio, 2) if ratio != float("inf") else None,
        anomalous=z >= Z_THRESHOLD and ratio >= RATIO_THRESHOLD,
        note=note,
    )


class MetricsAgent:
    def __init__(self, client: PrometheusClient | None = None) -> None:
        self.client = client or PrometheusClient()

    def observe(self, service: str, window: TimeWindow) -> list[MetricObservation]:
        baseline_start = window.start - BASELINE_LOOKBACK
        observations: list[MetricObservation] = []

        for metric, spec in SIGNALS.items():
            promql = spec["query"] % {"svc": service}

            try:
                base_series = self.client.query_range(
                    promql, baseline_start, window.start, step="15s"
                )
                inc_series = self.client.query_range(
                    promql, window.start, window.end, step="15s"
                )
            except Exception:
                # Un signal indisponible ne doit pas interrompre
                # l'observation : les autres suffisent souvent, et son
                # absence se verra à la lecture du rapport.
                continue

            if not inc_series:
                continue

            base_by_key = {_label(s): s for s in base_series}

            for series in inc_series:
                observations.append(
                    compare(
                        base_by_key.get(_label(series)),
                        series,
                        metric,
                        spec["discriminates"],
                        spec.get("compare_on", "peak"),
                        spec.get("saturation_threshold", 0.9),
                    )
                )

        return observations


def summarize(observations: list[MetricObservation]) -> str:
    """Résumé textuel, destiné au prompt du Root Cause Agent en S5.

    Seules les anomalies y figurent : transmettre les quarante
    observations saturerait le contexte d'un modèle 1.5B sans rien
    ajouter. Les signaux normaux restent dans l'état, consultables par le
    Verification Agent s'il doit confirmer qu'une piste a bien été
    écartée — l'absence d'anomalie est une preuve, pas un vide.
    """
    anomalies = [o for o in observations if o.anomalous]
    if not anomalies:
        return "aucune métrique anormale"

    # Les ratios infinis — un signal parti de zéro — sont les plus
    # significatifs et doivent arriver en tête.
    lines = []
    for o in sorted(anomalies, key=lambda x: -(x.ratio or float("inf"))):
        ratio = f"×{o.ratio}" if o.ratio else "depuis zéro"
        lines.append(
            f"{o.metric}{_label_from_dict(o.labels)} : "
            f"{o.baseline} -> {o.peak} ({ratio})"
        )
    return "\n".join(lines)