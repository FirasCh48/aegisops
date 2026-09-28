"""payment-service : autorise un paiement. Volontairement minimal."""
import asyncio
import os
import random

import structlog
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from logging_conf import setup_logging

SERVICE = "payment-service"
setup_logging(SERVICE)
log = structlog.get_logger()

VERSION = os.getenv("SERVICE_VERSION", "v3.1")
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", "40"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.0"))

app = FastAPI(title=SERVICE, version=VERSION)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# --- Injection de pannes -------------------------------------------------
FAULTS_ENABLED = os.getenv("FAULTS_ENABLED", "0") == "1"
fault_registry = None

if FAULTS_ENABLED:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from faults.registry import FaultRegistry
    from faults.router import build_router

    fault_registry = FaultRegistry(SERVICE)
    fault_registry.register("slow_response")
    app.include_router(build_router(fault_registry))
# -------------------------------------------------------------------------


class AuthorizeRequest(BaseModel):
    user_id: int
    amount_cents: int


@app.on_event("startup")
async def startup() -> None:
    log.info(
        "service_started",
        version=VERSION,
        base_latency_ms=BASE_LATENCY_MS,
        failure_rate=FAILURE_RATE,
        faults_enabled=FAULTS_ENABLED,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


def _extra_latency_s() -> float:
    """Latence supplémentaire injectée à chaud, sans redémarrage.

    Contrairement à BASE_LATENCY_MS, ne vide pas l'état du service et ne
    crée pas de trou dans les métriques. C'est ce qui permet d'enregistrer
    un incident dont le fond sain et la phase dégradée sont continus.
    """
    if fault_registry is None or not fault_registry.is_active("slow_response"):
        return 0.0
    params = fault_registry.get("slow_response").params
    return float(params.get("extra_ms", 3000)) / 1000


@app.post("/authorize")
async def authorize(req: AuthorizeRequest) -> dict:
    base = BASE_LATENCY_MS / 1000 * random.uniform(0.8, 1.2)
    await asyncio.sleep(base + _extra_latency_s())

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