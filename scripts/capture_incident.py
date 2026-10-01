#!/usr/bin/env python3
"""Enregistre un incident complet : injection, observation, extraction.

Le script existe parce que la séquence manuelle a échoué cinq fois en
trois sessions — panne oubliée du test précédent, stock épuisé, panne
activée avant le trafic. Ces erreurs ne viennent pas d'un manque
d'attention mais du nombre d'étapes à enchaîner dans le bon ordre.

Usage:
    uv run python scripts/capture_incident.py connection_leak
    uv run python scripts/capture_incident.py bad_release --duration 240
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from aegisops.tools.loki import LokiClient
from aegisops.tools.prometheus import PrometheusClient

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "incidents"

SERVICES = {
    "checkout-service": "http://localhost:8001",
    "payment-service": "http://localhost:8002",
    "inventory-service": "http://localhost:8003",
}

# Les premières secondes après le démarrage du générateur sont faussées :
# connexions neuves, caches froids, et rate() calculé sur trop peu de
# points. Sans cette marge, le pic de la fenêtre est un artefact de
# démarrage, pas l'incident.
COLD_START_SKIP_S = 20

# Chaque scénario : sur quel service injecter, avec quels paramètres, et
# la cause racine réelle — celle-ci devient le label du jeu de données.
SCENARIOS = {
    "connection_leak": {
        "service": "checkout-service",
        "params": {"rate": 0.3},
        "root_cause": "Fuite de connexions : le pool se vide car des connexions "
                      "sont empruntées sans être relâchées.",
        "remediation": "Corriger le chemin de code qui n'appelle pas close(), "
                       "puis redémarrer le service pour libérer le pool.",
    },
    "memory_leak": {
        "service": "checkout-service",
        "params": {"kb_per_request": 256, "max_mb": 300},
        "duration": 600,
        "root_cause": "Fuite mémoire : un cache applicatif grossit sans politique "
                      "d'éviction, sa clé contenant un élément unique par requête.",
        "remediation": "Ajouter une politique d'éviction (TTL ou LRU) et retirer "
                       "l'élément unique de la clé. Redémarrage nécessaire.",
    },
    "slow_response": {
        "service": "payment-service",
        "params": {"extra_ms": 3000},
        "root_cause": "Dépendance lente : payment-service dépasse le timeout de "
                      "checkout-service, provoquant des 504 en cascade.",
        "remediation": "Investiguer la lenteur de payment-service. Aucun "
                       "redémarrage de checkout-service n'est utile.",
    },
    "bad_release": {
        "service": "checkout-service",
        "params": {"version": "v2.8"},
        "root_cause": "Régression introduite par le déploiement v2.8, qui a "
                      "introduit une fuite de connexions.",
        "remediation": "Rollback vers v2.7, puis redéploiement pour libérer les "
                       "connexions déjà retenues.",
    },
    "cpu_saturation": {
        "service": "checkout-service",
        "params": {"burn_ms": 40, "max_duration_s": 60},
        "duration": 150,
        "root_cause": "Saturation CPU : la boucle d'événements est bloquée, ce qui "
                      "dégrade simultanément les appels vers toutes les dépendances.",
        "remediation": "Déplacer le calcul bloquant hors de la boucle d'événements.",
    },
    "internal_error": {
        "service": "inventory-service",
        "params": {"rate": 0.5},
        "root_cause": "Erreur interne d'inventory-service (corruption du registre "
                      "de stock), traduite en 409 par checkout-service.",
        "remediation": "Réparer le registre de stock d'inventory-service. "
                       "Réapprovisionner ne résoudrait rien.",
    },
}

# Les requêtes exécutées sur chaque phase. Ce sont celles de queries.md :
# le futur Metrics Agent disposera exactement du même outillage.
METRIC_QUERIES = {
    "error_rate": 'sum(rate(http_requests_total{service="checkout-service",status=~"5.."}[1m]))'
                  ' / sum(rate(http_requests_total{service="checkout-service"}[1m]))',
    "p95_checkout": 'histogram_quantile(0.95, sum(rate('
                    'http_request_duration_seconds_bucket{service="checkout-service"}[1m])) by (le))',
    "p95_by_dependency": 'histogram_quantile(0.95, sum(rate('
                         'dependency_call_duration_seconds_bucket[1m])) by (dependency, le))',
    "dependency_failures": 'sum(rate(dependency_failures_total[1m])) by (dependency, reason)',
    "db_pool_usage": "db_pool_connections_in_use / db_pool_size",
    "memory_mb": 'process_memory_rss_mb{service="checkout-service"}',
    "cache_entries": "app_cache_entries",
    "stock_total": "inventory_stock_total",
}


def _post(url: str, payload: dict | None = None) -> dict:
    r = httpx.post(url, json=payload, timeout=10.0)
    r.raise_for_status()
    return r.json() if r.content else {}


def reset_environment() -> None:
    """Remet le bac à sable dans un état connu.

    Sans cette étape, une panne oubliée d'une capture précédente
    contamine la suivante — erreur commise cinq fois manuellement.
    """
    print("  [reset] stock inventory")
    _post(f"{SERVICES['inventory-service']}/admin/reset")

    for name, url in SERVICES.items():
        try:
            r = httpx.delete(f"{url}/admin/fault", timeout=10.0)
            if r.status_code == 200:
                active = [f for f in r.json()["faults"] if f["active"]]
                if active:
                    print(f"  [reset] {name}: {len(active)} panne(s) levée(s)")
        except httpx.HTTPError:
            pass  # service sans registre de pannes


def verify_healthy() -> None:
    """Un service mort produirait une capture vide, étiquetée comme un
    incident valide."""
    for name, url in SERVICES.items():
        try:
            r = httpx.get(f"{url}/health", timeout=5.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            sys.exit(f"ERREUR: {name} ne répond pas ({type(e).__name__})")
    print("  [check] les trois services répondent")


def capture(scenario_name: str, duration: int, warmup: int, rps: float) -> Path:
    scenario = SCENARIOS[scenario_name]
    target = SERVICES[scenario["service"]]

    if warmup <= COLD_START_SKIP_S:
        sys.exit(
            f"ERREUR: warmup ({warmup}s) doit dépasser le cold start "
            f"({COLD_START_SKIP_S}s), sinon la phase baseline est vide."
        )

    print(f"\n=== capture : {scenario_name} ===")
    reset_environment()
    verify_healthy()

    print(f"  [load]  générateur à {rps} req/s")
    total = warmup + duration + 30
    load = subprocess.Popen(
        [
            sys.executable, str(ROOT / "sandbox" / "loadgen.py"),
            "--rps", str(rps),
            "--duration", str(total),
            "--pattern", "diurnal",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        window_start = datetime.now(timezone.utc)
        print(f"  [warm]  {warmup}s de trafic sain")
        time.sleep(warmup)

        print(f"  [fault] injection de {scenario_name}")
        injected_at = datetime.now(timezone.utc)
        _post(
            f"{target}/admin/fault",
            {"fault": scenario_name, "action": "enable", "params": scenario["params"]},
        )

        print(f"  [wait]  {duration}s d'observation")
        time.sleep(duration)

        cleared_at = datetime.now(timezone.utc)
        httpx.delete(f"{target}/admin/fault", timeout=10.0)
        print("  [fault] panne levée")

        # Marge : Prometheus scrape toutes les 5 s et rate() a besoin
        # d'une fenêtre pour se stabiliser.
        time.sleep(20)
        window_end = datetime.now(timezone.utc)
    finally:
        load.terminate()
        load.wait(timeout=10)

    print("  [fetch] extraction des métriques et des logs")
    prom = PrometheusClient()
    loki = LokiClient()

    # Trois phases distinctes. Résumer la fenêtre entière mélangerait le
    # cold start, le fond sain et l'incident : le pic global tomberait
    # souvent dans le démarrage, et l'agent daterait mal l'incident.
    # Découpées ainsi, les données portent la comparaison elle-même —
    # « p95 payment : 0,05 s puis 4,85 s » — plutôt qu'une valeur isolée.
    phases = {
        "baseline": (
            window_start + timedelta(seconds=COLD_START_SKIP_S),
            injected_at,
        ),
        "incident": (injected_at, cleared_at),
        "recovery": (cleared_at, window_end),
    }

    metrics: dict[str, dict] = {}
    for key, promql in METRIC_QUERIES.items():
        metrics[key] = {}
        for phase, (p_start, p_end) in phases.items():
            if (p_end - p_start).total_seconds() < 15:
                metrics[key][phase] = []
                continue
            try:
                series = prom.query_range(promql, p_start, p_end, step="15s")
                metrics[key][phase] = [s.summary() for s in series]
            except Exception as e:
                metrics[key][phase] = {"error": f"{type(e).__name__}: {e}"}

    logs: dict[str, dict] = {}
    for service in SERVICES:
        selector = f'{{service="{service}"}}'
        logs[service] = {}
        for phase, (p_start, p_end) in phases.items():
            if (p_end - p_start).total_seconds() < 15:
                logs[service][phase] = {"counts": {}, "samples": {}}
                continue
            logs[service][phase] = {
                "counts": loki.count_by_event(selector, p_start, p_end),
                "samples": loki.sample_by_event(selector, p_start, p_end),
            }

    incident_id = f"INC-{injected_at.strftime('%Y%m%d-%H%M%S')}-{scenario_name}"
    record = {
        "incident_id": incident_id,
        "scenario": scenario_name,
        "injected_service": scenario["service"],
        "params": scenario["params"],
        "window": {
            "start": window_start.isoformat(),
            "baseline_from": phases["baseline"][0].isoformat(),
            "injected_at": injected_at.isoformat(),
            "cleared_at": cleared_at.isoformat(),
            "end": window_end.isoformat(),
            "warmup_s": warmup,
            "duration_s": duration,
            "cold_start_skipped_s": COLD_START_SKIP_S,
        },
        "metrics": metrics,
        "logs": logs,
        # Le label : connu avec certitude parce que c'est nous qui avons
        # cassé le système. C'est ce qui rend l'évaluation possible.
        "ground_truth": {
            "root_cause": scenario["root_cause"],
            "remediation": scenario["remediation"],
        },
        "holdout": False,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{incident_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

    checkout_logs = logs["checkout-service"]
    print(f"  [done]  {path.name}")
    print(f"          baseline : {checkout_logs['baseline']['counts']}")
    print(f"          incident : {checkout_logs['incident']['counts']}")
    print(f"          recovery : {checkout_logs['recovery']['counts']}")
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", choices=sorted(SCENARIOS))
    p.add_argument("--duration", type=int, default=None)
    p.add_argument("--warmup", type=int, default=60)
    p.add_argument("--rps", type=float, default=6.0)
    args = p.parse_args()

    duration = args.duration or SCENARIOS[args.scenario].get("duration", 180)
    capture(args.scenario, duration, args.warmup, args.rps)


if __name__ == "__main__":
    main()