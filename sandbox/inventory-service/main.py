"""inventory-service : vérifie et réserve du stock. Volontairement minimal."""
import asyncio
import os
import random

import structlog
from fastapi import FastAPI, HTTPException
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from logging_conf import setup_logging

SERVICE = "inventory-service"
setup_logging(SERVICE)
log = structlog.get_logger()

VERSION = os.getenv("SERVICE_VERSION", "v1.4")
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", "20"))

app = FastAPI(title=SERVICE, version=VERSION)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# Stock en mémoire : le produit de ce projet est le système d'agents,
# pas la boutique. Un dictionnaire remplit la même fonction qu'une base.
STOCK = {i: 100 for i in range(1, 21)}

# Le stock total est une cause racine possible : sans cette métrique,
# une rupture n'apparaît que comme un pic de 409 sans explication.
stock_total = Gauge("inventory_stock_total", "Unités restantes, tous SKU confondus")
stock_total.set(sum(STOCK.values()))


class ReserveRequest(BaseModel):
    sku: int
    qty: int = 1


@app.on_event("startup")
async def startup() -> None:
    log.info(
        "service_started",
        version=VERSION,
        base_latency_ms=BASE_LATENCY_MS,
        skus=len(STOCK),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/reserve")
async def reserve(req: ReserveRequest) -> dict:
    await asyncio.sleep(BASE_LATENCY_MS / 1000 * random.uniform(0.8, 1.2))

    available = STOCK.get(req.sku, 0)
    if available < req.qty:
        log.warning(
            "out_of_stock", sku=req.sku, requested=req.qty, available=available
        )
        raise HTTPException(409, "out of stock")

    STOCK[req.sku] = available - req.qty
    stock_total.set(sum(STOCK.values()))
    log.info("stock_reserved", sku=req.sku, qty=req.qty, remaining=STOCK[req.sku])
    return {"sku": req.sku, "reserved": req.qty, "remaining": STOCK[req.sku]}