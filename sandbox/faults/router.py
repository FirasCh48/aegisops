"""Endpoints d'administration des pannes.

Monté uniquement si FAULTS_ENABLED=1. En production, ce router
n'existe pas — il n'est même pas importé.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from faults.registry import FaultRegistry


class FaultRequest(BaseModel):
    fault: str = Field(..., description="Nom de la panne à activer")
    action: str = Field("enable", pattern="^(enable|disable)$")
    params: dict = Field(default_factory=dict)


def build_router(registry: FaultRegistry) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/fault")
    async def list_faults() -> dict:
        return {"service": registry.service, "faults": registry.all()}

    @router.post("/fault")
    async def set_fault(req: FaultRequest) -> dict:
        try:
            if req.action == "enable":
                state = registry.enable(req.fault, req.params)
            else:
                state = registry.disable(req.fault)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return state.as_dict()

    @router.delete("/fault")
    async def clear_all() -> dict:
        registry.clear_all()
        return {"cleared": True, "faults": registry.all()}

    return router