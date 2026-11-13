"""Pure sizing functions.

The follower always tracks a *target position* derived from the leader's
position: ``sign(q) * min(floor(|q| * multiplier * ratio), max_contracts)``.
Order sizes are the difference between targets before and after an order,
which keeps scale-in/scale-out rounding consistent (0.5x of 1+1 contracts is
1 contract, not 0+0).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_FLOOR, Decimal

from app.core.contracts import CONTRACTS
from app.engine.config import FollowerConfig


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def scaled_qty(leader_abs_qty: int, multiplier: float, ratio: float) -> int:
    """floor(qty * multiplier * ratio) computed in decimal to avoid float errors
    (0.29 * 100 must be 29, not 28)."""
    if leader_abs_qty < 0:
        raise ValueError("leader_abs_qty must be >= 0")
    value = Decimal(leader_abs_qty) * Decimal(str(multiplier)) * Decimal(str(ratio))
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def follower_targets(leader_positions: Mapping[str, int], cfg: FollowerConfig) -> dict[str, int]:
    """Target signed position for every follower symbol this follower manages."""
    targets: dict[str, int] = defaultdict(int)
    for symbol in managed_symbols(cfg):
        targets[symbol] = 0
    for leader_symbol, qty in leader_positions.items():
        if qty == 0:
            continue
        mapping = cfg.mapping_for(leader_symbol)
        targets[mapping.follower_symbol] += _sign(qty) * scaled_qty(
            abs(qty), cfg.multiplier, mapping.ratio
        )
    if cfg.max_contracts is not None:
        cap = cfg.max_contracts
        for symbol, qty in targets.items():
            targets[symbol] = max(-cap, min(cap, qty))
    return dict(targets)


def managed_symbols(cfg: FollowerConfig) -> set[str]:
    """Follower symbols the copier is responsible for (the image of the mapping)."""
    return {cfg.mapping_for(symbol).follower_symbol for symbol in CONTRACTS}


def order_delta(
    leader_positions: Mapping[str, int],
    cfg: FollowerConfig,
    leader_symbol: str,
    leader_signed_qty: int,
) -> tuple[str, int]:
    """Follower symbol and signed quantity that mirrors a leader order of
    ``leader_signed_qty`` given the leader's current positions."""
    follower_symbol = cfg.mapping_for(leader_symbol).follower_symbol
    before = follower_targets(leader_positions, cfg).get(follower_symbol, 0)
    after_positions = dict(leader_positions)
    after_positions[leader_symbol] = after_positions.get(leader_symbol, 0) + leader_signed_qty
    after = follower_targets(after_positions, cfg).get(follower_symbol, 0)
    return follower_symbol, after - before
