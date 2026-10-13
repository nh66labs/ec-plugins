"""A leave trend laid out as two cards, for the platform to show as they are.

Enterprise Claw draws what a read returns in ``_meta["ec/blocks"]`` as cards in
the answer (EC-D271), each an ``AnswerBlock``: here a **figures card** — leave
taken, requests, Monday-or-Friday days, long breaks, the balance left — and a
**month card**, a row a month and a column a leave type. The figures are the ones
``trend`` counted; the model never copies them, so they are exact and laid out
the same every time.

Pure, like ``trend``: no I/O, so each card is tested on fixed dates.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hrms_mcp import trend
from hrms_mcp.trend import Trend

#: Where the platform reads a result's cards (EC-D271).
BLOCKS_META = "ec/blocks"

#: A table holds at most 8 columns: a month, the leave types and a total.
TYPE_COLUMNS = 6
#: How many leave types' balance the figures card names; a metrics card holds 12.
BALANCES_SHOWN = 4

#: A balance as the HRMS gives it: the leave type, days available, the quota.
Balance = tuple[str, float, float]


def cards(found: Trend, who: str, balances: list[Balance] | None = None) -> list[dict[str, Any]]:
    """The figures card, and the month card when there is a month to compare."""
    laid_out = [figures(found, who, balances or [])]
    months = month_table(found)
    if months is not None:
        laid_out.append(months)
    return laid_out


def figures(found: Trend, who: str, balances: list[Balance]) -> dict[str, Any]:
    span = found.span
    fields: list[dict[str, Any]] = [
        {"label": "Leave taken", "value": _days(found.days), "note": _against_before(found)},
        {
            "label": "Requests",
            "value": sum(found.requests.values()),
            "format": "number",
            "note": " · ".join(
                f"{found.requests[s]} {s.lower()}" for s in trend.STATUSES if found.requests[s]
            ),
        },
    ]
    if found.days:
        fields.append({
            "label": "Monday or Friday",
            "value": f"{found.edge_days:g} of {_days(found.days)}",
            "note": f"about {found.days * trend.EVEN_EDGE_SHARE:.1f} if spread evenly",
        })
    fields.append({
        "label": "Long breaks",
        "value": len(found.long_breaks),
        "format": "number",
        "note": f"{trend.LONG_BREAK_DAYS}+ days off around a weekend or holiday",
    })
    fields += [
        {"label": f"{kind} left", "value": f"{available:g} of {quota:g}"}
        for kind, available, quota in balances[:BALANCES_SHOWN]
    ]
    return {
        "type": "metrics",
        "title": f"Leave trend · {who}",
        "subtitle": f"{_period(span.start, span.end)} · {trend.pattern_line(found)}",
        "fields": fields,
    }


def month_table(found: Trend) -> dict[str, Any] | None:
    """A row a month, a column a leave type taken, a total column and row — or
    None when there is no leave or a single month, which a sentence says as
    well (the platform refuses a table of one row)."""
    if not found.days or len(found.by_month) < 2:
        return None
    kinds = [k for k, (days, _) in sorted(found.by_type.items(), key=lambda i: -i[1][0]) if days]
    shown, folded = kinds[:TYPE_COLUMNS], kinds[TYPE_COLUMNS:]
    if folded:
        shown = kinds[: TYPE_COLUMNS - 1]
        folded = kinds[TYPE_COLUMNS - 1 :]
    columns = [{"label": "Month"}]
    columns += [{"label": _short_kind(k), "format": "number"} for k in shown]
    if folded:
        columns.append({"label": "Other", "format": "number"})
    columns.append({"label": "Total", "format": "number"})

    def by(month: date, names: list[str]) -> float:
        return sum(found.by_month_type.get((month, k), 0.0) for k in names)

    rows = []
    for month, total in found.by_month:
        cells: list[Any] = [_month(found, month)]
        cells += [_cell(by(month, [k])) for k in shown]
        if folded:
            cells.append(_cell(by(month, folded)))
        cells.append(_cell(total))
        rows.append({"cells": cells})
    totals: list[Any] = ["Total"]
    totals += [_cell(found.by_type[k][0]) for k in shown]
    if folded:
        totals.append(_cell(sum(found.by_type[k][0] for k in folded)))
    totals.append(_cell(found.days))
    rows.append({"cells": totals})
    return {"type": "table", "title": "By month", "columns": columns, "rows": rows}


def _against_before(found: Trend) -> str:
    before = found.days_before
    if found.days == before:
        return "the same as the time before"
    way = "up" if found.days > before else "down"
    return f"{way} from {before:g} before"


def _days(n: float) -> str:
    return f"{n:g} day{'' if n == 1 else 's'}"


def _cell(n: float) -> float | int | str:
    """A count as the table shows it: a dash for none, whole days as integers."""
    if not n:
        return "–"
    return int(n) if float(n).is_integer() else n


def _short_kind(kind: str) -> str:
    """"Casual leave" → "Casual": the column says what the card's title says."""
    words = kind.split()
    return " ".join(words[:-1]) if len(words) > 1 and words[-1].lower() == "leave" else kind


def _month(found: Trend, month: date) -> str:
    several_years = found.span.start.year != found.span.end.year
    name = f"{month:%b %Y}" if several_years else f"{month:%b}"
    last = trend.add_months(month, 1) - timedelta(days=1)
    cut_short = found.span.end == found.today and found.today < last
    return f"{name} (so far)" if cut_short else name


def _period(start: date, end: date) -> str:
    first = f"{start.day} {start:%b}" if start.year == end.year else f"{start.day} {start:%b %Y}"
    return f"{first} – {end.day} {end:%b %Y}"
