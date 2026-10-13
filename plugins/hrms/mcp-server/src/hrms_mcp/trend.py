"""One employee's leave over a period, counted — for ``get_leave_trend``.

Pure: no I/O. The tool fetches the HRMS's records and hands them here, so every
rule below is tested on fixed dates and the model only words what this counts.
A figure an HRMS admin repeats about a person must be exact, so nothing here is
estimated, and what is *notable* is decided by the fixed thresholds below — never
by the model.

Days are counted the way the HRMS counts a leave's own value: Saturdays,
Sundays and every holiday in its calendar are not working days, and a half day
is half a day. Only **approved** leave counts as leave taken.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, get_args

#: The periods an HRMS admin can name; the dates are worked out here, from the
#: HRMS's own today, so "last quarter" can never be off by a month.
Period = Literal[
    "last 3 months", "last 6 months", "last 12 months",
    "this quarter", "last quarter", "this year", "last year", "between dates",
]
PERIODS: tuple[str, ...] = get_args(Period)
DEFAULT_PERIOD: Period = "last 6 months"

#: The longest period "between dates" may span.
MAX_MONTHS = 24

# What is notable. Candidates for plugin settings once HR has a view on them.
#: Fewer approved days than this, and no pattern is drawn at all.
MIN_DAYS_FOR_A_PATTERN = 3
#: Monday + Friday days are notable from this many, and this share of all days…
EDGE_DAYS_MIN = 4
EDGE_SHARE_MIN = 0.6
#: …against the share an even spread over five weekdays gives.
EVEN_EDGE_SHARE = 0.4
#: A change against the period before is notable from this many days and share.
CHANGE_DAYS_MIN = 3
CHANGE_SHARE_MIN = 0.5
#: A break this many consecutive days off or longer is a long break…
LONG_BREAK_DAYS = 3
#: …and this many of them are notable.
LONG_BREAKS_NOTABLE = 3
#: The same leave type on the same weekday this many times is a repeat.
REPEATS_NOTABLE = 3

STATUSES = ("Approved", "Pending", "Rejected", "Cancelled")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
#: How many upcoming leaves are listed.
UPCOMING_LIMIT = 5


# --- dates --------------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    """The same day ``months`` later (or earlier), the 31st becoming the month's last."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


def quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


@dataclass(frozen=True)
class Span:
    """The period analysed and the one of equal length just before it."""

    start: date
    end: date
    before_start: date
    before_end: date
    #: "the last 6 months", "last quarter" — how the period is said.
    label: str


def span_of(period: str, today: date, date_from: str = "", date_to: str = "") -> Span:
    """The dates a named period means, ending today unless it lies in the past.

    Raises ``ValueError`` with a sentence the assistant can relay."""
    if period not in PERIODS:
        raise ValueError(f"The period must be one of: {', '.join(PERIODS)}.")
    if period.startswith("last ") and period.endswith(" months"):
        months = int(period.split()[1])
        start = add_months(today, -months)
        return _span(start, today, months, f"the last {months} months")
    if period == "this quarter":
        return _span(quarter_start(today), today, 3, "this quarter so far")
    if period == "last quarter":
        start = add_months(quarter_start(today), -3)
        return _span(start, quarter_start(today) - timedelta(days=1), 3, "last quarter")
    if period == "this year":
        return _span(date(today.year, 1, 1), today, 12, "this year so far")
    if period == "last year":
        return _span(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), 12, "last year")
    return _between(date_from, date_to, today)


def _span(start: date, end: date, months: int, label: str) -> Span:
    # The same stretch one period earlier: a partial quarter against the same
    # days of the quarter before, never against a whole one.
    before_end = min(add_months(end, -months), start - timedelta(days=1))
    return Span(start, end, add_months(start, -months), before_end, label)


