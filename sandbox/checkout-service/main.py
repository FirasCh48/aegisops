"""checkout-service : orchestre inventory + payment, puis persiste la commande."""
import os
import random

import httpx
import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine

from logging_conf import setup_logging

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
    log.info(
        "service_started",
        version=VERSION,
        pool_size=POOL_SIZE,
        dependency_timeout_s=DEPENDENCY_TIMEOUT,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/checkout")
async def checkout(req: CheckoutRequest) -> dict:
    async with httpx.AsyncClient(timeout=DEPENDENCY_TIMEOUT) as client:
        # 1. Réserver le stock
        try:
            r = await client.post(
                f"{INVENTORY_URL}/reserve", json={"sku": req.sku, "qty": 1}
            )
            r.raise_for_status()
        except httpx.TimeoutException:
            log.error(
                "dependency_timeout",
                dependency="inventory-service",
                timeout_s=DEPENDENCY_TIMEOUT,
                user_id=req.user_id,
            )
            raise HTTPException(504, "inventory-service timeout")
        except httpx.ConnectError:
            log.error(
                "dependency_unreachable",
                dependency="inventory-service",
                url=INVENTORY_URL,
                user_id=req.user_id,
            )
            raise HTTPException(503, "inventory-service unreachable")
        except httpx.HTTPStatusError as e:
            log.warning(
                "dependency_error",
                dependency="inventory-service",
                status=e.response.status_code,
                user_id=req.user_id,
            )
            raise HTTPException(409, "stock unavailable")

        # 2. Autoriser le paiement
        try:
            r = await client.post(
                f"{PAYMENT_URL}/authorize",
                json={"user_id": req.user_id, "amount_cents": req.amount_cents},
            )
            r.raise_for_status()
            auth_id = r.json()["auth_id"]
        except httpx.TimeoutException:
            log.error(
                "dependency_timeout",
                dependency="payment-service",
                timeout_s=DEPENDENCY_TIMEOUT,
                user_id=req.user_id,
            )
            raise HTTPException(504, "payment-service timeout")
        except httpx.ConnectError:
            log.error(
                "dependency_unreachable",
                dependency="payment-service",
                url=PAYMENT_URL,
                user_id=req.user_id,
            )
            raise HTTPException(503, "payment-service unreachable")
        except httpx.HTTPStatusError as e:
            log.error(
                "dependency_error",
                dependency="payment-service",
                status=e.response.status_code,
                user_id=req.user_id,
            )
            raise HTTPException(502, "payment declined")

    # 3. Persister la commande
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO orders (user_id, amount_cents) "
                    "VALUES (:u, :a) RETURNING id"
                ),
                {"u": req.user_id, "a": req.amount_cents},
            )
            order_id = result.scalar_one()
    except (SQLTimeoutError, TimeoutError):
        log.error("db_pool_timeout", user_id=req.user_id, pool_size=POOL_SIZE)
        raise HTTPException(503, "database connection pool exhausted")
    except Exception as e:
        log.error("checkout_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(500, "internal error")

    log.info(
        "order_created",
        order_id=order_id,
        user_id=req.user_id,
        auth_id=auth_id,
        sku=req.sku,
    )
    return {"order_id": order_id, "auth_id": auth_id, "status": "confirmed"}