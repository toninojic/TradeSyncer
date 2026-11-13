"""Demo data: 1 simulated leader + 3 simulated followers with different settings."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, ContractMappingRow, CopySettings, RiskRules


async def seed_demo_data(session: AsyncSession) -> bool:
    """Insert the demo accounts if the database has no accounts. Returns True if seeded."""
    count = await session.scalar(select(func.count()).select_from(Account))
    if count:
        return False

    session.add(Account(
        name="Leader (sim)", broker_type="sim", role="leader",
        copy_settings=CopySettings(), risk_rules=RiskRules(),
    ))
    session.add(Account(
        name="F1 - 1x NQ", broker_type="sim", role="follower",
        copy_settings=CopySettings(multiplier=1.0, max_contracts=10),
        risk_rules=RiskRules(daily_loss_limit=2000, max_position=10),
    ))
    session.add(Account(
        name="F2 - 0.5x NQ", broker_type="sim", role="follower",
        copy_settings=CopySettings(multiplier=0.5, max_contracts=5, mismatch_mode="alert_only"),
        risk_rules=RiskRules(daily_loss_limit=1000, max_position=5),
    ))
    session.add(Account(
        name="F3 - NQ to MNQ 10:1", broker_type="sim", role="follower",
        copy_settings=CopySettings(multiplier=1.0, max_contracts=50),
        mappings=[ContractMappingRow(leader_symbol="NQ", follower_symbol="MNQ", ratio=10)],
        risk_rules=RiskRules(daily_loss_limit=1500, max_position=50),
    ))
    await session.commit()
    return True
