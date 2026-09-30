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
INITIAL_STOCK_PER_SKU = 100

# Le stock total est une cause racine possible : sans cette métrique, une
# rupture n'apparaît que comme un pic de 409 sans explication.
stock_total = Gauge("inventory_stock_total", "Unités restantes, tous SKU confondus")
stock_total.set(sum(STOCK.values()))

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
    fault_registry.register("internal_error")
    app.include_router(build_router(fault_registry))
# -------------------------------------------------------------------------


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
        faults_enabled=FAULTS_ENABLED,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/admin/reset")
async def reset_stock() -> dict:
    """Remet le stock à son état initial.

    Sans cette remise à zéro, une capture longue épuise le stock et
    produit des 409 qui n'ont rien à voir avec la panne enregistrée.
    Deux causes mélangées dans un même incident le rendent inutilisable
    comme donnée étiquetée.

    Ce n'est pas une panne : c'est une opération d'administration du bac
    à sable, donc hors du registre de pannes.
    """
    for sku in STOCK:
        STOCK[sku] = INITIAL_STOCK_PER_SKU
    stock_total.set(sum(STOCK.values()))
    log.warning("stock_reset", skus=len(STOCK), per_sku=INITIAL_STOCK_PER_SKU)
    return {"reset": True, "skus": len(STOCK), "per_sku": INITIAL_STOCK_PER_SKU}


def _maybe_fail() -> None:
    """Erreur applicative interne, propagée en 500.

    checkout la traduira en 409 (son `except HTTPStatusError` pour
    inventory), ce qui est précisément le piège : le code d'erreur vu par
    le client ne dit rien du code réel de la dépendance. Un agent qui ne
    lit pas les logs d'inventory conclura « rupture de stock » et
    recommandera de réapprovisionner — ce qui ne résout rien.
    """
    if fault_registry is None or not fault_registry.is_active("internal_error"):
        return

    params = fault_registry.get("internal_error").params
    rate = float(params.get("rate", 0.5))
    if random.random() >= rate:
        return

    log.error(
        "inventory_internal_error",
        error_type="StockLedgerCorruption",
        hint="checksum mismatch on stock ledger",
    )
    raise HTTPException(500, "internal stock ledger error")


@app.post("/reserve")
async def reserve(req: ReserveRequest) -> dict:
    _maybe_fail()
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