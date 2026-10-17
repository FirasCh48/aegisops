"""Détection de la fenêtre d'incident.

Premier nœud réel du graphe. Il ne raisonne pas : il calcule.

Le problème qu'il résout est celui mesuré en semaine 2 — la bonne fenêtre
d'analyse dépend du type de panne. Une dépendance lente se voit en trente
secondes, une fuite mémoire demande dix minutes. Une fenêtre fixe rate la
seconde ou noie la première dans du bruit.

L'approche : partir large, repérer l'instant où les signaux changent,
resserrer autour. Aucun appel LLM — la fenêtre doit être déterministe,
parce que tout ce que les agents suivants liront en dépend.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from aegisops.agents.state import Alert, TimeWindow
from aegisops.tools.prometheus import PrometheusClient, Series

# Fenêtre de recherche initiale. Large pour couvrir les pannes lentes :
# une fuite mémoire reste invisible sur moins de dix minutes.
SEARCH_BEFORE = timedelta(minutes=30)
SEARCH_AFTER = timedelta(minutes=5)

# Pas d'échantillonnage. Prometheus scrape toutes les 5 s ; 15 s lisse le
# bruit sans masquer un changement de régime.
STEP = "15s"

# Marge ajoutée de part et d'autre du changement détecté. Le début réel
# d'une panne précède toujours sa manifestation métrique : le pool se
# vide avant que la première requête n'échoue.
PADDING = timedelta(seconds=60)

# Les signaux examinés, par ordre de sensibilité décroissante. Le premier
# qui change nettement détermine la fenêtre.
SIGNALS = {
    "error_rate": 'sum(rate(http_requests_total{service="%(svc)s",status=~"5.."}[1m]))'
                  ' / sum(rate(http_requests_total{service="%(svc)s"}[1m]))',
    "p95_by_dependency": 'histogram_quantile(0.95, sum(rate('
                         'dependency_call_duration_seconds_bucket[1m])) by (dependency, le))',
    "db_pool_usage": "db_pool_connections_in_use / db_pool_size",
    "memory_mb": 'process_memory_rss_mb{service="%(svc)s"}',
}


@dataclass
class Changepoint:
    """Instant où un signal quitte son régime de base."""

    signal: str
    at: datetime
    baseline: float
    value: float
    z_score: float

    @property
    def ratio(self) -> float:
        if self.baseline == 0:
            return float("inf") if self.value > 0 else 1.0
        return self.value / self.baseline


def find_changepoint(
    series: Series,
    baseline_points: int = 8,
    z_threshold: float = 3.0,
    ratio_threshold: float = 1.5,
) -> Changepoint | None:
    """Trouve le premier point qui s'écarte nettement du début de la série.

    La référence est prise sur un nombre **fixe** de points initiaux, et
    non sur une fraction de la série. Une fraction paraît plus robuste,
    elle est en réalité piégeuse : si l'incident commence tôt dans la
    fenêtre, le tiers initial le contient déjà et la « baseline » devient
    la valeur de panne. Observé sur une capture où le pool passe de 0 à 1
    après cinq points : la médiane du premier tiers valait 1,0, et plus
    aucun point ne s'en écartait.

    Deux critères combinés, parce qu'aucun ne suffit seul. Le z-score
    repère un écart relatif à la variabilité observée, mais explose sur
    un signal plat à zéro. Le ratio repère un changement d'ordre de
    grandeur, mais manque les dérives lentes sur signal bruité.
    """
    points = series.points
    if len(points) < baseline_points + 3:
        return None

    head = [v for _, v in points[:baseline_points]]
    baseline = statistics.median(head)
    spread = statistics.pstdev(head)

    for ts, value in points[baseline_points:]:
        if spread > 0:
            z = abs(value - baseline) / spread
        else:
            z = float("inf") if value != baseline else 0.0

        ratio = value / baseline if baseline else (float("inf") if value else 1.0)

        if z >= z_threshold and ratio >= ratio_threshold:
            return Changepoint(
                signal="",
                at=ts,
                baseline=round(baseline, 4),
                value=round(value, 4),
                z_score=round(z, 2) if z != float("inf") else 999.0,
            )

    return None


class WindowDetector:
    def __init__(self, client: PrometheusClient | None = None) -> None:
        self.client = client or PrometheusClient()

    def detect(self, alert: Alert) -> tuple[TimeWindow, list[Changepoint]]:
        search_start = alert.fired_at - SEARCH_BEFORE
        search_end = alert.fired_at + SEARCH_AFTER

        found: list[Changepoint] = []

        for name, template in SIGNALS.items():
            promql = template % {"svc": alert.service}
            try:
                series_list = self.client.query_range(
                    promql, search_start, search_end, step=STEP
                )
            except Exception:
                # Un signal indisponible n'arrête pas la détection : les
                # autres suffisent souvent, et l'absence sera visible
                # dans la liste des changepoints trouvés.
                continue

            for series in series_list:
                cp = find_changepoint(series)
                if cp:
                    label = series.labels.get("dependency", "")
                    cp.signal = f"{name}[{label}]" if label else name
                    found.append(cp)

        if not found:
            # Repli : aucun signal n'a bougé de façon nette. Plutôt que
            # d'échouer, on renvoie une fenêtre par défaut en signalant
            # que la détection n'a rien trouvé — l'information est utile
            # au Root Cause Agent, qui saura que la fenêtre est arbitraire.
            return (
                TimeWindow(
                    start=alert.fired_at - timedelta(minutes=10),
                    end=alert.fired_at + timedelta(minutes=2),
                    reason="aucun changement détecté, fenêtre par défaut",
                ),
                [],
            )

        # Le plus précoce : une panne se manifeste d'abord sur un signal,
        # puis se propage. Prendre le premier remonte au plus près de la
        # cause plutôt qu'à ses conséquences.
        earliest = min(found, key=lambda c: c.at)

        return (
            TimeWindow(
                start=earliest.at - PADDING,
                end=min(alert.fired_at + SEARCH_AFTER, search_end),
                reason=(
                    f"{earliest.signal} : {earliest.baseline} -> {earliest.value} "
                    f"(z={earliest.z_score}) à {earliest.at.strftime('%H:%M:%S')}"
                ),
            ),
            sorted(found, key=lambda c: c.at),
        )


def main() -> None:
    from datetime import timezone

    alert = Alert(
        service="checkout-service",
        summary="error rate above 15%",
        fired_at=datetime.now(timezone.utc),
    )

    window, changepoints = WindowDetector().detect(alert)

    print(f"fenêtre : {window.start.strftime('%H:%M:%S')} -> "
          f"{window.end.strftime('%H:%M:%S')}  ({window.duration_s:.0f}s)")
    print(f"raison  : {window.reason}\n")

    if changepoints:
        print("changements détectés :")
        for cp in changepoints:
            print(f"  {cp.at.strftime('%H:%M:%S')}  {cp.signal:<32} "
                  f"{cp.baseline} -> {cp.value}  (z={cp.z_score})")
    else:
        print("aucun changement détecté")


if __name__ == "__main__":
    main()