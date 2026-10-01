"""Client Prometheus : lecture des métriques du bac à sable.

Outil appelé par le Metrics Agent en semaine 4. Il ne décide de rien :
il traduit une requête PromQL en séries exploitables, et distingue les
trois réponses possibles — série trouvée, métrique absente, Prometheus
injoignable — qui mènent à trois conclusions différentes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from aegisops.core.config import settings


class MetricNotFound(Exception):
    """La requête est valide mais ne renvoie aucune série.

    Distinct d'une erreur : « le pool est vide » et « je ne sais pas si
    le pool est vide » mènent à des conclusions opposées.
    """


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    points: list[tuple[datetime, float]]

    @property
    def peak(self) -> float | None:
        return max((v for _, v in self.points), default=None)

    @property
    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None

    @property
    def mean(self) -> float | None:
        if not self.points:
            return None
        return sum(v for _, v in self.points) / len(self.points)

    def peak_at(self) -> datetime | None:
        if not self.points:
            return None
        return max(self.points, key=lambda p: p[1])[0]

    def summary(self) -> dict:
        """Forme compacte destinée au LLM : un résumé, pas 200 points."""
        return {
            "labels": self.labels,
            "points": len(self.points),
            "peak": round(self.peak, 4) if self.peak is not None else None,
            "mean": round(self.mean, 4) if self.mean is not None else None,
            "last": round(self.last, 4) if self.last is not None else None,
            "peak_at": self.peak_at().isoformat() if self.points else None,
        }

    def __repr__(self) -> str:
        name = self.labels.get("dependency") or self.labels.get("service") or "?"
        return f"<Series {name} n={len(self.points)} peak={self.peak}>"


def _to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse(raw: str) -> float | None:
    """Prometheus sérialise NaN en chaîne. Le propager fausserait tout
    calcul en aval, donc on l'écarte ici, au plus bas niveau."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


class PrometheusClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or settings.prometheus_url).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        r = httpx.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus: {payload.get('error', 'erreur inconnue')}")
        return payload["data"]

    def query(self, promql: str, at: datetime | None = None) -> list[Series]:
        params = {"query": promql}
        if at is not None:
            params["time"] = at.timestamp()
        data = self._get("/api/v1/query", params)

        out: list[Series] = []
        for item in data.get("result", []):
            ts, raw = item["value"]
            v = _parse(raw)
            if v is not None:
                out.append(Series(labels=item["metric"], points=[(_to_dt(ts), v)]))
        return out

    def query_range(
        self,
        promql: str,
        start: datetime,
        end: datetime,
        step: str = "15s",
    ) -> list[Series]:
        data = self._get(
            "/api/v1/query_range",
            {
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )

        out: list[Series] = []
        for item in data.get("result", []):
            points = [
                (_to_dt(ts), v)
                for ts, raw in item.get("values", [])
                if (v := _parse(raw)) is not None
            ]
            if points:
                out.append(Series(labels=item["metric"], points=points))
        return out

    def scalar(self, promql: str, at: datetime | None = None) -> float:
        """Valeur unique. Lève MetricNotFound si la série est absente."""
        series = self.query(promql, at)
        if not series:
            raise MetricNotFound(promql)
        return series[0].last