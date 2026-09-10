"""checkout-service : commande d'un panier. Volontairement minimal."""
import asyncio
import os
import random

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
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
    log.info("service_started", version=VERSION, pool_size=POOL_SIZE)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/checkout")
async def checkout(req: CheckoutRequest) -> dict:
    try:
        async with engine.begin() as conn:
            await asyncio.sleep(random.uniform(0.01, 0.05))
            result = await conn.execute(
                text(
                    "INSERT INTO orders (user_id, amount_cents) "
                    "VALUES (:u, :a) RETURNING id"
                ),
                {"u": req.user_id, "a": req.amount_cents},
            )
            order_id = result.scalar_one()
    except TimeoutError:
        log.error("db_pool_timeout", user_id=req.user_id, pool_size=POOL_SIZE)
        raise HTTPException(503, "database connection pool exhausted")
    except Exception as e:
        log.error("checkout_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(500, "internal error")

    log.info("order_created", order_id=order_id, user_id=req.user_id)
    return {"order_id": order_id, "status": "confirmed"}
