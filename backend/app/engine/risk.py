"""Risk hooks used by the copy engine.

M1 ships the interface and a permissive default. The per-follower rules
(daily loss limit, max position, trading hours) are implemented in M4.
"""

from __future__ import annotations

from typing import Protocol

from app.brokers.base import OrderRequest


class RiskGuard(Protocol):
    def check_order(
        self, follower_id: str, request: OrderRequest, current_position: int, reduces_exposure: bool
    ) -> str | None:
        """Return a reason string to block the order, or None to allow it."""
        ...


class AllowAll:
    def check_order(
        self, follower_id: str, request: OrderRequest, current_position: int, reduces_exposure: bool
    ) -> str | None:
        return None