def _between(date_from: str, date_to: str, today: date) -> Span:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to) if date_to else today
    except ValueError:
        raise ValueError(
            "With \"between dates\", give date_from (and optionally date_to) as YYYY-MM-DD."
        ) from None
    # A trend is about leave already taken; days still ahead are listed apart.
    end = min(end, today)
    if end < start:
        raise ValueError("The period ends before it starts.")
    if start < add_months(end, -MAX_MONTHS):
        raise ValueError(f"A period can be at most {MAX_MONTHS} months long.")
    length = (end - start).days + 1
    before_end = start - timedelta(days=1)
    return Span(start, end, before_end - timedelta(days=length - 1), before_end,
                "the period asked about")


# --- leave records ------------------------------------------------------------


@dataclass(frozen=True)
class Leave:
    kind: str
    start: date
    end: date
    half: bool
    status: str


def leaves_of(records: Iterable[dict[str, Any]], kind_of: Callable[[str], str]) -> list[Leave]:
    """The HRMS's ``get_leaves`` records as leaves; one with unreadable dates is
    skipped rather than guessed at."""
    found = []
    for record in records:
        try:
            start = date.fromisoformat(str(record.get("leave_date_from", ""))[:10])
            end_text = str(record.get("leave_date_to") or "")[:10]
            end = date.fromisoformat(end_text) if end_text else start
        except ValueError:
            continue
        mode = " ".join(str(record.get("leave_mode") or "").replace("_", " ").split()).lower()
        found.append(Leave(
            kind=kind_of(str(record.get("leave_type") or "")),
            start=start,
            end=max(start, end),
            half=mode in ("first half", "second half"),
            status=str(record.get("status") or ""),
        ))
    return found


def holidays_of(records: Iterable[dict[str, Any]]) -> set[date]:
    """Every holiday in the HRMS's calendar — it counts each one, optional ones
    too, as a day not worked when it values a leave."""
    days = set()
    for record in records:
        try:
            days.add(date.fromisoformat(str(record.get("date", ""))[:10]))
        except ValueError:
            continue
    return days


