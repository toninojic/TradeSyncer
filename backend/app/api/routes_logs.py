"""Trade log (with CSV export), system log and risk events."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_runtime, require_auth
from app.db.models import FollowerOrder, LeaderEvent, RiskEvent, SystemLog
from app.services.runtime import Runtime

router = APIRouter(prefix="/api", tags=["logs"], dependencies=[Depends(require_auth)])

ERROR_STATUSES = ["rejected", "failed", "blocked"]


def _statuses(status: str | None) -> list[str] | None:
    if not status:
        return None
    result: list[str] = []
    for part in status.split(","):
        part = part.strip().lower()
        result.extend(ERROR_STATUSES if part == "error" else [part])
    return result


def _order_filters(follower_id: int | None, statuses: list[str] | None, action: str | None,
                   symbol: str | None) -> list[Any]:
    conds: list[Any] = []
    if follower_id is not None:
        conds.append(FollowerOrder.follower_id == follower_id)
    if statuses:
        conds.append(FollowerOrder.status.in_(statuses))
    if action:
        conds.append(FollowerOrder.action == action)
    if symbol:
        conds.append(FollowerOrder.symbol == symbol.upper())
    return conds


def _order_dict(o: FollowerOrder, names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": o.id, "follower_id": o.follower_id, "follower_name": names.get(o.follower_id),
        "leader_event_id": o.leader_event_id, "leader_order_id": o.leader_order_id,
        "action": o.action, "status": o.status, "broker_order_id": o.broker_order_id,
        "symbol": o.symbol, "side": o.side, "qty": o.qty, "order_type": o.order_type,
        "price": o.price, "filled_qty": o.filled_qty, "avg_fill_price": o.avg_fill_price,
        "latency_ms": o.latency_ms, "error": o.error,
        "created_at": o.created_at, "updated_at": o.updated_at,
    }


def _event_dict(e: LeaderEvent) -> dict[str, Any]:
    return {
        "id": e.id, "account_id": e.account_id, "type": e.type, "ts": e.ts,
        "received_at": e.received_at, "order_id": e.order_id, "symbol": e.symbol, "side": e.side,
        "qty": e.qty, "order_type": e.order_type, "price": e.price, "fill_qty": e.fill_qty,
        "fill_price": e.fill_price, "leader_position": e.leader_position, "copied": e.copied,
        "note": e.note,
    }


def _names(runtime: Runtime) -> dict[int, str]:
    return {info.id: info.name for info in runtime.accounts.values()}


async def _query_trades(
    db: AsyncSession, *, since: float | None, until: float | None, follower_id: int | None,
    status: str | None, action: str | None, symbol: str | None, include_engine: bool, limit: int,
    offset: int,
) -> tuple[list[LeaderEvent], dict[str, list[FollowerOrder]], list[FollowerOrder]]:
    statuses = _statuses(status)
    order_conds = _order_filters(follower_id, statuses, action, None)
    window = limit + offset

    events_q: Select = select(LeaderEvent)
    if since is not None:
        events_q = events_q.where(LeaderEvent.received_at >= since)
    if until is not None:
        events_q = events_q.where(LeaderEvent.received_at <= until)
    if symbol:
        events_q = events_q.where(or_(
            LeaderEvent.symbol == symbol.upper(),
            exists().where(and_(FollowerOrder.leader_event_id == LeaderEvent.id,
                                FollowerOrder.symbol == symbol.upper())),
        ))
    if order_conds:
        events_q = events_q.where(
            exists().where(and_(FollowerOrder.leader_event_id == LeaderEvent.id, *order_conds))
        )
    events = list((await db.execute(
        events_q.order_by(LeaderEvent.received_at.desc()).limit(window))).scalars())

    by_event: dict[str, list[FollowerOrder]] = {e.id: [] for e in events}
    if events:
        rows = await db.execute(
            select(FollowerOrder)
            .where(FollowerOrder.leader_event_id.in_(list(by_event)), *order_conds)
            .order_by(FollowerOrder.created_at)
        )
        for o in rows.scalars():
            by_event[o.leader_event_id].append(o)

    engine_orders: list[FollowerOrder] = []
    if include_engine:
        q = select(FollowerOrder).where(FollowerOrder.leader_event_id.is_(None),
                                        *_order_filters(follower_id, statuses, action, symbol))
        if since is not None:
            q = q.where(FollowerOrder.created_at >= since)
        if until is not None:
            q = q.where(FollowerOrder.created_at <= until)
        engine_orders = list((await db.execute(
            q.order_by(FollowerOrder.created_at.desc()).limit(window))).scalars())
    return events, by_event, engine_orders


@router.get("/trades")
async def trades(
    since: float | None = None,
    until: float | None = None,
    follower_id: int | None = None,
    status: str | None = Query(default=None, description="comma separated; 'error' = rejected,failed,blocked"),
    action: str | None = None,
    symbol: str | None = None,
    include_engine: bool = True,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, Any]:
    """Timeline of leader events (each with the follower orders it caused) and
    engine actions not caused by a leader event (reconciliation, flatten)."""
    events, by_event, engine_orders = await _query_trades(
        db, since=since, until=until, follower_id=follower_id, status=status, action=action,
        symbol=symbol, include_engine=include_engine, limit=limit, offset=offset,
    )
    names = _names(runtime)
    items: list[dict[str, Any]] = [
        {"kind": "leader_event", "ts": e.received_at, "event": _event_dict(e),
         "orders": [_order_dict(o, names) for o in by_event[e.id]]}
        for e in events
    ]
    items += [{"kind": "engine_action", "ts": o.created_at, "event": None,
               "orders": [_order_dict(o, names)]} for o in engine_orders]
    items.sort(key=lambda item: item["ts"], reverse=True)
    page = items[offset: offset + limit]
    return {"items": page, "has_more": len(items) > offset + limit}


CSV_COLUMNS = [
    "time_utc", "leader_event_id", "leader_event", "leader_symbol", "leader_side", "leader_qty",
    "leader_order_type", "leader_price", "leader_fill_qty", "leader_fill_price",
    "leader_position_after", "copied", "follower_id", "follower_name", "action", "symbol", "side",
    "qty", "order_type", "price", "status", "filled_qty", "avg_fill_price", "latency_ms", "error",
    "broker_order_id",
]


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


@router.get("/trades/export.csv")
async def export_trades(
    since: float | None = None,
    until: float | None = None,
    follower_id: int | None = None,
    status: str | None = None,
    action: str | None = None,
    symbol: str | None = None,
    limit: int = Query(default=10_000, ge=1, le=100_000),
    db: AsyncSession = Depends(get_db),
    runtime: Runtime = Depends(get_runtime),
) -> StreamingResponse:
    events, by_event, engine_orders = await _query_trades(
        db, since=since, until=until, follower_id=follower_id, status=status, action=action,
        symbol=symbol, include_engine=True, limit=limit, offset=0,
    )
    names = _names(runtime)
    rows: list[tuple[float, dict[str, Any]]] = []
    for e in events:
        base = {
            "time_utc": _iso(e.received_at), "leader_event_id": e.id, "leader_event": e.type,
            "leader_symbol": e.symbol, "leader_side": e.side, "leader_qty": e.qty,
            "leader_order_type": e.order_type, "leader_price": e.price, "leader_fill_qty": e.fill_qty,
            "leader_fill_price": e.fill_price, "leader_position_after": e.leader_position,
            "copied": e.copied,
        }
        orders = by_event[e.id]
        if not orders:
            rows.append((e.received_at, base))
        for o in orders:
            rows.append((o.created_at, {**base, **_csv_order(o, names)}))
    for o in engine_orders:
        rows.append((o.created_at, {"time_utc": _iso(o.created_at), **_csv_order(o, names)}))
    rows.sort(key=lambda r: r[0])

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for _, row in rows[-limit:]:
        writer.writerow(row)
    filename = f"trade-log-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_order(o: FollowerOrder, names: dict[int, str]) -> dict[str, Any]:
    return {
        "follower_id": o.follower_id, "follower_name": names.get(o.follower_id, f"#{o.follower_id}"),
        "action": o.action, "symbol": o.symbol, "side": o.side, "qty": o.qty,
        "order_type": o.order_type, "price": o.price, "status": o.status,
        "filled_qty": o.filled_qty, "avg_fill_price": o.avg_fill_price,
        "latency_ms": o.latency_ms, "error": o.error, "broker_order_id": o.broker_order_id,
    }


@router.get("/logs")
async def system_log(
    level: str | None = Query(default=None, description="comma separated: info,warning,error"),
    category: str | None = Query(default=None, description="comma separated"),
    account_id: int | None = None,
    search: str | None = None,
    since: float | None = None,
    before_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    q = select(SystemLog)
    if level:
        q = q.where(SystemLog.level.in_([x.strip() for x in level.split(",")]))
    if category:
        q = q.where(SystemLog.category.in_([x.strip() for x in category.split(",")]))
    if account_id is not None:
        q = q.where(SystemLog.account_id == account_id)
    if search:
        q = q.where(SystemLog.message.ilike(f"%{search}%"))
    if since is not None:
        q = q.where(SystemLog.ts >= since)
    if before_id is not None:
        q = q.where(SystemLog.id < before_id)
    rows = await db.execute(q.order_by(SystemLog.id.desc()).limit(limit))
    return [
        {"id": r.id, "ts": r.ts, "level": r.level, "category": r.category,
         "account_id": r.account_id, "message": r.message, "data": r.data}
        for r in rows.scalars()
    ]


@router.get("/risk/events")
async def risk_events(
    day: str | None = Query(default=None, description="trading day YYYY-MM-DD or 'today'; default: all"),
    limit: int = Query(default=500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    runtime: Runtime = Depends(get_runtime),
) -> list[dict[str, Any]]:
    q = select(RiskEvent)
    if day == "today":
        day = runtime.risk.info()["trading_day"]
    if day:
        q = q.where(RiskEvent.trading_day == day)
    rows = await db.execute(q.order_by(RiskEvent.ts.desc()).limit(limit))
    return [{"id": e.id, "account_id": e.account_id, "rule": e.rule, "message": e.message,
             "trading_day": e.trading_day, "ts": e.ts} for e in rows.scalars()]
