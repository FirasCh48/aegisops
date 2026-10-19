"""Clients de rejeu : lecture des métriques et logs depuis une capture.

La rétention de Prometheus est de six heures. Un incident capturé le
matin n'est plus interrogeable l'après-midi, ce qui rend toute mesure du
pipeline impossible dès le lendemain — et rendait nécessaire une capture
fraîche avant chaque session.

Ces clients lisent les mêmes données depuis le JSON de la capture, qui
contient `metrics_raw` (séries complètes, stockées depuis S4-J2) et
`logs` (événements agrégés par phase).

Ils exposent la même interface que les clients réels : les agents ne
savent pas lequel ils utilisent. C'est la condition pour que la mesure
porte sur le pipeline tel qu'il tourne, et non sur une variante écrite
pour le test.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aegisops.tools.loki import LogEntry
from aegisops.tools.prometheus import MetricNotFound, Series

ROOT = Path(__file__).resolve().parents[3]

# Correspondance entre les requêtes PromQL émises par les agents et les
# clés sous lesquelles la capture a stocké leurs résultats.
#
# Le rejeu ne peut pas évaluer du PromQL : il retrouve la série par le
# nom de la métrique qu'elle contient. Une requête dont la métrique
# n'apparaît dans aucune clé lève MetricNotFound, comme le ferait un
# Prometheus qui ne connaît pas la série.
QUERY_KEYS = {
    "db_pool_connections_in_use": "db_pool_usage",
    "process_memory_rss_mb": "memory_mb",
    "app_cache_entries": "cache_entries",
    "inventory_stock_total": "stock_total",
    "dependency_call_duration_seconds_bucket": "p95_by_dependency",
    "dependency_failures_total": "dependency_failures",
    "http_requests_total": "error_rate",
}


def _resolve_key(promql: str) -> str | None:
    for marker, key in QUERY_KEYS.items():
        if marker in promql:
            return key
    return None


class ReplayPrometheus:
    """Rejoue les séries d'une capture avec l'interface de PrometheusClient."""

    def __init__(self, incident_path: str | Path) -> None:
        self.path = Path(incident_path)
        self.record = json.loads(self.path.read_text())
        self.raw = self.record.get("metrics_raw", {})
        if not self.raw:
            raise RuntimeError(
                f"{self.path.name} ne contient pas metrics_raw — "
                "capture antérieure à S4-J2, non rejouable"
            )

    def query_range(
        self, promql: str, start: datetime, end: datetime, step: str = "15s"
    ) -> list[Series]:
        key = _resolve_key(promql)
        if key is None or key not in self.raw:
            return []

        out: list[Series] = []
        # Les trois phases sont concaténées : la capture les sépare, mais
        # une fenêtre d'analyse peut les chevaucher. Le filtrage par
        # horodatage reproduit ce que ferait Prometheus.
        merged: dict[str, list[tuple[datetime, float]]] = {}
        labels_by_key: dict[str, dict] = {}

        for phase in ("baseline", "incident", "recovery"):
            series_list = self.raw[key].get(phase, [])
            if not isinstance(series_list, list):
                continue
            for s in series_list:
                sig = json.dumps(s["labels"], sort_keys=True)
                labels_by_key.setdefault(sig, s["labels"])
                points = merged.setdefault(sig, [])
                for ts_str, value in s["points"]:
                    ts = datetime.fromisoformat(ts_str)
                    if start <= ts <= end:
                        points.append((ts, value))

        for sig, points in merged.items():
            if points:
                points.sort(key=lambda p: p[0])
                out.append(Series(labels=labels_by_key[sig], points=points))

        return out

    def query(self, promql: str, at: datetime | None = None) -> list[Series]:
        raise NotImplementedError("le rejeu ne couvre que query_range")

    def scalar(self, promql: str, at: datetime | None = None) -> float:
        raise MetricNotFound(promql)


class ReplayLoki:
    """Rejoue les logs d'une capture avec l'interface de LokiClient.

    La capture stocke des comptes et quelques échantillons par type
    d'événement, pas les lignes brutes. Les entrées sont donc
    reconstruites : un échantillon répété autant de fois que son compte.

    Cette reconstruction est exacte pour tout ce dont le Log Agent se
    sert — comptes, champs qualifiants — et fausse pour ce dont il ne se
    sert pas : les horodatages individuels. Le template mining sur les
    lignes non structurées ne donnera rien, la capture n'en conservant
    aucune.
    """

    def __init__(self, incident_path: str | Path) -> None:
        self.path = Path(incident_path)
        self.record = json.loads(self.path.read_text())
        self.logs = self.record.get("logs", {})

    def query_range(
        self,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
        direction: str = "forward",
    ) -> list[LogEntry]:
        service = self._service_from(logql)
        if service is None or service not in self.logs:
            return []

        window = self.record["window"]
        phase_bounds = {
            "baseline": (window["baseline_from"], window["injected_at"]),
            "incident": (window["injected_at"], window["cleared_at"]),
            "recovery": (window["cleared_at"], window["end"]),
        }

        entries: list[LogEntry] = []
        for phase, (p_start, p_end) in phase_bounds.items():
            ps, pe = datetime.fromisoformat(p_start), datetime.fromisoformat(p_end)
            # Phase entièrement hors de la fenêtre demandée.
            if pe < start or ps > end:
                continue

            block = self.logs[service].get(phase, {})
            counts = block.get("counts", {})
            samples = block.get("samples", {})

            for event, count in counts.items():
                sample_list = samples.get(event, [{}])
                payload = dict(sample_list[0]) if sample_list else {}
                payload.setdefault("event", event)
                payload.setdefault("service", service)
                line = json.dumps(payload, ensure_ascii=False)
                # Tous les événements d'une phase portent le même
                # horodatage, celui du début de phase. Le Log Agent
                # n'utilise pas les horodatages individuels ; s'il venait
                # à le faire, cette approximation devrait être revue.
                for _ in range(min(count, limit)):
                    entries.append(LogEntry(timestamp=ps, line=line,
                                            labels={"service": service}))

        return entries[:limit]

    def count_by_event(self, logql, start, end, limit=5000) -> dict[str, int]:
        from collections import Counter
        counts: Counter = Counter()
        for entry in self.query_range(logql, start, end, limit=limit):
            counts[entry.event or "_unparsed"] += 1
        return dict(counts.most_common())

    def sample_by_event(self, logql, start, end, per_event=3) -> dict[str, list[dict]]:
        samples: dict[str, list[dict]] = {}
        for entry in self.query_range(logql, start, end, limit=5000):
            key = entry.event or "_unparsed"
            bucket = samples.setdefault(key, [])
            if len(bucket) < per_event:
                bucket.append(entry.as_json())
        return samples

    @staticmethod
    def _service_from(logql: str) -> str | None:
        import re
        m = re.search(r'service="([^"]+)"', logql)
        return m.group(1) if m else None