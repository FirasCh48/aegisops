"""Client Loki : lecture des logs du bac à sable.

Outil appelé par le Log Agent en semaine 4, une fois la fenêtre
délimitée par les métriques.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from aegisops.core.config import settings


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    line: str
    labels: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict:
        """Les logs du bac à sable sont du JSON. Une ligne d'une autre
        forme remonte sous une clé brute plutôt que de lever : un flux
        de logs contient toujours quelques lignes non conformes."""
        try:
            parsed = json.loads(self.line)
        except json.JSONDecodeError:
            return {"_raw": self.line}
        return parsed if isinstance(parsed, dict) else {"_raw": self.line}

    @property
    def event(self) -> str | None:
        return self.as_json().get("event")

    def __repr__(self) -> str:
        return f"<LogEntry {self.timestamp.isoformat()} {self.event or '?'}>"


class LokiClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or settings.loki_url).rstrip("/")
        self.timeout = timeout

    def query_range(
        self,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
        direction: str = "forward",
    ) -> list[LogEntry]:
        r = httpx.get(
            f"{self.base_url}/loki/api/v1/query_range",
            params={
                "query": logql,
                # Loki attend des nanosecondes, pas des secondes.
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": direction,
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "success":
            raise RuntimeError("Loki: réponse en échec")

        entries: list[LogEntry] = []
        for stream in payload["data"].get("result", []):
            labels = stream.get("stream", {})
            for ns, line in stream.get("values", []):
                entries.append(
                    LogEntry(
                        timestamp=datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc),
                        line=line,
                        labels=labels,
                    )
                )

        # Loki renvoie un tableau par stream : sans ce tri, les services
        # arrivent en blocs séparés et la chronologie causale est perdue.
        entries.sort(key=lambda e: e.timestamp)
        return entries

    def count_by_event(
        self, logql: str, start: datetime, end: datetime, limit: int = 5000
    ) -> dict[str, int]:
        """Résumé d'une fenêtre : c'est ce qu'on passe au modèle, pas
        les centaines de lignes brutes."""
        counts: Counter = Counter()
        for entry in self.query_range(logql, start, end, limit=limit):
            counts[entry.event or "_unparsed"] += 1
        return dict(counts.most_common())

    def sample_by_event(
        self, logql: str, start: datetime, end: datetime, per_event: int = 3
    ) -> dict[str, list[dict]]:
        """Quelques exemples par type d'événement.

        Le compte dit ce qui s'est passé, l'échantillon dit à quoi ça
        ressemblait — les champs `dependency`, `version`, `error_type`
        sont là.
        """
        samples: dict[str, list[dict]] = {}
        for entry in self.query_range(logql, start, end, limit=5000):
            key = entry.event or "_unparsed"
            bucket = samples.setdefault(key, [])
            if len(bucket) < per_event:
                bucket.append(entry.as_json())
        return samples