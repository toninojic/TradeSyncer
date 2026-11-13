"""Configuration objects for the copy engine (persisted in the DB from M2 on)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.contracts import get_spec, same_underlying


class MismatchMode(StrEnum):
    AUTO_CORRECT = "auto_correct"  # reconciliation sends market orders to fix the position
    ALERT_ONLY = "alert_only"  # reconciliation only raises an alert


@dataclass(slots=True)
class ContractMapping:
    leader_symbol: str
    follower_symbol: str
    ratio: float = 1.0  # follower contracts per leader contract, e.g. NQ -> MNQ = 10

    def validate(self) -> None:
        get_spec(self.leader_symbol)
        get_spec(self.follower_symbol)
        if self.ratio <= 0:
            raise ValueError("Mapping ratio must be positive")
        if not same_underlying(self.leader_symbol, self.follower_symbol):
            raise ValueError(
                f"Cannot map {self.leader_symbol} to {self.follower_symbol}: different index"
            )


@dataclass(slots=True)
class FollowerConfig:
    account_id: str
    name: str = ""
    multiplier: float = 1.0
    max_contracts: int | None = None
    mappings: list[ContractMapping] = field(default_factory=list)
    copy_sl_tp: bool = True
    mismatch_mode: MismatchMode = MismatchMode.AUTO_CORRECT
    copy_enabled: bool = True

    def validate(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("Multiplier must be positive")
        if self.max_contracts is not None and self.max_contracts < 0:
            raise ValueError("max_contracts cannot be negative")
        seen: set[str] = set()
        for mapping in self.mappings:
            mapping.validate()
            if mapping.leader_symbol in seen:
                raise ValueError(f"Duplicate mapping for {mapping.leader_symbol}")
            seen.add(mapping.leader_symbol)

    def mapping_for(self, leader_symbol: str) -> ContractMapping:
        for mapping in self.mappings:
            if mapping.leader_symbol == leader_symbol:
                return mapping
        return ContractMapping(leader_symbol, leader_symbol, 1.0)


@dataclass(slots=True)
class EngineSettings:
    reconcile_interval_s: float = 3.0
    # How long a follower's mirrored order may lag behind the leader's fill
    # before reconciliation cancels it and corrects with a market order.
    linked_fill_grace_s: float = 2.0
    order_timeout_s: float = 10.0
    # A working follower market order (e.g. the remainder of a partial fill)
    # older than this is cancelled and replaced.
    stale_market_order_s: float = 5.0
    # Consecutive auto-corrections before reconciliation gives up and only alerts.
    max_correction_attempts: int = 3
    dedup_window: int = 50_000
    latency_samples: int = 50
