"""The leave trend's pattern line and the figures its card is laid out from.

The assistant lays the trend out as a card with ``present`` (EC-D274), copying
the figures the result writes — so each is pinned here as the result writes it:
every clause of the pattern line from the counts it rests on, the change, the
Monday-or-Friday share, the long breaks' count and the days a month by type.
"""

from __future__ import annotations

from datetime import date

from hrms_mcp import trend
from hrms_mcp.trend import Leave

TODAY = date(2026, 10, 13)  # a Tuesday
HOLIDAYS = {date(2026, 10, 1)}  # a Thursday


def leave(start: str, end: str = "", kind: str = "Casual leave", status: str = "Approved",
          half: bool = False) -> Leave:
    return Leave(kind, date.fromisoformat(start), date.fromisoformat(end or start), half, status)


def six_months(leaves: list[Leave], period: str = "last 6 months") -> trend.Trend:
    return trend.summarise(leaves, HOLIDAYS, trend.span_of(period, TODAY), TODAY)


#: The live test's history: single days, Mondays, long weekends, more than before.
HISTORY = [
    leave("2026-05-22"),
    leave("2026-06-08", kind="Sick leave"),
    leave("2026-06-10", kind="Sick leave", half=True),
    leave("2026-08-03", kind="Sick leave"),
    leave("2026-09-07", kind="Sick leave"),
    leave("2026-09-14", "2026-09-15"),
    leave("2026-10-02", kind="Floating leave"),
    leave("2026-07-10", status="Cancelled"),
    leave("2025-11-05"),
]


# --- the pattern, in one line --------------------------------------------------


def test_the_pattern_says_shape_weekday_breaks_and_change() -> None:
    assert trend.pattern_line(six_months(HISTORY)) == (
        "Mostly single days, most often on a Monday or Friday and often next to a weekend "
        "or holiday; up from 1 day before."
    )


def test_too_few_days_is_all_the_pattern_says() -> None:
    found = six_months([leave("2026-05-20"), leave("2026-05-27")])
    assert trend.pattern_line(found) == "Too few leave days to show a pattern."


def test_longer_leave_spread_across_the_week() -> None:
    found = six_months([
        leave("2026-05-12", "2026-05-14"),  # Tue–Thu
        leave("2026-06-16", "2026-06-18"),  # Tue–Thu
        leave("2026-07-21", "2026-07-22"),  # Tue–Wed
    ])
    assert trend.pattern_line(found) == (
        "Mostly longer leave, spread across the week; up from 0 days before."
    )


def test_one_weekday_holding_the_days_is_named() -> None:
    found = six_months([leave(d) for d in ("2026-05-13", "2026-05-20", "2026-06-03")] +
                       [leave("2026-06-09"), leave("2025-11-05"), leave("2025-11-12")])
    assert trend.pattern_line(found) == "Mostly single days, most often on a Wednesday."


def test_a_mix_and_no_notable_change() -> None:
    found = six_months([
        leave("2026-05-13"), leave("2026-06-10"),
        leave("2026-07-14", "2026-07-16"), leave("2026-08-11", "2026-08-13"),
        *(leave(d) for d in ("2025-11-04", "2025-11-11", "2025-12-02", "2026-01-13",
                             "2026-02-10", "2026-03-10", "2026-03-17")),
    ])
    assert trend.pattern_line(found).startswith("A mix of single days and longer leave, ")
    assert "before" not in trend.pattern_line(found)


def test_the_assistant_reads_the_pattern_too() -> None:
    out = trend.lines(six_months(HISTORY), "Navaneeth K (EMP001)")
    assert out[1] == "Pattern: " + trend.pattern_line(six_months(HISTORY))


# --- the figures the card copies ----------------------------------------------------


def test_the_result_writes_each_figure_as_the_card_shows_it() -> None:
    out = trend.lines(six_months(HISTORY), "Navaneeth K (EMP001)")
    assert "Change: up from 1 day before." in out
    assert "Monday or Friday: 6/7.5 days; about 3.0 if spread evenly." in out
    assert any(line.startswith("Long breaks: 6 — 22 May") for line in out)
    assert (
        "By month and type: Apr none · May Casual 1 · Jun Sick 1.5 · Jul none · "
        "Aug Sick 1 · Sep Casual 2, Sick 1 · Oct (so far) Floating 1."
    ) in out


def test_the_same_or_fewer_days_than_before_is_said_so() -> None:
    same = trend.lines(six_months([leave("2026-05-20"), leave("2025-11-05")]), "A")
    assert "Change: the same as the time before." in same
    fewer = trend.lines(
        six_months([leave("2026-05-20"), leave("2025-11-05"), leave("2025-11-06")]), "A"
    )
    assert "Change: down from 2 days before." in fewer


def test_months_across_two_years_carry_the_year() -> None:
    out = trend.lines(six_months(HISTORY, "last 12 months"), "A")
    line = next(line for line in out if line.startswith("By month and type: "))
    assert line.startswith("By month and type: Oct 2025 none · Nov 2025 Casual 1 · ")
    assert line.endswith("Oct 2026 (so far) Floating 1.")
