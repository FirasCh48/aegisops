"""Registre des pannes injectables.

Ce module n'est chargé que si FAULTS_ENABLED=1. Le code métier des
services ne le connaît pas : il n'y a aucun `if fault_active` dans
checkout.py. Les pannes agissent sur l'état du service (pool, mémoire,
latence), pas sur son code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class FaultState:
    """État d'une panne : active ou non, depuis quand, avec quels paramètres."""

    name: str
    active: bool = False
    started_at: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        if not self.active or self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "active": self.active,
            "elapsed_s": round(self.elapsed_s, 1),
            "params": self.params,
        }


class FaultRegistry:
    """Un registre par service. Les pannes sont nommées et indépendantes."""

    def __init__(self, service: str) -> None:
        self.service = service
        self._faults: dict[str, FaultState] = {}

    def register(self, name: str) -> None:
        self._faults[name] = FaultState(name=name)

    def enable(self, name: str, params: dict | None = None) -> FaultState:
        fault = self._get(name)
        fault.active = True
        fault.started_at = time.time()
        fault.params = params or {}
        # Ce log est la borne gauche de la fenêtre d'incident.
        # capture_incident.py s'en servira pour dater le début exact.
        log.warning(
            "fault_injected",
            fault=name,
            params=fault.params,
            injected_service=self.service,
        )
        return fault

    def disable(self, name: str) -> FaultState:
        fault = self._get(name)
        duration = fault.elapsed_s
        fault.active = False
        fault.started_at = None
        fault.params = {}
        log.warning(
            "fault_cleared",
            fault=name,
            duration_s=round(duration, 1),
            injected_service=self.service,
        )
        return fault

    def is_active(self, name: str) -> bool:
        return self._get(name).active

    def get(self, name: str) -> FaultState:
        return self._get(name)

    def all(self) -> list[dict]:
        return [f.as_dict() for f in self._faults.values()]

    def clear_all(self) -> None:
        for name, fault in self._faults.items():
            if fault.active:
                self.disable(name)

    def _get(self, name: str) -> FaultState:
        if name not in self._faults:
            raise KeyError(f"panne inconnue: {name}")
        return self._faults[name]