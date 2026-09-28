"""checkout-service : orchestre inventory + payment, puis persiste la commande."""
import os
import random
import time

import httpx
import structlog
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine

from logging_conf import setup_logging
from metrics import (
    cache_entries,
    db_pool_in_use,
    db_pool_size,
    dependency_failures,
    dependency_latency,
    refresh_memory_metrics,
)

SERVICE = "checkout-service"
setup_logging(SERVICE)
log = structlog.get_logger()

DB_URL = os.getenv(
    "DB_URL", "postgresql+asyncpg://aegis:aegis@localhost:5432/aegis"
)
POOL_SIZE = int(os.getenv("POOL_SIZE", "10"))
POOL_TIMEOUT = float(os.getenv("POOL_TIMEOUT", "5"))
VERSION = os.getenv("SERVICE_VERSION", "v2.7")

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8002")
DEPENDENCY_TIMEOUT = float(os.getenv("DEPENDENCY_TIMEOUT", "2.0"))

engine = create_async_engine(
    DB_URL,
    pool_size=POOL_SIZE,
    max_overflow=0,
    pool_timeout=POOL_TIMEOUT,
    pool_pre_ping=True,
)

app = FastAPI(title=SERVICE, version=VERSION)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# --- Injection de pannes -------------------------------------------------
# Le router n'est monté que si FAULTS_ENABLED=1. Sans cette variable, le
# module faults n'est même pas importé et l'endpoint /admin/fault n'existe
# pas. Le code métier ne contient aucune branche conditionnelle de chaos :
# seulement deux appels à des fonctions qui sortent immédiatement quand le
# registre est absent.
FAULTS_ENABLED = os.getenv("FAULTS_ENABLED", "0") == "1"
fault_registry = None
_leaked_connections: list = []
_response_cache: dict[str, bytes] = {}

if FAULTS_ENABLED:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from faults.registry import FaultRegistry
    from faults.router import build_router

    fault_registry = FaultRegistry(SERVICE)
    fault_registry.register("connection_leak")
    fault_registry.register("memory_leak")
    app.include_router(build_router(fault_registry))
# -------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    user_id: int
    amount_cents: int
    sku: int = 1


