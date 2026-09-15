"""checkout-service : orchestre inventory + payment, puis persiste la commande."""
import os
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
    db_pool_in_use,
    db_pool_size,
    dependency_failures,
    dependency_latency,
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
    log.info(
        "service_started",
        version=VERSION,
        pool_size=POOL_SIZE,
        dependency_timeout_s=DEPENDENCY_TIMEOUT,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


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