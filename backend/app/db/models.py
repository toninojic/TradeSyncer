"""Database tables.

Times are stored as UNIX epoch seconds (UTC) to keep SQLite simple; the UI
formats them. Log tables reference accounts by id without a foreign key so the
history survives when an account is deleted.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> float:
    return time.time()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    broker_type: Mapped[str] = mapped_column(String(20), default="sim")  # sim | tradovate
    role: Mapped[str] = mapped_column(String(10), default="follower")  # leader | follower
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credentials_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet token
    sim_starting_balance: Mapped[float] = mapped_column(Float, default=50_000.0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)

    copy_settings: Mapped["CopySettings | None"] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    mappings: Mapped[list["ContractMappingRow"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", lazy="selectin",
        order_by="ContractMappingRow.id",
    )
    risk_rules: Mapped["RiskRules | None"] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    @property
    def engine_id(self) -> str:
        return str(self.id)


class CopySettings(Base):
    __tablename__ = "copy_settings"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    max_contracts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    copy_sl_tp: Mapped[bool] = mapped_column(Boolean, default=True)
    mismatch_mode: Mapped[str] = mapped_column(String(20), default="auto_correct")
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    account: Mapped[Account] = relationship(back_populates="copy_settings")


class ContractMappingRow(Base):
    __tablename__ = "contract_mappings"
    __table_args__ = (UniqueConstraint("account_id", "leader_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    leader_symbol: Mapped[str] = mapped_column(String(10))
    follower_symbol: Mapped[str] = mapped_column(String(10))
    ratio: Mapped[float] = mapped_column(Float, default=1.0)

    account: Mapped[Account] = relationship(back_populates="mappings")


class RiskRules(Base):
    """Per-follower risk rules. Stored from M2, enforced from M4."""

    __tablename__ = "risk_rules"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    daily_loss_limit: Mapped[float | None] = mapped_column(Float, nullable=True)  # USD, positive number
    max_position: Mapped[int | None] = mapped_column(Integer, nullable=True)  # contracts per symbol
    trading_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    trading_start: Mapped[str] = mapped_column(String(5), default="09:30")  # New York time
    trading_end: Mapped[str] = mapped_column(String(5), default="16:00")
    trading_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4")  # Mon=0

    account: Mapped[Account] = relationship(back_populates="risk_rules")


class RiskEvent(Base):
    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_events_day", "trading_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    trading_day: Mapped[str] = mapped_column(String(10))
    ts: Mapped[float] = mapped_column(Float, default=_now)


class LeaderEvent(Base):
    __tablename__ = "leader_events"
    __table_args__ = (Index("ix_leader_events_received", "received_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # broker event id
    account_id: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(30))
    ts: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    leader_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    copied: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FollowerOrder(Base):
    __tablename__ = "follower_orders"
    __table_args__ = (
        Index("ix_follower_orders_event", "leader_event_id"),
        Index("ix_follower_orders_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    follower_id: Mapped[int] = mapped_column(Integer)
    leader_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    leader_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class SystemLog(Base):
    __tablename__ = "system_log"
    __table_args__ = (Index("ix_system_log_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=_now)
    level: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(20))
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