@app.on_event("startup")
async def startup() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS orders ("
                "id SERIAL PRIMARY KEY, "
                "user_id INT NOT NULL, "
                "amount_cents INT NOT NULL, "
                "created_at TIMESTAMPTZ DEFAULT now())"
            )
        )
    db_pool_size.set(POOL_SIZE)
    db_pool_in_use.set(0)
    cache_entries.set(0)
    refresh_memory_metrics()
    log.info(
        "service_started",
        version=VERSION,
        pool_size=POOL_SIZE,
        pool_timeout_s=POOL_TIMEOUT,
        dependency_timeout_s=DEPENDENCY_TIMEOUT,
        faults_enabled=FAULTS_ENABLED,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    """Libère ce que les pannes ont volontairement retenu.

    Sans ce handler, les connexions fuitées resteraient ouvertes côté
    Postgres et la session suivante démarrerait avec un pool déjà occupé.
    """
    for conn in _leaked_connections:
        try:
            await conn.close()
        except Exception:
            pass
    _leaked_connections.clear()
    _response_cache.clear()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


async def _maybe_leak_connection() -> None:
    """Emprunte une connexion au pool sans jamais la rendre.

    Reproduit un bug classique : une connexion ouverte dans un chemin de
    code qui ne la ferme pas. Le pool se vide progressivement, et la
    saturation arrive plusieurs minutes après le déploiement fautif.

    À distinguer d'un pool sous-dimensionné : même symptôme
    (db_pool_timeout), causes et remédiations opposées. Le signal qui
    sépare les deux est la gauge, qui ne redescend jamais ici.
    """
    if fault_registry is None or not fault_registry.is_active("connection_leak"):
        return

    params = fault_registry.get("connection_leak").params
    rate = float(params.get("rate", 0.3))
    max_leaked = int(params.get("max_leaked", POOL_SIZE))

    if len(_leaked_connections) >= max_leaked:
        return
    if random.random() >= rate:
        return

    try:
        conn = await engine.connect()
        _leaked_connections.append(conn)
        db_pool_in_use.set(engine.pool.checkedout())
        log.info(
            "connection_leaked",
            leaked_total=len(_leaked_connections),
            pool_size=POOL_SIZE,
        )
    except Exception:
        # Le pool est déjà vide : la fuite a atteint son objectif.
        pass


def _maybe_leak_memory(req: CheckoutRequest) -> None:
    """Fait grossir un cache qui n'expire jamais.

    Reproduit le bug de cache le plus courant en production : une clé
    contenant un élément unique (ici un timestamp nanoseconde), ce qui
    rend toute réutilisation impossible et supprime de fait l'expiration
    naturelle.

    Contrairement aux autres pannes, celle-ci ne casse rien pendant
    longtemps : le taux d'erreur reste à zéro pendant que la mémoire
    monte. L'agent doit détecter une tendance, pas un seuil d'erreur.
    """
    if fault_registry is None or not fault_registry.is_active("memory_leak"):
        return

    params = fault_registry.get("memory_leak").params
    kb_per_request = int(params.get("kb_per_request", 256))
    # Plafond obligatoire : la machine fait déjà tourner cinq conteneurs.
    # Une fuite non bornée la figerait et ferait perdre la session.
    max_mb = int(params.get("max_mb", 300))

    if len(_response_cache) * kb_per_request / 1024 >= max_mb:
        return

    key = f"{req.user_id}:{req.sku}:{time.time_ns()}"
    _response_cache[key] = b"\x00" * (kb_per_request * 1024)
    cache_entries.set(len(_response_cache))


async def call_dependency(
    client: httpx.AsyncClient, name: str, url: str, payload: dict
) -> dict:
    """Appelle une dépendance en mesurant la latence et en qualifiant l'échec.

    Le mode de défaillance est porté à la fois par le log (pour le détail)
    et par le label `reason` du compteur (pour l'agrégation).
    """
    start = time.perf_counter()
    try:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    except httpx.TimeoutException:
        dependency_failures.labels(dependency=name, reason="timeout").inc()
        log.error("dependency_timeout", dependency=name, timeout_s=DEPENDENCY_TIMEOUT)
        raise HTTPException(504, f"{name} timeout")
    except httpx.ConnectError:
        dependency_failures.labels(dependency=name, reason="unreachable").inc()
        log.error("dependency_unreachable", dependency=name, url=url)
        raise HTTPException(503, f"{name} unreachable")
    except httpx.HTTPStatusError as e:
        dependency_failures.labels(dependency=name, reason="http_error").inc()
        log.error("dependency_error", dependency=name, status=e.response.status_code)
        status = 409 if name == "inventory-service" else 502
        raise HTTPException(status, f"{name} error")
    finally:
        # Mesurée même en cas d'échec : une dépendance lente doit apparaître
        # dans l'histogramme, sinon le p95 ment.
        dependency_latency.labels(dependency=name).observe(
            time.perf_counter() - start
        )


@app.post("/checkout")
async def checkout(req: CheckoutRequest) -> dict:
    refresh_memory_metrics()
    await _maybe_leak_connection()
    _maybe_leak_memory(req)

    async with httpx.AsyncClient(timeout=DEPENDENCY_TIMEOUT) as client:
        await call_dependency(
            client,
            "inventory-service",
            f"{INVENTORY_URL}/reserve",
            {"sku": req.sku, "qty": 1},
        )
        payment = await call_dependency(
            client,
            "payment-service",
            f"{PAYMENT_URL}/authorize",
            {"user_id": req.user_id, "amount_cents": req.amount_cents},
        )
        auth_id = payment["auth_id"]

    try:
        async with engine.begin() as conn:
            db_pool_in_use.set(engine.pool.checkedout())
            result = await conn.execute(
                text(
                    "INSERT INTO orders (user_id, amount_cents) "
                    "VALUES (:u, :a) RETURNING id"
                ),
                {"u": req.user_id, "a": req.amount_cents},
            )
            order_id = result.scalar_one()
    except (SQLTimeoutError, TimeoutError):
        dependency_failures.labels(
            dependency="postgres", reason="pool_exhausted"
        ).inc()
        log.error("db_pool_timeout", user_id=req.user_id, pool_size=POOL_SIZE)
        raise HTTPException(503, "database connection pool exhausted")
    except Exception as e:
        dependency_failures.labels(dependency="postgres", reason="error").inc()
        log.error("checkout_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(500, "internal error")
    finally:
        db_pool_in_use.set(engine.pool.checkedout())

    log.info(
        "order_created",
        order_id=order_id,
        user_id=req.user_id,
        auth_id=auth_id,
        sku=req.sku,
    )
    return {"order_id": order_id, "auth_id": auth_id, "status": "confirmed"}