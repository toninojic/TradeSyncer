"""Small data-access helpers shared by the API and the runtime."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, AppSetting, CopySettings, RiskRules
from app.engine.config import ContractMapping, FollowerConfig, MismatchMode


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(AppSetting, key)
    return default if row is None else row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def list_accounts(session: AsyncSession) -> list[Account]:
    result = await session.execute(select(Account).order_by(Account.id))
    return list(result.scalars())


def ensure_children(account: Account) -> None:
    if account.copy_settings is None:
        account.copy_settings = CopySettings()
    if account.risk_rules is None:
        account.risk_rules = RiskRules()


def follower_config(account: Account) -> FollowerConfig:
    cs = account.copy_settings or CopySettings(
        multiplier=1.0, max_contracts=None, copy_sl_tp=True,
        mismatch_mode="auto_correct", copy_enabled=True,
    )
    return FollowerConfig(
        account_id=account.engine_id,
        name=account.name,
        multiplier=cs.multiplier if cs.multiplier is not None else 1.0,
        max_contracts=cs.max_contracts,
        mappings=[ContractMapping(m.leader_symbol, m.follower_symbol, m.ratio) for m in account.mappings],
        copy_sl_tp=cs.copy_sl_tp if cs.copy_sl_tp is not None else True,
        mismatch_mode=MismatchMode(cs.mismatch_mode or "auto_correct"),
        copy_enabled=cs.copy_enabled if cs.copy_enabled is not None else True,
    )
