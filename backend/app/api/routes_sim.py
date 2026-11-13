"""Simulator controls: manual leader trades, prices and failure injection.

Only works with simulated accounts; real broker accounts are never touched here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_runtime, require_auth
from app.api.schemas import SimCloseIn, SimFailureIn, SimModifyIn, SimOrderIn, SimPriceIn, SimWalkIn
from app.brokers.base import BrokerError, OrderRequest, OrderType, Side
from app.brokers.sim import SimulatedBroker
from app.engine.events import LogCategory, LogLevel
from app.services.runtime import Runtime

router = APIRouter(prefix="/api/sim", tags=["simulator"], dependencies=[Depends(require_auth)])


def _leader(runtime: Runtime) -> SimulatedBroker:
    info = runtime.leader_info()
    if info is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No enabled leader account")
    try:
        return runtime.sim_adapter(info.id)
    except LookupError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The simulator only works when the leader is a simulated account") from None


def _sim_account(runtime: Runtime, account_id: int) -> SimulatedBroker:
    try:
        return runtime.sim_adapter(account_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Simulated account not found") from None


@router.get("/state")
async def state(runtime: Runtime = Depends(get_runtime)) -> dict:
    leader = runtime.leader_info()
    accounts = []
    for info in runtime.accounts.values():
        adapter = runtime.adapters.get(str(info.id))
        if isinstance(adapter, SimulatedBroker):
            accounts.append({"id": info.id, "name": info.name, "role": info.role,
                             "connection": str(adapter.status), "failures": adapter.failures.to_dict()})
    leader_orders = []
    if leader is not None:
        adapter = runtime.adapters.get(str(leader.id))
        if isinstance(adapter, SimulatedBroker):
            leader_orders = [o.to_dict() for o in adapter.orders_snapshot()]
    return {
        "prices": runtime.market.prices(),
        "random_walk": runtime.market.walk_running,
        "leader_id": leader.id if leader else None,
        "leader_orders": leader_orders,
        "accounts": accounts,
    }


@router.post("/price")
async def set_price(body: SimPriceIn, runtime: Runtime = Depends(get_runtime)) -> dict:
    if body.price is None and body.move is None:
        raise HTTPException(422, "Give either price or move")
    if body.price is not None:
        price = runtime.market.set_price(body.symbol, body.price)
    else:
        price = runtime.market.move(body.symbol, body.move or 0.0)
    return {"symbol": body.symbol, "price": price, "prices": runtime.market.prices()}


@router.post("/walk")
async def set_walk(body: SimWalkIn, runtime: Runtime = Depends(get_runtime)) -> dict:
    if body.running:
        runtime.market.start()
        runtime.market.resume_walk()
    else:
        runtime.market.pause_walk()
    runtime.mark_dirty()
    return {"random_walk": runtime.market.walk_running}


@router.post("/new-day")
async def new_trading_day(runtime: Runtime = Depends(get_runtime)) -> dict:
    """Do now what normally happens at 17:00 Chicago time: reset the day P&L of
    the simulated accounts and re-enable followers locked by the daily loss limit."""
    await runtime.new_trading_day()
    return {"ok": True, **runtime.risk.info()}


@router.post("/order")
async def place_leader_order(body: SimOrderIn, runtime: Runtime = Depends(get_runtime)) -> dict:
    leader = _leader(runtime)
    request = OrderRequest(
        symbol=body.symbol, side=Side(body.side), qty=body.qty, type=OrderType(body.type),
        limit_price=body.limit_price, stop_price=body.stop_price,
        stop_loss=body.stop_loss, take_profit=body.take_profit,
    )
    try:
        order = await leader.place_order(request)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Leader order failed: {exc}") from exc
    runtime.mark_dirty()
    return order.to_dict()


@router.post("/close")
async def close_leader(body: SimCloseIn, runtime: Runtime = Depends(get_runtime)) -> dict:
    leader = _leader(runtime)
    try:
        await leader.flatten(body.symbol)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Close failed: {exc}") from exc
    runtime.mark_dirty()
    return {"ok": True}


@router.post("/orders/{order_id}/modify")
async def modify_leader_order(order_id: str, body: SimModifyIn,
                              runtime: Runtime = Depends(get_runtime)) -> dict:
    leader = _leader(runtime)
    try:
        order = await leader.modify_order(order_id, qty=body.qty, limit_price=body.limit_price,
                                          stop_price=body.stop_price)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc) or type(exc).__name__) from exc
    runtime.mark_dirty()
    return order.to_dict()


@router.post("/orders/{order_id}/cancel")
async def cancel_leader_order(order_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    leader = _leader(runtime)
    try:
        order = await leader.cancel_order(order_id)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc) or type(exc).__name__) from exc
    runtime.mark_dirty()
    return order.to_dict()


@router.post("/accounts/{account_id}/failures")
async def inject_failure(account_id: int, body: SimFailureIn,
                         runtime: Runtime = Depends(get_runtime)) -> dict:
    broker = _sim_account(runtime, account_id)
    if body.action == "disconnect":
        broker.inject_disconnect(body.duration_s)
        text = f"disconnect for {body.duration_s}s" if body.duration_s else "disconnect until restored"
    elif body.action == "restore":
        broker.restore_connection()
        text = "connection restored (reconnects with backoff)"
    elif body.action == "reject_next":
        broker.reject_next_orders(body.count, body.reason or "Simulated rejection")
        text = f"reject next {body.count} order(s)"
    elif body.action == "partial_next":
        broker.partial_fill_next_orders(body.count, body.ratio, body.remainder_delay_s)
        text = f"partial fill next {body.count} order(s) at {body.ratio:.0%}"
    elif body.action == "latency":
        broker.set_latency(body.latency_ms)
        text = f"latency {body.latency_ms:.0f} ms"
    elif body.action == "duplicate_next":
        broker.duplicate_next_events(body.count)
        text = f"duplicate next {body.count} event(s)"
    else:
        broker.clear_failures()
        text = "all failure injection cleared"
    await runtime.log(LogLevel.WARNING, LogCategory.ENGINE, f"Simulator: {text}", str(account_id))
    runtime.mark_dirty()
    return {"ok": True, "failures": broker.failures.to_dict()}
