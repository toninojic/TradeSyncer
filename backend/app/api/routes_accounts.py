"""Accounts, copy settings, risk rules and per-account actions."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_runtime, require_auth
from app.api.schemas import (
    AccountIn,
    AccountOut,
    AccountUpdate,
    CopySettingsIn,
    CopySettingsOut,
    MappingIn,
    RiskRulesIn,
    RiskRulesOut,
)
from app.brokers.base import BrokerError
from app.db.models import Account, ContractMappingRow, RiskEvent
from app.db.repo import ensure_children, list_accounts
from app.engine.config import ContractMapping, FollowerConfig, MismatchMode
from app.engine.events import LogCategory, LogLevel
from app.services.runtime import Runtime

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])


def account_out(account: Account, runtime: Runtime) -> AccountOut:
    adapter = runtime.adapters.get(account.engine_id)
    return AccountOut(
        id=account.id, name=account.name, broker_type=account.broker_type, role=account.role,
        enabled=account.enabled, has_credentials=bool(account.credentials_enc),
        sim_starting_balance=account.sim_starting_balance,
        connection=str(adapter.status) if adapter else "error",
        created_at=account.created_at, updated_at=account.updated_at,
    )


async def _get(db: AsyncSession, account_id: int) -> Account:
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


async def _check_unique(db: AsyncSession, name: str, role: str, enabled: bool,
                        exclude_id: int | None = None) -> None:
    for other in await list_accounts(db):
        if other.id == exclude_id:
            continue
        if other.name.lower() == name.lower():
            raise HTTPException(status.HTTP_409_CONFLICT, f"An account named {name!r} already exists")
        if role == "leader" and enabled and other.role == "leader" and other.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{other.name!r} is already the leader. Disable it or make it a follower first.",
            )


@router.get("", response_model=list[AccountOut])
async def list_all(db: AsyncSession = Depends(get_db), runtime: Runtime = Depends(get_runtime)):
    return [account_out(a, runtime) for a in await list_accounts(db)]


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create(body: AccountIn, db: AsyncSession = Depends(get_db),
                 runtime: Runtime = Depends(get_runtime)):
    await _check_unique(db, body.name, body.role, body.enabled)
    account = Account(
        name=body.name, broker_type=body.broker_type, role=body.role, enabled=body.enabled,
        sim_starting_balance=body.sim_starting_balance,
        credentials_enc=runtime.cipher.encrypt(body.credentials) if body.credentials else None,
    )
    ensure_children(account)
    db.add(account)
    await db.commit()
    await runtime.log(LogLevel.INFO, LogCategory.ENGINE, f"Account created: {account.name}",
                      account.engine_id)
    await runtime.rebuild("account created")
    return account_out(account, runtime)


@router.get("/{account_id}", response_model=AccountOut)
async def get_one(account_id: int, db: AsyncSession = Depends(get_db),
                  runtime: Runtime = Depends(get_runtime)):
    return account_out(await _get(db, account_id), runtime)


@router.put("/{account_id}", response_model=AccountOut)
async def update(account_id: int, body: AccountUpdate, db: AsyncSession = Depends(get_db),
                 runtime: Runtime = Depends(get_runtime)):
    account = await _get(db, account_id)
    name = body.name if body.name is not None else account.name
    role = body.role or account.role
    enabled = body.enabled if body.enabled is not None else account.enabled
    await _check_unique(db, name, role, enabled, exclude_id=account.id)

    needs_rebuild = (
        (body.broker_type is not None and body.broker_type != account.broker_type)
        or role != account.role
        or enabled != account.enabled
        or body.credentials is not None
        or (body.sim_starting_balance is not None
            and body.sim_starting_balance != account.sim_starting_balance)
    )
    account.name = name
    account.role = role
    account.enabled = enabled
    if body.broker_type is not None:
        account.broker_type = body.broker_type
    if body.sim_starting_balance is not None:
        account.sim_starting_balance = body.sim_starting_balance
    if body.credentials is not None:
        account.credentials_enc = runtime.cipher.encrypt(body.credentials) if body.credentials else None
    ensure_children(account)
    await db.commit()
    await runtime.log(LogLevel.INFO, LogCategory.ENGINE, f"Account updated: {account.name}",
                      account.engine_id)
    if needs_rebuild:
        await runtime.rebuild("account updated")
    else:
        await runtime.apply_follower_settings(account.id)
    return account_out(account, runtime)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(account_id: int, db: AsyncSession = Depends(get_db),
                 runtime: Runtime = Depends(get_runtime)) -> None:
    account = await _get(db, account_id)
    name = account.name
    await db.delete(account)
    await db.commit()
    await runtime.log(LogLevel.WARNING, LogCategory.ENGINE, f"Account deleted: {name}", str(account_id))
    await runtime.rebuild("account deleted")


@router.post("/{account_id}/test-connection")
async def test_connection(account_id: int, db: AsyncSession = Depends(get_db),
                          runtime: Runtime = Depends(get_runtime)) -> dict:
    account = await _get(db, account_id)
    adapter = runtime.adapters.get(account.engine_id)
    if adapter is None:
        return {"ok": False, "message": "No adapter could be created for this account", "latency_ms": None}
    managed = runtime.engine is not None and (
        account.engine_id in runtime.engine.workers
        or runtime.engine.leader_adapter is adapter
    )
    started = time.perf_counter()
    try:
        if not adapter.is_connected:
            await asyncio.wait_for(adapter.connect(), 5)
        snapshot = await asyncio.wait_for(adapter.get_account(), 5)
        message = f"Connected. Balance ${snapshot.balance:,.2f}"
        ok = True
    except (BrokerError, asyncio.TimeoutError, NotImplementedError) as exc:
        message = str(exc) or type(exc).__name__
        ok = False
    latency = round((time.perf_counter() - started) * 1000, 1)
    if not managed and adapter.is_connected:
        await adapter.disconnect()  # the copier does not use this account right now
    return {"ok": ok, "message": message, "latency_ms": latency}


@router.post("/{account_id}/flatten")
async def flatten(account_id: int, runtime: Runtime = Depends(get_runtime)) -> dict:
    try:
        await runtime.flatten_account(account_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found") from None
    except BrokerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Flatten failed: {exc}") from exc
    return {"ok": True}


@router.post("/{account_id}/retry-sync")
async def retry_sync(account_id: int, runtime: Runtime = Depends(get_runtime)) -> dict:
    try:
        await runtime.retry_sync(account_id)
    except KeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "This account is not an active follower") from None
    return {"ok": True}


@router.get("/{account_id}/orders")
async def working_orders(account_id: int, runtime: Runtime = Depends(get_runtime)) -> list[dict]:
    return [o.to_dict() for o in runtime.account_orders(account_id)]


# ----------------------------------------------------------------------- copy settings


def _copy_out(account: Account) -> CopySettingsOut:
    cs = account.copy_settings
    return CopySettingsOut(
        account_id=account.id, multiplier=cs.multiplier, max_contracts=cs.max_contracts,
        copy_sl_tp=cs.copy_sl_tp, mismatch_mode=cs.mismatch_mode, copy_enabled=cs.copy_enabled,
        mappings=[MappingIn(leader_symbol=m.leader_symbol, follower_symbol=m.follower_symbol,
                            ratio=m.ratio) for m in account.mappings],
    )


@router.get("/{account_id}/copy-settings", response_model=CopySettingsOut)
async def get_copy_settings(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await _get(db, account_id)
    ensure_children(account)
    return _copy_out(account)


@router.put("/{account_id}/copy-settings", response_model=CopySettingsOut)
async def put_copy_settings(account_id: int, body: CopySettingsIn, db: AsyncSession = Depends(get_db),
                            runtime: Runtime = Depends(get_runtime)):
    account = await _get(db, account_id)
    if account.role != "follower":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Copy settings apply to follower accounts only")
    candidate = FollowerConfig(
        account_id=account.engine_id, multiplier=body.multiplier, max_contracts=body.max_contracts,
        mappings=[ContractMapping(m.leader_symbol, m.follower_symbol, m.ratio) for m in body.mappings],
        copy_sl_tp=body.copy_sl_tp, mismatch_mode=MismatchMode(body.mismatch_mode),
        copy_enabled=body.copy_enabled,
    )
    try:
        candidate.validate()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    ensure_children(account)
    cs = account.copy_settings
    cs.multiplier = body.multiplier
    cs.max_contracts = body.max_contracts
    cs.copy_sl_tp = body.copy_sl_tp
    cs.mismatch_mode = body.mismatch_mode
    cs.copy_enabled = body.copy_enabled
    account.mappings.clear()
    await db.flush()  # delete old mappings before inserting new ones (unique constraint)
    for m in body.mappings:
        account.mappings.append(ContractMappingRow(
            leader_symbol=m.leader_symbol, follower_symbol=m.follower_symbol, ratio=m.ratio))
    await db.commit()
    await runtime.log(LogLevel.INFO, LogCategory.ENGINE, "Copy settings updated", account.engine_id,
                      data=body.model_dump())
    await runtime.apply_follower_settings(account.id)
    return _copy_out(account)


# ----------------------------------------------------------------------- risk rules


def _risk_out(account: Account) -> RiskRulesOut:
    r = account.risk_rules
    days = [int(d) for d in r.trading_days.split(",") if d.strip()] if r.trading_days else []
    return RiskRulesOut(
        account_id=account.id, daily_loss_limit=r.daily_loss_limit, max_position=r.max_position,
        trading_hours_enabled=r.trading_hours_enabled, trading_start=r.trading_start,
        trading_end=r.trading_end, trading_days=days,
    )


@router.get("/{account_id}/risk-rules", response_model=RiskRulesOut)
async def get_risk_rules(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await _get(db, account_id)
    ensure_children(account)
    return _risk_out(account)


@router.put("/{account_id}/risk-rules", response_model=RiskRulesOut)
async def put_risk_rules(account_id: int, body: RiskRulesIn, db: AsyncSession = Depends(get_db),
                         runtime: Runtime = Depends(get_runtime)):
    account = await _get(db, account_id)
    ensure_children(account)
    r = account.risk_rules
    r.daily_loss_limit = body.daily_loss_limit
    r.max_position = body.max_position
    r.trading_hours_enabled = body.trading_hours_enabled
    r.trading_start = body.trading_start
    r.trading_end = body.trading_end
    r.trading_days = ",".join(str(d) for d in body.trading_days)
    await db.commit()
    await runtime.log(LogLevel.INFO, LogCategory.RISK, "Risk rules updated", account.engine_id,
                      data=body.model_dump())
    await runtime.apply_follower_settings(account.id)
    return _risk_out(account)


@router.post("/{account_id}/risk/unlock")
async def unlock_risk(account_id: int, runtime: Runtime = Depends(get_runtime)) -> dict:
    """Re-enable copying after a daily-loss lock. The follower gets a fresh
    allowance of the same size, measured from its current day P&L."""
    if not await runtime.unlock_risk(account_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This account is not locked by a risk rule")
    return {"ok": True}


@router.get("/{account_id}/risk-events")
async def risk_events_for(account_id: int, db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = await db.execute(
        select(RiskEvent).where(RiskEvent.account_id == account_id).order_by(RiskEvent.ts.desc()).limit(200)
    )
    return [{"id": e.id, "rule": e.rule, "message": e.message, "trading_day": e.trading_day, "ts": e.ts}
            for e in rows.scalars()]
