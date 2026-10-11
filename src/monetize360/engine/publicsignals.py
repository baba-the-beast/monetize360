"""Public read-only signals derived from as_of in the pack timezone: is_weekend, weekday, hour, minute, day, month,
and calendar signals (festival, or any enum the pack's `calendar:` section names).

Dates belong to the PACK (`calendar:`), so a new festival, country or sale day never needs an engine change, and
they are versioned with the pack, so old prices replay exactly. The table below is only the default for `festival`
when a pack has no calendar. It is NOT the `holidays` library: library data changes between versions.
Dates must be verified by a human (BUILD_GUIDE research item R1) — a wrong date prices a whole day wrongly.
"""
from __future__ import annotations

from datetime import date, datetime

# name -> required signal type
PUBLIC_SIGNALS = {"is_weekend": "bool", "weekday": "enum", "hour": "int", "minute": "int", "day": "int",
                  "month": "int", "festival": "enum"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Verified 2026-10-11 (BUILD_GUIDE R1). 2026: central government holiday list (DoPT order of 3 Jul 2025) —
# Holi 4 Mar, Diwali 8 Nov. 2025 and 2027: Drik / Dekho Panchang (Holi 14 Mar 2025, 22 Mar 2027; Diwali
# 20 Oct 2025, 29 Oct 2027). Some regions play Holi a day earlier (3 Mar 2026): a pack that needs regional dates
# carries its own `calendar:` section. Add 2028+ before late 2027 (the linter warns: calendar_ends).
FESTIVALS: dict[date, str] = {
    date(2025, 3, 14): "holi",
    date(2025, 10, 20): "diwali",
    date(2026, 3, 4): "holi",
    date(2026, 11, 8): "diwali",
    date(2027, 3, 22): "holi",
    date(2027, 10, 29): "diwali",
}


DEFAULT_CALENDARS = {"festival": FESTIVALS}


def compute(name: str, local: datetime, calendar: dict[date, str] | None) -> bool | int | str:
    """Value of public signal `name` at local time `local` (already converted to the pack timezone)."""
    if calendar is not None:
        return calendar.get(local.date(), "none")
    if name == "is_weekend":
        return local.weekday() >= 5
    if name == "weekday":
        return WEEKDAYS[local.weekday()]
    if name == "hour":
        return local.hour
    if name == "minute":
        return local.minute
    if name == "day":
        return local.day
    if name == "month":
        return local.month
    raise KeyError(name)
