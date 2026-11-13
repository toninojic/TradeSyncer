"""Pure reconciliation decision logic (the loop itself lives in the follower worker)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.engine.config import MismatchMode


class SyncStatus(StrEnum):
    IN_SYNC = "in_sync"
    PENDING = "pending"  # orders in flight will bring the follower to target
    OUT_OF_SYNC = "out_of_sync"
    WAITING = "waiting_for_leader_flat"  # not copying until the leader is flat
    PAUSED = "paused"
    DISABLED = "disabled"
    DISCONNECTED = "disconnected"
    RISK_LOCKED = "risk_locked"  # daily loss limit hit: flattened, not copying


class ReconcileAction(StrEnum):
    NONE = "none"
    WAIT = "wait"
    CORRECT = "correct"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    action: ReconcileAction
    status: SyncStatus
    correction: int  # signed quantity to send (CORRECT) or the size of the gap (ALERT)
    reason: str


def decide(
    *,
    target: int,
    actual: int,
    pending: int,
    mode: MismatchMode,
    attempts: int,
    max_attempts: int,
) -> ReconcileDecision:
    """Decide what to do for one follower symbol.

    ``pending`` is the signed quantity that orders already in flight are
    expected to add (working market orders, mirrored orders whose leader
    counterpart already filled).
    """
    if actual == target and pending == 0:
        return ReconcileDecision(ReconcileAction.NONE, SyncStatus.IN_SYNC, 0, "in sync")
    gap = target - actual - pending
    if gap == 0:
        return ReconcileDecision(
            ReconcileAction.WAIT, SyncStatus.PENDING, 0, "waiting for working orders"
        )
    if mode is MismatchMode.ALERT_ONLY:
        return ReconcileDecision(
            ReconcileAction.ALERT, SyncStatus.OUT_OF_SYNC, gap, "alert-only mode"
        )
    if attempts >= max_attempts:
        return ReconcileDecision(
            ReconcileAction.ALERT, SyncStatus.OUT_OF_SYNC, gap,
            f"auto-correct gave up after {attempts} attempts",
        )
    return ReconcileDecision(ReconcileAction.CORRECT, SyncStatus.OUT_OF_SYNC, gap, "auto-correct")
