"""inventory-service : vérifie et réserve du stock. Volontairement minimal."""
import asyncio
import os
import random

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from logging_conf import setup_logging

SERVICE = "inventory-service"
setup_logging(SERVICE)
log = structlog.get_logger()

VERSION = os.getenv("SERVICE_VERSION", "v1.4")
# Latence artificielle, pilotable à chaud : c'est le levier de panne
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", "20"))

app = FastAPI(title=SERVICE, version=VERSION)

# Stock en mémoire : pas de DB ici, le produit c'est le système d'agents
STOCK = {i: 100 for i in range(1, 21)}


class ReserveRequest(BaseModel):
    sku: int
    qty: int = 1


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/reserve")
async def reserve(req: ReserveRequest) -> dict:
    await asyncio.sleep(BASE_LATENCY_MS / 1000 * random.uniform(0.8, 1.2))

    available = STOCK.get(req.sku, 0)
    if available < req.qty:
        log.warning("out_of_stock", sku=req.sku, requested=req.qty, available=available)
        raise HTTPException(409, "out of stock")

    STOCK[req.sku] = available - req.qty
    log.info("stock_reserved", sku=req.sku, qty=req.qty, remaining=STOCK[req.sku])
    return {"sku": req.sku, "reserved": req.qty, "remaining": STOCK[req.sku]}