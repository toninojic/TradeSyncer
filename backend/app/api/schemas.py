"""Request/response models for the REST API."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.contracts import CONTRACTS

BrokerType = Literal["sim", "tradovate"]
Role = Literal["leader", "follower"]
MismatchModeT = Literal["auto_correct", "alert_only"]
SymbolT = Literal["NQ", "MNQ", "ES", "MES", "YM", "MYM", "RTY", "M2K"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


# ----------------------------------------------------------------------- accounts


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker_type: BrokerType = "sim"
    role: Role = "follower"
    enabled: bool = True
    sim_starting_balance: float = Field(default=50_000.0, gt=0)
    # Write-only. Never returned by the API and never logged.
    credentials: dict[str, str] | None = None


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    broker_type: BrokerType | None = None
    role: Role | None = None
    enabled: bool | None = None
    sim_starting_balance: float | None = Field(default=None, gt=0)
    credentials: dict[str, str] | None = None  # {} clears saved credentials


class AccountOut(BaseModel):
    id: int
    name: str
    broker_type: str
    role: str
    enabled: bool
    has_credentials: bool
    sim_starting_balance: float
    connection: str
    created_at: float
    updated_at: float


class MappingIn(BaseModel):
    leader_symbol: SymbolT
    follower_symbol: SymbolT
    ratio: float = Field(gt=0, le=1000)


class CopySettingsIn(BaseModel):
    multiplier: float = Field(gt=0, le=100)
    max_contracts: int | None = Field(default=None, ge=1, le=10_000)
    copy_sl_tp: bool = True
    mismatch_mode: MismatchModeT = "auto_correct"
    copy_enabled: bool = True
    mappings: list[MappingIn] = Field(default_factory=list)


class CopySettingsOut(CopySettingsIn):
    account_id: int


class RiskRulesIn(BaseModel):
    daily_loss_limit: float | None = Field(default=None, gt=0)
    max_position: int | None = Field(default=None, ge=1, le=10_000)
    trading_hours_enabled: bool = False
    trading_start: str = "09:30"
    trading_end: str = "16:00"
    trading_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])

    @field_validator("trading_start", "trading_end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("Use HH:MM (24h), e.g. 09:30")
        return value

    @field_validator("trading_days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in value):
            raise ValueError("Days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(value))


class RiskRulesOut(RiskRulesIn):
    account_id: int


# ----------------------------------------------------------------------- simulator


class SimPriceIn(BaseModel):
    symbol: str = "NQ"
    price: float | None = Field(default=None, gt=0)
    move: float | None = None  # points, e.g. +10 or -5

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        value = value.upper()
        underlyings = {c.underlying for c in CONTRACTS.values()}
        if value not in CONTRACTS and value not in underlyings:
            raise ValueError("Unknown symbol")
        return value


class SimWalkIn(BaseModel):
    running: bool


class SimOrderIn(BaseModel):
    symbol: SymbolT = "NQ"
    side: Literal["buy", "sell"]
    qty: int = Field(ge=1, le=1000)
    type: Literal["market", "limit", "stop"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class SimCloseIn(BaseModel):
    symbol: SymbolT | None = None


class SimModifyIn(BaseModel):
    qty: int | None = Field(default=None, ge=1)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)


class SimFailureIn(BaseModel):
    action: Literal[
        "disconnect", "restore", "reject_next", "partial_next", "latency", "duplicate_next", "clear"
    ]
    count: int = Field(default=1, ge=0, le=100)
    duration_s: float | None = Field(default=None, gt=0, le=3600)
    reason: str | None = Field(default=None, max_length=200)
    ratio: float = Field(default=0.5, gt=0, lt=1)
    remainder_delay_s: float | None = Field(default=1.0, ge=0, le=600)
    latency_ms: float = Field(default=0, ge=0, le=30_000)