def is_working(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def days_of(leave: Leave, holidays: set[date]) -> dict[date, float]:
    """Each working day of a leave and how much of it was taken."""
    if leave.half:
        return {leave.start: 0.5} if is_working(leave.start, holidays) else {}
    taken = {}
    day = leave.start
    while day <= leave.end:
        if is_working(day, holidays):
            taken[day] = 1.0
        day += timedelta(days=1)
    return taken


def overlaps(leave: Leave, start: date, end: date) -> bool:
    return leave.start <= end and leave.end >= start


# --- the summary --------------------------------------------------------------


@dataclass(frozen=True)
class LongBreak:
    #: The leave days in it, first and last.
    leave_from: date
    leave_to: date
    #: The whole break, weekends and holidays included.
    off_from: date
    off_to: date
    bridge: bool

    @property
    def length(self) -> int:
        return (self.off_to - self.off_from).days + 1


@dataclass(frozen=True)
class Repeat:
    kind: str
    weekday: str
    dates: tuple[date, ...]


@dataclass
class Trend:
    span: Span
    today: date
    requests: dict[str, int] = field(default_factory=dict)
    days: float = 0.0
    by_type: dict[str, tuple[float, int]] = field(default_factory=dict)
    by_month: list[tuple[date, float]] = field(default_factory=list)
    #: Approved days by (first day of the month, leave type).
    by_month_type: dict[tuple[date, str], float] = field(default_factory=dict)
    #: Approved requests in the period, as single days (one day or less) or not.
    single_requests: int = 0
    longer_requests: int = 0
    #: Approved requests in the period with a day inside a long break.
    requests_in_breaks: int = 0
    by_weekday: dict[str, float] = field(default_factory=dict)
    long_breaks: list[LongBreak] = field(default_factory=list)
    repeats: list[Repeat] = field(default_factory=list)
    days_before: float = 0.0
    requests_before: int = 0
    upcoming: list[Leave] = field(default_factory=list)

    @property
    def edge_days(self) -> float:
        return self.by_weekday.get("Mon", 0.0) + self.by_weekday.get("Fri", 0.0)


def _clipped(leave: Leave, holidays: set[date], start: date, end: date) -> dict[date, float]:
    return {day: n for day, n in days_of(leave, holidays).items() if start <= day <= end}


def _taken_within(
    leaves: list[Leave], holidays: set[date], start: date, end: date
) -> dict[date, float]:
    """Approved leave taken on each working day from ``start`` to ``end``."""
    taken: dict[date, float] = {}
    for leave in leaves:
        if leave.status == "Approved":
            for day, amount in _clipped(leave, holidays, start, end).items():
                taken[day] = taken.get(day, 0.0) + amount
    return taken


def summarise(leaves: list[Leave], holidays: set[date], span: Span, today: date) -> Trend:
    trend = Trend(span=span, today=today)
    within = [leave for leave in leaves if overlaps(leave, span.start, span.end)]
    trend.requests = {
        status: sum(1 for leave in within if leave.status == status) for status in STATUSES
    }
    taken = _taken_within(leaves, holidays, span.start, span.end)
    trend.days = sum(taken.values())

    for leave in within:
        if leave.status != "Approved":
            continue
        clipped = _clipped(leave, holidays, span.start, span.end)
        days = sum(clipped.values())
        total, count = trend.by_type.get(leave.kind, (0.0, 0))
        trend.by_type[leave.kind] = (total + days, count + 1)
        for day, amount in clipped.items():
            key = (date(day.year, day.month, 1), leave.kind)
            trend.by_month_type[key] = trend.by_month_type.get(key, 0.0) + amount
        if clipped:
            whole = sum(days_of(leave, holidays).values())
            if whole <= 1:
                trend.single_requests += 1
            else:
                trend.longer_requests += 1

    month = date(span.start.year, span.start.month, 1)
    while month <= span.end:
        in_month = sum(
            amount for day, amount in taken.items()
            if (day.year, day.month) == (month.year, month.month)
        )
        trend.by_month.append((month, in_month))
        month = add_months(month, 1)

    trend.by_weekday = {name: 0.0 for name in WEEKDAYS}
    for day, amount in taken.items():
        trend.by_weekday[WEEKDAYS[day.weekday()]] += amount

    trend.long_breaks = long_breaks(leaves, holidays, span)
    trend.requests_in_breaks = sum(
        1 for leave in within
        if leave.status == "Approved" and any(
            b.leave_from <= day <= b.leave_to
            for day in _clipped(leave, holidays, span.start, span.end)
            for b in trend.long_breaks
        )
    )
    trend.repeats = repeats(leaves, holidays, span)

    before = _taken_within(leaves, holidays, span.before_start, span.before_end)
    trend.days_before = sum(before.values())
    trend.requests_before = sum(
        1 for leave in leaves if overlaps(leave, span.before_start, span.before_end)
    )
    trend.upcoming = sorted(
        (leave for leave in leaves
         if leave.start > max(today, span.end) and leave.status in ("Approved", "Pending")),
        key=lambda leave: leave.start,
    )
    return trend


def long_breaks(leaves: list[Leave], holidays: set[date], span: Span) -> list[LongBreak]:
    """Runs of whole leave days that, with the weekends and holidays touching
    them, make a break of ``LONG_BREAK_DAYS`` or more consecutive days off.

    A run of leave on working days alone is not one: it must join a weekend or
    a holiday. A half day is a day worked, so it joins nothing."""
    leave_days = {
        day for leave in leaves
        if leave.status == "Approved" and not leave.half
        for day in days_of(leave, holidays)
        if span.start <= day <= span.end
    }

    def off(day: date) -> bool:
        return day in leave_days or not is_working(day, holidays)

    found: list[LongBreak] = []
    seen: set[date] = set()
    for day in sorted(leave_days):
        if day in seen:
            continue
        first = last = day
        while off(first - timedelta(days=1)):
            first -= timedelta(days=1)
        while off(last + timedelta(days=1)):
            last += timedelta(days=1)
        run = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        on_leave = [d for d in run if d in leave_days]
        seen.update(on_leave)
        if len(run) < LONG_BREAK_DAYS or len(on_leave) == len(run):
            continue
        # A bridge day: one working day taken off between two days off that are
        # not leave — a holiday on one side, a weekend or holiday on the other.
        bridge = len(on_leave) == 1 and first < on_leave[0] < last and any(
            d in holidays for d in (on_leave[0] - timedelta(days=1),
                                    on_leave[0] + timedelta(days=1))
        )
        found.append(LongBreak(on_leave[0], on_leave[-1], first, last, bridge))
    return found


def repeats(leaves: list[Leave], holidays: set[date], span: Span) -> list[Repeat]:
    """The same leave type taken as a single day on the same weekday
    ``REPEATS_NOTABLE`` times or more. Only single days count: a week away
    touches every weekday and says nothing about one of them."""
    single: dict[tuple[str, str], list[date]] = {}
    for leave in leaves:
        if leave.status != "Approved":
            continue
        days = list(_clipped(leave, holidays, span.start, span.end))
        if len(days_of(leave, holidays)) != 1 or not days:
            continue
        single.setdefault((leave.kind, WEEKDAYS[days[0].weekday()]), []).append(days[0])
    return [
        Repeat(kind, weekday, tuple(sorted(dates)))
        for (kind, weekday), dates in sorted(single.items(), key=lambda item: min(item[1]))
        if len(dates) >= REPEATS_NOTABLE
    ]


# --- what is notable ----------------------------------------------------------


def notable(trend: Trend) -> list[str]:
    """The observations worth pointing out, by the thresholds above. Each says
    counts and dates; none says why."""
    if trend.days < MIN_DAYS_FOR_A_PATTERN:
        return ["Too few leave days in this period to show a pattern."]
    said = []
    edge = trend.edge_days
    if _edge_notable(trend):
        said.append(
            f"{edge:g} of {trend.days:g} leave days were on a Monday or Friday; an even "
            f"spread would be about {trend.days * EVEN_EDGE_SHARE:.1f}."
        )
    if _change_notable(trend):
        said.append(
            f"{_days(trend.days)} this period against {_days(trend.days_before)} in the "
            f"same length of time before ({_short(trend.span.before_start)} to "
            f"{_short(trend.span.before_end)})."
        )
    if len(trend.long_breaks) >= LONG_BREAKS_NOTABLE:
        said.append(
            f"{len(trend.long_breaks)} leaves joined a weekend or holiday into a break of "
            f"{LONG_BREAK_DAYS} days or more."
        )
    for repeat in trend.repeats:
        said.append(
            f"Single-day {repeat.kind.lower()} on a {_weekday_name(repeat.weekday)} "
            f"{len(repeat.dates)} times: {', '.join(_dm(d) for d in repeat.dates)}."
        )
    return said


# --- the pattern, in one line -------------------------------------------------

#: A shape or a weekday holds the pattern from this share.
SHAPE_SHARE = 0.6
WEEKDAY_SHARE = 0.4
#: "often next to a weekend or holiday" from this share of requests.
BREAK_SHARE = 0.5


def pattern_line(trend: Trend) -> str:
    """The leave pattern in one line, from the counts by fixed rules — what the
    person did, never why. Each clause says only what a count shows."""
    if trend.days < MIN_DAYS_FOR_A_PATTERN:
        return "Too few leave days to show a pattern."
    requests = trend.single_requests + trend.longer_requests
    if requests and trend.single_requests / requests >= SHAPE_SHARE:
        shape = "Mostly single days"
    elif requests and trend.longer_requests / requests >= SHAPE_SHARE:
        shape = "Mostly longer leave"
    else:
        shape = "A mix of single days and longer leave"
    busiest = max(WEEKDAYS, key=lambda name: trend.by_weekday.get(name, 0.0))
    if _edge_notable(trend):
        when = "most often on a Monday or Friday"
    elif trend.by_weekday.get(busiest, 0.0) / trend.days >= WEEKDAY_SHARE:
        when = f"most often on a {_weekday_name(busiest)}"
    else:
        when = "spread across the week"
    beside = " and often next to a weekend or holiday" if (
        requests and trend.requests_in_breaks / requests >= BREAK_SHARE
    ) else ""
    change = ""
    if _change_notable(trend):
        way = "up" if trend.days > trend.days_before else "down"
        change = f"; {way} from {_days(trend.days_before)} before"
    return f"{shape}, {when}{beside}{change}."


def _edge_notable(trend: Trend) -> bool:
    return trend.edge_days >= EDGE_DAYS_MIN and trend.edge_days / trend.days >= EDGE_SHARE_MIN


def _change_notable(trend: Trend) -> bool:
    change = abs(trend.days - trend.days_before)
    return change >= CHANGE_DAYS_MIN and (
        trend.days_before == 0 or change / trend.days_before >= CHANGE_SHARE_MIN
    )


# --- the lines the assistant reads --------------------------------------------


def lines(trend: Trend, who: str, balance: str = "") -> list[str]:
    """The trend as short lines: the period first, the notable lines last. No
    request ids and no leave reasons — a trend needs neither, and a reason can
    be medical."""
    span = trend.span
    out = [
        f"{who} — leave from {_day(span.start)} to {_day(span.end)} ({span.label}).",
        f"Pattern: {pattern_line(trend)}",
    ]
    # Leave taken, its change and the long breaks are written even at none, so
    # the card the assistant copies them into keeps its tiles (EC-D274).
    total = sum(trend.requests.values())
    if total == 0:
        out.append("Requests: 0 — no leave requests in this period.")
    else:
        parts = [f"{trend.requests[s]} {s.lower()}" for s in STATUSES if trend.requests[s]]
        out.append(f"Requests: {total} — {' · '.join(parts)}.")
    out.append(f"Leave taken: {_days(trend.days)}, approved leave only.")
    out.append(f"Change: {_change(trend)}.")
    if trend.by_type:
        out.append("By type: " + " · ".join(
            f"{kind} {_days(days)} ({count} request{'' if count == 1 else 's'})"
            for kind, (days, count) in sorted(trend.by_type.items(), key=lambda i: -i[1][0])
        ) + ".")
    if trend.days:
        out.append("By month: " + " · ".join(_month_part(trend, m, d) for m, d in trend.by_month)
                   + ".")
        out.append("By month and type: " + " · ".join(
            _month_types(trend, m) for m, _ in trend.by_month
        ) + ".")
        quarters = _quarters(trend)
        if len(quarters) > 1:
            out.append("By quarter: " + " · ".join(quarters) + ".")
        out.append("Weekdays: " + " · ".join(
            f"{name} {trend.by_weekday[name]:g}" for name in WEEKDAYS
        ) + ".")
        out.append(
            f"Monday or Friday: {trend.edge_days:g}/{trend.days:g} days; about "
            f"{trend.days * EVEN_EDGE_SHARE:.1f} if spread evenly."
        )
    out.append(
        f"Before: {_days(trend.days_before)} in {trend.requests_before} "
        f"request{'' if trend.requests_before == 1 else 's'} from {_day(span.before_start)} "
        f"to {_day(span.before_end)}."
    )
    if trend.long_breaks:
        out.append(f"Long breaks: {len(trend.long_breaks)} — "
                   + " · ".join(_break(b) for b in trend.long_breaks) + ".")
    else:
        out.append("Long breaks: 0.")
    if trend.upcoming:
        shown = trend.upcoming[:UPCOMING_LIMIT]
        more = len(trend.upcoming) - len(shown)
        out.append("Upcoming, not counted: " + " · ".join(
            f"{leave.kind} {_when(leave)}, {leave.status.lower()}" for leave in shown
        ) + (f" · and {more} more" if more > 0 else "") + ".")
    if balance:
        out.append(balance)
    said = notable(trend)
    if said:
        out.append("Notable:")
        out.extend(f"- {line}" for line in said)
    return out


def _days(n: float) -> str:
    return f"{n:g} day{'' if n == 1 else 's'}"


def _day(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b %Y}"


def _short(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def _dm(day: date) -> str:
    return f"{day.day} {day:%b}"


def _weekday_name(short: str) -> str:
    return {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
            "Thu": "Thursday", "Fri": "Friday"}[short]


def _when(leave: Leave) -> str:
    if leave.start == leave.end:
        return _day(leave.start)
    return f"{_day(leave.start)} to {_day(leave.end)}"


def _so_far(trend: Trend, last_day: date) -> str:
    """" so far" when the stretch is cut short by today."""
    return " so far" if trend.span.end == trend.today and trend.today < last_day else ""


def _month_part(trend: Trend, month: date, days: float) -> str:
    several_years = trend.span.start.year != trend.span.end.year
    name = f"{month:%b %Y}" if several_years else f"{month:%b}"
    last = add_months(month, 1) - timedelta(days=1)
    return f"{name} {days:g}{_so_far(trend, last)}"


def _month_types(trend: Trend, month: date) -> str:
    """A month and its days by leave type, for the model's month table: "Jun
    Sick 1, Casual 0.5" — or "none"."""
    name = f"{month:%b %Y}" if trend.span.start.year != trend.span.end.year else f"{month:%b}"
    if _so_far(trend, add_months(month, 1) - timedelta(days=1)):
        name += " (so far)"
    kinds = sorted(
        ((kind, days) for (m, kind), days in trend.by_month_type.items() if m == month and days),
        key=lambda item: -item[1],
    )
    said = ", ".join(f"{short_kind(kind)} {days:g}" for kind, days in kinds) or "none"
    return f"{name} {said}"


def short_kind(kind: str) -> str:
    """"Casual leave" → "Casual": short enough for a card's column or note."""
    words = kind.split()
    return " ".join(words[:-1]) if len(words) > 1 and words[-1].lower() == "leave" else kind


def _change(trend: Trend) -> str:
    """Days taken against the same length of time before, as the card's note says it."""
    before = trend.days_before
    if trend.days == before:
        return "the same as the time before"
    return f"{'up' if trend.days > before else 'down'} from {_days(before)} before"


def _quarters(trend: Trend) -> list[str]:
    parts = []
    quarter = quarter_start(trend.span.start)
    while quarter <= trend.span.end:
        last = add_months(quarter, 3) - timedelta(days=1)
        days = sum(d for m, d in trend.by_month if quarter <= m <= last)
        name = f"{quarter:%b}–{last:%b}"
        if trend.span.start.year != trend.span.end.year:
            name += f" {quarter:%Y}"
        cut = f" from {_dm(trend.span.start)}" if quarter < trend.span.start else ""
        parts.append(f"{name} {days:g}{cut}{_so_far(trend, last)}")
        quarter = add_months(quarter, 3)
    return parts


def _break(found: LongBreak) -> str:
    leave = _dm(found.leave_from) if found.leave_from == found.leave_to else (
        f"{_dm(found.leave_from)}–{_dm(found.leave_to)}"
    )
    bridge = ", a bridge day" if found.bridge else ""
    return (
        f"{leave}{bridge} ({found.off_from:%a} {_dm(found.off_from)}–{found.off_to:%a} "
        f"{_dm(found.off_to)}, {found.length} days off)"
    )
