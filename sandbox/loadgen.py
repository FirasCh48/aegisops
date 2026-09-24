#!/usr/bin/env python3
"""Générateur de trafic de fond pour le bac à sable.

Produit une charge continue et concurrente sur checkout-service, avec
une intensité qui varie dans le temps. Sans ce trafic, les métriques
sont plates et les pannes lentes restent invisibles.

Usage:
    uv run python sandbox/loadgen.py --rps 5 --duration 600
    uv run python sandbox/loadgen.py --rps 5 --pattern diurnal
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
import signal
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

CHECKOUT_URL = "http://localhost:8001/checkout"


@dataclass
class Stats:
    """Compteurs locaux, indépendants de Prometheus.

    Sert de contrôle croisé : si le générateur voit 12 % d'erreurs et
    que Prometheus en voit 3 %, c'est que l'instrumentation ment.
    """

    sent: int = 0
    by_status: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def record(self, status: int, latency: float) -> None:
        self.sent += 1
        self.by_status[status] += 1
        self.latencies.append(latency)

    @property
    def error_rate(self) -> float:
        if not self.sent:
            return 0.0
        errors = sum(n for s, n in self.by_status.items() if s >= 500 or s == 0)
        return errors / self.sent

    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(int(len(ordered) * 0.95), len(ordered) - 1)
        return ordered[idx]

    def summary(self) -> str:
        elapsed = time.time() - self.started_at
        statuses = " ".join(
            f"{s}:{n}" for s, n in sorted(self.by_status.items())
        )
        return (
            f"{elapsed:6.0f}s  envoyées={self.sent:<6} "
            f"rps={self.sent / max(elapsed, 1):.1f}  "
            f"erreurs={self.error_rate:.1%}  "
            f"p95={self.p95() * 1000:.0f}ms  [{statuses}]"
        )


def diurnal_factor(elapsed_s: float, period_s: float = 600.0) -> float:
    """Variation d'intensité façon jour/nuit, comprimée sur 10 minutes.

    Oscille entre 0,3× et 1,0× du débit nominal. Donne aux métriques une
    forme de fond : sans elle, l'anomalie se détache d'une ligne droite,
    ce qui est irréaliste et rend la détection trop facile.
    """
    phase = 2 * math.pi * (elapsed_s % period_s) / period_s
    return 0.3 + 0.7 * (0.5 + 0.5 * math.sin(phase))


async def one_request(client: httpx.AsyncClient, stats: Stats) -> None:
    payload = {
        "user_id": random.randint(1, 500),
        "amount_cents": random.randint(500, 20000),
        "sku": random.randint(1, 20),
    }
    start = time.perf_counter()
    try:
        r = await client.post(CHECKOUT_URL, json=payload)
        stats.record(r.status_code, time.perf_counter() - start)
    except httpx.TimeoutException:
        stats.record(0, time.perf_counter() - start)
    except httpx.HTTPError:
        stats.record(0, time.perf_counter() - start)


async def run(rps: float, duration: float, pattern: str, concurrency: int) -> Stats:
    stats = Stats()
    stop = asyncio.Event()

    def _handle_signal() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    # Le sémaphore borne la concurrence : sans lui, un service lent fait
    # s'accumuler les requêtes en vol jusqu'à saturer le générateur
    # lui-même, et on mesure alors le client, pas le service.
    sem = asyncio.Semaphore(concurrency)
    pending: set[asyncio.Task] = set()

    async def guarded(client: httpx.AsyncClient) -> None:
        async with sem:
            await one_request(client, stats)

    limits = httpx.Limits(max_connections=concurrency + 10,
                          max_keepalive_connections=concurrency + 10)

    async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:
        next_report = time.time() + 15
        while not stop.is_set():
            elapsed = time.time() - stats.started_at
            if duration and elapsed >= duration:
                break

            factor = diurnal_factor(elapsed) if pattern == "diurnal" else 1.0
            current_rps = max(rps * factor, 0.2)

            task = asyncio.create_task(guarded(client))
            pending.add(task)
            task.add_done_callback(pending.discard)

            if time.time() >= next_report:
                print(stats.summary(), flush=True)
                next_report = time.time() + 15

            # Intervalle exponentiel : le trafic réel n'est pas cadencé
            # au métronome. Un processus de Poisson produit des rafales
            # et des creux, donc des pics de latence réalistes.
            await asyncio.sleep(random.expovariate(current_rps))

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rps", type=float, default=5.0,
                        help="requêtes par seconde visées")
    parser.add_argument("--duration", type=float, default=0,
                        help="durée en secondes (0 = jusqu'à Ctrl+C)")
    parser.add_argument("--pattern", choices=["flat", "diurnal"],
                        default="diurnal")
    parser.add_argument("--concurrency", type=int, default=20,
                        help="requêtes simultanées maximum")
    args = parser.parse_args()

    print(
        f"loadgen  rps={args.rps}  pattern={args.pattern}  "
        f"concurrency={args.concurrency}  "
        f"duration={'∞' if not args.duration else args.duration}",
        flush=True,
    )
    stats = asyncio.run(
        run(args.rps, args.duration, args.pattern, args.concurrency)
    )
    print("\n--- résumé ---")
    print(stats.summary())


if __name__ == "__main__":
    main()