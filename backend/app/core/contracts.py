"""Contract specifications for the supported US index futures.

Symbols are root symbols (``NQ``, ``MNQ`` ...). Real broker adapters are
responsible for translating a root symbol to the active contract month
(e.g. ``NQZ6``); the copier itself only ever reasons in root symbols.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str
    underlying: str  # Index family. Contracts with the same underlying share one price.
    point_value: float  # USD per 1.0 index point per contract
    tick_size: float
    description: str

    @property
    def tick_value(self) -> float:
        return self.point_value * self.tick_size


CONTRACTS: dict[str, ContractSpec] = {
    "NQ": ContractSpec("NQ", "NASDAQ100", 20.0, 0.25, "E-mini Nasdaq-100"),
    "MNQ": ContractSpec("MNQ", "NASDAQ100", 2.0, 0.25, "Micro E-mini Nasdaq-100"),
    "ES": ContractSpec("ES", "SP500", 50.0, 0.25, "E-mini S&P 500"),
    "MES": ContractSpec("MES", "SP500", 5.0, 0.25, "Micro E-mini S&P 500"),
    "YM": ContractSpec("YM", "DOW30", 5.0, 1.0, "E-mini Dow"),
    "MYM": ContractSpec("MYM", "DOW30", 0.5, 1.0, "Micro E-mini Dow"),
    "RTY": ContractSpec("RTY", "RUSSELL2000", 50.0, 0.1, "E-mini Russell 2000"),
    "M2K": ContractSpec("M2K", "RUSSELL2000", 5.0, 0.1, "Micro E-mini Russell 2000"),
}

# Tick size of each underlying's price series (the finest tick of its contracts).
UNDERLYING_TICK: dict[str, float] = {
    "NASDAQ100": 0.25,
    "SP500": 0.25,
    "DOW30": 1.0,
    "RUSSELL2000": 0.1,
}

# Starting prices for the simulator. Override them via settings or move the
# price by hand in the simulator panel.
DEFAULT_PRICES: dict[str, float] = {
    "NASDAQ100": 25000.0,
    "SP500": 6800.0,
    "DOW30": 47000.0,
    "RUSSELL2000": 2450.0,
}


class UnknownSymbolError(ValueError):
    pass


def get_spec(symbol: str) -> ContractSpec:
    try:
        return CONTRACTS[symbol.upper()]
    except KeyError as exc:
        raise UnknownSymbolError(f"Unknown symbol: {symbol}") from exc


def underlying_of(symbol: str) -> str:
    return get_spec(symbol).underlying


def same_underlying(a: str, b: str) -> bool:
    return underlying_of(a) == underlying_of(b)


def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the nearest tick, avoiding float noise like 25000.250000001."""
    ticks = math.floor(price / tick + 0.5)
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(ticks * tick, decimals + 2)


def pnl(symbol: str, entry_price: float, exit_price: float, signed_qty: int) -> float:
    """P&L in USD for ``signed_qty`` contracts (positive = long) moved from entry to exit."""
    spec = get_spec(symbol)
    return round((exit_price - entry_price) * signed_qty * spec.point_value, 2)
