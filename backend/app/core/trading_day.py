"""CME trading day and trading-hours helpers.

* The trading day follows the CME session: it rolls at 17:00 America/Chicago.
  A session that starts on Sunday or Monday evening belongs to the next
  calendar day, and the weekend belongs to Monday.
* Allowed trading hours are entered in New York time (ET).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    CHICAGO = ZoneInfo("America/Chicago")
    NEW_YORK = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError as exc:  # Windows has no built-in time zone database
    raise RuntimeError(
        'Time zone data is missing. Run `pip install -e ".[dev]"` in the backend folder '
        "(it installs the tzdata package)."
    ) from exc

SESSION_ROLL = time(17, 0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def trading_day(now: datetime) -> date:
    """The CME trade date `now` belongs to."""
    local = now.astimezone(CHICAGO)
    day = local.date() + timedelta(days=1) if local.time() >= SESSION_ROLL else local.date()
    while day.weekday() >= 5:  # Saturday/Sunday -> Monday
        day += timedelta(days=1)
    return day


def next_roll(now: datetime) -> datetime:
    """The next moment the trading day changes, in UTC."""
    current = trading_day(now)
    local = now.astimezone(CHICAGO)
    candidate = local.replace(hour=SESSION_ROLL.hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while trading_day(candidate) == current:  # e.g. Saturday and Sunday 17:00
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def within_trading_hours(now: datetime, start: str, end: str, days: Iterable[int]) -> bool:
    """True if `now` (any time zone) is inside the allowed window in New York time.

    `days` are weekdays with Monday = 0. A window with start == end is the whole
    day. A window that crosses midnight (start > end, e.g. 18:00-02:00) belongs
    to the weekday it starts on.
    """
    allowed = set(days)
    local = now.astimezone(NEW_YORK)
    start_t, end_t = parse_hhmm(start), parse_hhmm(end)
    current = local.time()
    weekday = local.weekday()
    if start_t == end_t:
        return weekday in allowed
    if start_t < end_t:
        return weekday in allowed and start_t <= current < end_t
    if current >= start_t:
        return weekday in allowed
    if current < end_t:
        return (weekday - 1) % 7 in allowed
    return False
