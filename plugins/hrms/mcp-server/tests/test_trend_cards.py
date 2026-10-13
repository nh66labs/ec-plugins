"""The leave trend's cards and its pattern line, on fixed dates.

What an HRMS administrator reads first is the figures card and its one-line
pattern, so each is pinned: every clause of the pattern line from the counts it
rests on, every figure the card shows, and when the month card is left out.
"""

from __future__ import annotations

from datetime import date

from hrms_mcp import trend, trend_cards
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


# --- the figures card ---------------------------------------------------------------


def test_the_figures_card_names_the_person_period_and_pattern() -> None:
    card = trend_cards.figures(
        six_months(HISTORY), "Navaneeth K (EMP001)",
        [("Casual leave", 3, 12), ("Sick leave", 7.5, 12)],
    )
    assert card["type"] == "metrics"
    assert card["title"] == "Leave trend · Navaneeth K (EMP001)"
    assert card["subtitle"] == (
        "13 Apr – 13 Oct 2026 · " + trend.pattern_line(six_months(HISTORY))
    )
    assert card["fields"] == [
        {"label": "Leave taken", "value": "7.5 days", "note": "up from 1 before"},
        {"label": "Requests", "value": 8, "format": "number",
         "note": "7 approved · 1 cancelled"},
        {"label": "Monday or Friday", "value": "6 of 7.5 days",
         "note": "about 3.0 if spread evenly"},
        {"label": "Long breaks", "value": 6, "format": "number",
         "note": "3+ days off around a weekend or holiday"},
        {"label": "Casual leave left", "value": "3 of 12"},
        {"label": "Sick leave left", "value": "7.5 of 12"},
    ]


def test_with_no_leave_the_card_says_none_and_no_month_card_follows() -> None:
    found = six_months([])
    laid_out = trend_cards.cards(found, "Anu (H032)")
    assert len(laid_out) == 1
    fields = {f["label"]: f for f in laid_out[0]["fields"]}
    assert fields["Leave taken"] == {
        "label": "Leave taken", "value": "0 days", "note": "the same as the time before",
    }
    assert "Monday or Friday" not in fields
    assert laid_out[0]["subtitle"].endswith("Too few leave days to show a pattern.")


def test_a_period_across_two_years_names_both() -> None:
    card = trend_cards.figures(six_months(HISTORY, "last 12 months"), "A", [])
    assert card["subtitle"].startswith("13 Oct 2025 – 13 Oct 2026 · ")


def test_only_four_balances_are_named() -> None:
    balances = [(f"Kind {n}", 1, 2) for n in range(6)]
    card = trend_cards.figures(six_months(HISTORY), "A", balances)
    assert [f["label"] for f in card["fields"]][-4:] == [f"Kind {n} left" for n in range(4)]


# --- the month card -----------------------------------------------------------------


def test_the_month_card_has_a_row_a_month_and_a_total() -> None:
    table = trend_cards.month_table(six_months(HISTORY))
    assert table is not None
    labels = [c["label"] for c in table["columns"]]
    assert labels == ["Month", "Sick", "Casual", "Floating", "Total"]
    assert [r["cells"] for r in table["rows"]] == [
        ["Apr", "–", "–", "–", "–"],
        ["May", "–", 1, "–", 1],
        ["Jun", 1.5, "–", "–", 1.5],
        ["Jul", "–", "–", "–", "–"],
        ["Aug", 1, "–", "–", 1],
        ["Sep", 1, 2, "–", 3],
        ["Oct (so far)", "–", "–", 1, 1],
        ["Total", 3.5, 3, 1, 7.5],
    ]


def test_one_month_or_no_leave_leaves_the_month_card_out() -> None:
    assert trend_cards.month_table(six_months([])) is None
    this_quarter = trend.summarise(
        [leave("2026-10-05")], HOLIDAYS, trend.span_of("this quarter", TODAY), TODAY
    )
    assert trend_cards.month_table(this_quarter) is None


def test_more_types_than_columns_are_folded_into_other() -> None:
    # Eight leave types, a working day each: Mon 1 Jun to Wed 10 Jun.
    days = ["01", "02", "03", "04", "05", "08", "09", "10"]
    found = six_months([leave(f"2026-06-{d}", kind=f"Kind{n} leave") for n, d in enumerate(days)])
    table = trend_cards.month_table(found)
    assert table is not None
    labels = [c["label"] for c in table["columns"]]
    assert labels == ["Month", "Kind0", "Kind1", "Kind2", "Kind3", "Kind4", "Other", "Total"]
    assert table["rows"][-1]["cells"][-2:] == [3, 8]


def test_months_across_two_years_carry_the_year() -> None:
    table = trend_cards.month_table(six_months(HISTORY, "last 12 months"))
    assert table is not None
    assert table["rows"][0]["cells"][0] == "Oct 2025"
    assert table["rows"][-2]["cells"][0] == "Oct 2026 (so far)"
