from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import TypedDict


class OfficeSchedule(TypedDict):
    office_tz: str
    open_minute: int
    close_minute: int
    closed_weekdays: list[int]
    source: str
    raw_hours: str | None
    raw_closed_days: str | None


_HOURS_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[~\-–ー〜]\s*(\d{1,2})\s*[:：]\s*(\d{2})")

_WEEKDAY_MAP: list[tuple[str, int]] = [
    ("月曜日", 0),
    ("月", 0),
    ("火曜日", 1),
    ("火", 1),
    ("水曜日", 2),
    ("水", 2),
    ("木曜日", 3),
    ("木", 3),
    ("金曜日", 4),
    ("金", 4),
    ("土曜日", 5),
    ("土", 5),
    ("日曜日", 6),
    ("日", 6),
    ("mon", 0),
    ("monday", 0),
    ("tue", 1),
    ("tuesday", 1),
    ("wed", 2),
    ("wednesday", 2),
    ("thu", 3),
    ("thursday", 3),
    ("fri", 4),
    ("friday", 4),
    ("sat", 5),
    ("saturday", 5),
    ("sun", 6),
    ("sunday", 6),
]


def _parse_minutes(hours_text: str | None) -> tuple[int, int] | None:
    if not hours_text:
        return None
    m = _HOURS_RE.search(hours_text)
    if not m:
        return None
    h1, m1, h2, m2 = [int(x) for x in m.groups()]
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def _parse_fallback_minutes(fallback_text: str) -> tuple[int, int]:
    parsed = _parse_minutes(fallback_text)
    if parsed:
        return parsed
    return 9 * 60, 19 * 60


def _parse_closed_weekdays(value: str | None) -> list[int]:
    if not value:
        return []
    normalized = value.strip().lower()
    if not normalized:
        return []
    if any(x in normalized for x in ("無休", "年中無休", "なし", "定休日なし", "no holidays", "open daily")):
        return []

    result: set[int] = set()
    for key, num in _WEEKDAY_MAP:
        if key in normalized:
            result.add(num)
    return sorted(result)


def build_office_schedule(
    *,
    raw_hours: str | None,
    raw_closed_days: str | None,
    office_timezone: str,
    fallback_hours: str,
) -> OfficeSchedule:
    minutes = _parse_minutes(raw_hours)
    source = "dealer_hours"
    if not minutes:
        minutes = _parse_fallback_minutes(fallback_hours)
        source = "fallback"
    closed = _parse_closed_weekdays(raw_closed_days)
    return OfficeSchedule(
        office_tz=office_timezone,
        open_minute=minutes[0],
        close_minute=minutes[1],
        closed_weekdays=closed,
        source=source,
        raw_hours=raw_hours,
        raw_closed_days=raw_closed_days,
    )


def _is_open_with_minutes(schedule: OfficeSchedule, now_jst: datetime) -> bool:
    weekday = now_jst.weekday()
    if weekday in schedule["closed_weekdays"]:
        return False
    now_minutes = now_jst.hour * 60 + now_jst.minute
    open_min = schedule["open_minute"]
    close_min = schedule["close_minute"]
    if open_min < close_min:
        return open_min <= now_minutes < close_min
    # crossing midnight
    return now_minutes >= open_min or now_minutes < close_min


def is_open_now(schedule: OfficeSchedule, now_jst: datetime) -> bool:
    return _is_open_with_minutes(schedule, now_jst)


def next_opening_utc(schedule: OfficeSchedule, now_jst: datetime) -> datetime:
    open_min = schedule["open_minute"]
    open_h = open_min // 60
    open_m = open_min % 60

    for day_offset in range(0, 15):
        candidate_day = (now_jst + timedelta(days=day_offset)).date()
        weekday = candidate_day.weekday()
        if weekday in schedule["closed_weekdays"]:
            continue
        candidate = datetime.combine(candidate_day, time(open_h, open_m), tzinfo=now_jst.tzinfo)
        if day_offset == 0 and candidate <= now_jst:
            continue
        return candidate.astimezone(timezone.utc)

    # Fallback-safe: if everything looks closed, retry in 24h.
    return (now_jst + timedelta(days=1)).astimezone(timezone.utc)

