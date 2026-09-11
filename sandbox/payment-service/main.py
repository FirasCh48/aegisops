"""payment-service : autorise un paiement. Volontairement minimal."""
import asyncio
import os
import random

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from logging_conf import setup_logging

SERVICE = "payment-service"
setup_logging(SERVICE)
log = structlog.get_logger()

VERSION = os.getenv("SERVICE_VERSION", "v3.1")
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", "40"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.0"))

app = FastAPI(title=SERVICE, version=VERSION)


class AuthorizeRequest(BaseModel):
    user_id: int
    amount_cents: int


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/authorize")
async def authorize(req: AuthorizeRequest) -> dict:
    await asyncio.sleep(BASE_LATENCY_MS / 1000 * random.uniform(0.8, 1.2))

    if random.random() < FAILURE_RATE:
        log.error(
            "payment_gateway_error",
            user_id=req.user_id,
            amount_cents=req.amount_cents,
            upstream="acme-psp",
        )
        raise HTTPException(502, "payment gateway error")

    auth_id = random.randint(100000, 999999)
    log.info("payment_authorized", user_id=req.user_id, auth_id=auth_id)
    return {"auth_id": auth_id, "status": "authorized"}
