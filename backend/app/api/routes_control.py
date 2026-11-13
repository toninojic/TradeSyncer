"""Dashboard status and copier controls (pause, resume, kill switch)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_runtime, require_auth
from app.core.contracts import CONTRACTS
from app.services.runtime import Runtime

router = APIRouter(prefix="/api", tags=["copier"], dependencies=[Depends(require_auth)])


@router.get("/status")
async def status(runtime: Runtime = Depends(get_runtime)) -> dict:
    """Everything the dashboard shows. The same payload is pushed over the WebSocket."""
    return runtime.snapshot()


@router.get("/meta")
async def meta() -> dict:
    return {
        "contracts": [
            {"symbol": c.symbol, "underlying": c.underlying, "point_value": c.point_value,
             "tick_size": c.tick_size, "description": c.description}
            for c in CONTRACTS.values()
        ],
        "broker_types": ["sim", "tradovate"],
        "mismatch_modes": ["auto_correct", "alert_only"],
    }


@router.post("/copier/pause")
async def pause(runtime: Runtime = Depends(get_runtime)) -> dict:
    await runtime.set_running(False)
    return {"state": runtime.copier_state()}


@router.post("/copier/resume")
async def resume(runtime: Runtime = Depends(get_runtime)) -> dict:
    await runtime.set_running(True)
    return {"state": runtime.copier_state()}


@router.post("/copier/kill")
async def kill(runtime: Runtime = Depends(get_runtime)) -> dict:
    """KILL SWITCH: flatten all followers and stop copying."""
    await runtime.kill_switch()
    return {"state": runtime.copier_state()}


@router.post("/copier/reconcile")
async def reconcile_now(runtime: Runtime = Depends(get_runtime)) -> dict:
    if runtime.engine:
        await runtime.engine.reconcile_now()
    return {"ok": True}
