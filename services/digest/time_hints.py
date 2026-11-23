"""Relative Moments time hints → absolute approximate timestamps.

"2 hour（s） ago" / "2小时前" / "昨天 14:30" / "Nov 3" are only renderable
relative to the capture moment, so we convert at collection time and store
`posted_at_approx` (minute precision "YYYY-MM-DD HH:MM", or date-only when
the hint carries no clock) for stable sorting. The raw hint is kept in
`posted_at` as evidence. Both zh and en UIs are handled; matching runs on
the fullwidth-normalized form (Vision renders ASCII parens as fullwidth).
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from .ocr_extract import split_time_row_prefix

_ZH_WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_EN_MONTH = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_EN_WEEK = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_UNIT_SECONDS = {
    "second": 1, "秒": 1,
    "minute": 60, "分钟": 60,
    "hour": 3600, "小时": 3600,
    "day": 86400, "天": 86400,
    "week": 604800, "周": 604800, "个星期": 604800,
    "month": 2592000, "个月": 2592000,
    "year": 31536000, "年": 31536000,
}

_CLOCK = r"(\d{1,2}):(\d{2})(?::(\d{2}))?"
_CLOCK_CORE = r"\d{1,2}:\d{2}(?::\d{2})?"  # spliceable, non-capturing form


def _with_clock(base: datetime, h: str, m: str, s: str = None,
                ampm: str = "") -> datetime:
    hour = int(h)
    ap = (ampm or "").strip().lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    return base.replace(hour=hour, minute=int(m), second=int(s or 0), microsecond=0)


def _fmt(dt: datetime, with_clock: bool) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if with_clock else dt.strftime("%Y-%m-%d")


def _weekday_date(now: datetime, target_weekday: int) -> datetime:
    """Most recent occurrence of that weekday, excluding today (delta 0 → 7
    days back), matching WeChat's "within the last week" rendering."""
    delta = (now.weekday() - target_weekday) % 7 or 7
    return now - timedelta(days=delta)


def approx_posted_at(hint: str, now: Optional[datetime] = None) -> Optional[str]:
    """Absolute approximate time for a time_hint, or None when unparseable.

    Channel/video posts store the time row with an inline source suffix
    ("21 hour（s） ago Channels•画家任重") — the leading timestamp is
    split off (split_time_row_prefix) before parsing; the raw hint is kept
    elsewhere as evidence."""
    if not hint or not hint.strip():
        return None
    now = now or datetime.now()
    n, _rest = split_time_row_prefix(hint)
    if not n:
        return None

    # relative: "N unit(s) ago" / "N单位前"
    m = re.match(r"^(\d+) ?(second|minute|hour|day|week|month|year)(?:s|\(s\))? ago$", n)
    if not m:
        m = re.match(r"^(\d+) ?(秒|分钟|小时|天|周|个星期|个月|年)前$", n)
    if m:
        return _fmt(now - timedelta(seconds=int(m.group(1)) *
                                    _UNIT_SECONDS[m.group(2)]), True)
    if n in ("just now", "刚刚"):
        return _fmt(now, True)
    m = re.match(r"^an? (second|minute|hour|day) ago$", n)
    if m:
        return _fmt(now - timedelta(seconds=_UNIT_SECONDS[m.group(1)]), True)
    if n in ("半小时前",):
        return _fmt(now - timedelta(minutes=30), True)
    if n in ("半天前",):
        return _fmt(now - timedelta(hours=12), True)

    # yesterday / 前天, optional clock
    m = re.match(r"^(yesterday|昨天)(?: at | )?" + _CLOCK + r"( (am|pm))?$", n)
    if m:
        base = now - timedelta(days=1)
        return _fmt(_with_clock(base, m.group(2), m.group(3), m.group(4),
                                m.group(5) or ""), True)
    if n in ("yesterday", "昨天"):
        return _fmt(now - timedelta(days=1), False)
    if n in ("前天",):
        return _fmt(now - timedelta(days=2), False)

    # weekday (within the last week), optional clock
    m = re.match(r"^星期([一二三四五六日天])(?: (" + _CLOCK_CORE + r"))?$", n) \
        or re.match(r"^周([一二三四五六日天])(?: (" + _CLOCK_CORE + r"))?$", n)
    if m:
        base = _weekday_date(now, _ZH_WEEK[m.group(1)])
        if m.group(2):
            parts = re.match(_CLOCK, m.group(2))
            base = _with_clock(base, *parts.groups())
            return _fmt(base, True)
        return _fmt(base, False)
    m = re.match(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*?(?: at )?" + _CLOCK + r"( (am|pm))?$", n) \
        or re.match(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*$", n)
    if m:
        base = _weekday_date(now, _EN_WEEK[m.group(1)])
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            base = _with_clock(base, m.group(2), m.group(3), m.group(4),
                               m.group(5) or "")
            return _fmt(base, True)
        return _fmt(base, False)

    # absolute dates
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", n)
    if m:
        try:
            return _fmt(datetime(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3))), False)
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})月(\d{1,2})日(?: (" + _CLOCK_CORE + r"))?$", n)
    if m:
        return _fmt(_abs_date_clock(now, int(m.group(1)), int(m.group(2)),
                                    m.group(3)), bool(m.group(3)))
    m = re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? "
                 r"(\d{1,2})(?: at (\d{1,2}):(\d{2})(?::(\d{2}))?( (am|pm))?)?$", n)
    if m:
        base = _abs_date_clock(now, _EN_MONTH[m.group(1)], int(m.group(2)), None)
        if m.group(3):
            return _fmt(_with_clock(base, m.group(3), m.group(4),
                                    m.group(5), m.group(7) or ""), True)
        return _fmt(base, False)
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})$", n)
    if m:
        return _fmt(_abs_date_clock(now, int(m.group(1)), int(m.group(2)), None), False)

    return None


def _abs_date_clock(now: datetime, month: int, day: int,
                    clock: Optional[str]) -> datetime:
    """This-year date; a future date means last year. Optional HH:MM."""
    try:
        dt = now.replace(month=month, day=day, hour=0, minute=0,
                         second=0, microsecond=0)
    except ValueError:  # impossible date (2月30日 OCR noise)
        dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dt > now:
        try:
            dt = dt.replace(year=dt.year - 1)
        except ValueError:  # Feb 29 → non-leap previous year
            dt = dt.replace(year=dt.year - 1, day=28)
    if clock:
        parts = re.match(_CLOCK, clock)
        dt = _with_clock(dt, *parts.groups())
    return dt
