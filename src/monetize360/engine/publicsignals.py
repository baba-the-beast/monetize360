"""Public read-only signals derived from as_of in the pack timezone: is_weekend, weekday, hour, festival.

The festival dates are a built-in table, NOT the `holidays` library: library data changes between versions,
and a changed date would make an old price replay differently. Dates must be verified by a human
(BUILD_GUIDE research item R1) — a wrong date here prices a whole day wrongly.
"""
from __future__ import annotations

from datetime import date, datetime

# name -> required signal type
PUBLIC_SIGNALS = {"is_weekend": "bool", "weekday": "enum", "hour": "int", "festival": "enum"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# TODO(R1): verify every date against an official calendar before the demo.
FESTIVALS: dict[date, str] = {
    date(2025, 3, 14): "holi",
    date(2025, 10, 20): "diwali",
    date(2026, 3, 4): "holi",
    date(2026, 11, 8): "diwali",
    date(2027, 3, 22): "holi",
    date(2027, 10, 29): "diwali",
}


def compute(name: str, local: datetime, allowed_values: tuple[str, ...]) -> bool | int | str:
    """Value of public signal `name` at local time `local` (already converted to the pack timezone)."""
    if name == "is_weekend":
        return local.weekday() >= 5
    if name == "weekday":
        return WEEKDAYS[local.weekday()]
    if name == "hour":
        return local.hour
    if name == "festival":
        festival = FESTIVALS.get(local.date(), "none")
        return festival if festival in allowed_values else "none"
    raise KeyError(name)
