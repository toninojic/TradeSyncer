"""Persists the risk lock state in SQLite (table ``risk_state``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, RiskStateRow
from app.engine.risk import RiskState


class DbRiskStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def load(self) -> dict[str, RiskState]:
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(RiskStateRow))).scalars()
            return {
                str(row.account_id): RiskState(
                    locked=bool(row.locked), locked_day=row.locked_day, locked_at=row.locked_at,
                    reason=row.reason, baseline=row.baseline or 0.0, baseline_day=row.baseline_day,
                )
                for row in rows
            }

    async def save(self, follower_id: str, state: RiskState) -> None:
        account_id = int(follower_id)
        async with self._sessionmaker() as session:
            if await session.get(Account, account_id) is None:
                return  # the account was deleted meanwhile
            row = await session.get(RiskStateRow, account_id)
            if row is None:
                row = RiskStateRow(account_id=account_id)
                session.add(row)
            row.locked = state.locked
            row.locked_day = state.locked_day
            row.locked_at = state.locked_at
            row.reason = state.reason
            row.baseline = state.baseline
            row.baseline_day = state.baseline_day
            await session.commit()
