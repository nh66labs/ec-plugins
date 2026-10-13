"""The counting behind get_leave_trend, on fixed dates.

Every figure an HRMS admin is told about a person comes from here, so each rule
is pinned: what counts as a day taken, which period a name means, what makes a
long break or a repeat, and the thresholds that make a line notable — each just
met and just missed.
"""

from __future__ import annotations

from datetime import date

import pytest

from hrms_mcp import trend
from hrms_mcp.trend import Leave

TODAY = date(2026, 10, 13)  # a Tuesday
HOLIDAYS = {date(2026, 10, 1)}  # a Thursday


def d(text: str) -> date:
    return date.fromisoformat(text)


def leave(start: str, end: str = "", kind: str = "Casual leave", status: str = "Approved",
          half: bool = False) -> Leave:
    return Leave(kind=kind, start=d(start), end=d(end or start), half=half, status=status)


def six_months(leaves: list[Leave], holidays: set[date] = HOLIDAYS) -> trend.Trend:
    span = trend.span_of("last 6 months", TODAY)
    return trend.summarise(leaves, holidays, span, TODAY)


# --- which dates a period means ---------------------------------------------------


def test_the_last_six_months_end_today_and_compare_with_the_six_before() -> None:
    span = trend.span_of("last 6 months", TODAY)
    assert (span.start, span.end) == (d("2026-04-13"), TODAY)
    assert (span.before_start, span.before_end) == (d("2025-10-13"), d("2026-04-12"))
    assert span.label == "the last 6 months"


def test_a_month_back_from_the_31st_is_the_months_last_day() -> None:
    assert trend.add_months(d("2026-03-31"), -1) == d("2026-02-28")
    assert trend.add_months(d("2026-01-31"), 1) == d("2026-02-28")
    assert trend.add_months(d("2026-01-15"), -1) == d("2025-12-15")


def test_this_quarter_is_compared_with_the_same_days_of_the_last() -> None:
    span = trend.span_of("this quarter", TODAY)
    assert (span.start, span.end) == (d("2026-10-01"), TODAY)
    assert (span.before_start, span.before_end) == (d("2026-07-01"), d("2026-07-13"))


def test_last_quarter_across_a_year_boundary() -> None:
    span = trend.span_of("last quarter", d("2027-01-15"))
    assert (span.start, span.end) == (d("2026-10-01"), d("2026-12-31"))
    assert (span.before_start, span.before_end) == (d("2026-07-01"), d("2026-09-30"))


def test_this_year_and_last_year() -> None:
    this = trend.span_of("this year", TODAY)
    assert (this.start, this.end) == (d("2026-01-01"), TODAY)
    assert (this.before_start, this.before_end) == (d("2025-01-01"), d("2025-10-13"))
    last = trend.span_of("last year", TODAY)
    assert (last.start, last.end) == (d("2025-01-01"), d("2025-12-31"))
    assert (last.before_start, last.before_end) == (d("2024-01-01"), d("2024-12-31"))


def test_between_dates_compares_with_as_many_days_before() -> None:
    span = trend.span_of("between dates", TODAY, "2026-09-01", "2026-09-30")
    assert (span.start, span.end) == (d("2026-09-01"), d("2026-09-30"))
    assert (span.before_start, span.before_end) == (d("2026-08-02"), d("2026-08-31"))


def test_between_dates_stops_at_today() -> None:
    span = trend.span_of("between dates", TODAY, "2026-09-01", "2026-12-31")
    assert span.end == TODAY


@pytest.mark.parametrize(
    ("period", "date_from", "date_to", "said"),
    [
        ("between dates", "2024-01-01", "2026-10-01", "at most 24 months"),
        ("between dates", "2026-10-01", "2026-09-01", "ends before it starts"),
        ("between dates", "1 Oct", "", "YYYY-MM-DD"),
        ("last 2 weeks", "", "", "must be one of"),
    ],
)
def test_a_period_that_cannot_be_counted_is_refused(
    period: str, date_from: str, date_to: str, said: str
) -> None:
    with pytest.raises(ValueError, match=said):
        trend.span_of(period, TODAY, date_from, date_to)


def test_exactly_24_months_is_allowed() -> None:
    span = trend.span_of("between dates", TODAY, "2024-10-01", "2026-10-01")
    assert span.start == d("2024-10-01")


# --- what a day taken is --------------------------------------------------------


def test_weekends_and_holidays_are_not_days_taken() -> None:
    # Wed 30 Sep to Mon 5 Oct: Thu 1 Oct is a holiday, Sat and Sun are the weekend.
    taken = trend.days_of(leave("2026-09-30", "2026-10-05"), HOLIDAYS)
    assert taken == {d("2026-09-30"): 1, d("2026-10-02"): 1, d("2026-10-05"): 1}


def test_a_half_day_is_half_and_a_half_day_on_a_holiday_is_nothing() -> None:
    assert trend.days_of(leave("2026-10-02", half=True), HOLIDAYS) == {d("2026-10-02"): 0.5}
    assert trend.days_of(leave("2026-10-01", half=True), HOLIDAYS) == {}


def test_every_holiday_in_the_calendar_counts_optional_ones_too() -> None:
    days = trend.holidays_of([
        {"date": "2026-10-01", "type": "Public"},
        {"date": "2026-10-20", "type": "Optional"},
        {"date": "not a date"},
    ])
    assert days == {d("2026-10-01"), d("2026-10-20")}


def test_a_leave_across_the_periods_edge_counts_only_its_days_inside() -> None:
    # Fri 10 Apr to Tue 14 Apr; the period starts Mon 13 Apr.
    found = six_months([leave("2026-04-10", "2026-04-14")])
    assert found.days == 2
    assert found.days_before == 1


def test_only_approved_leave_is_taken_but_every_request_is_counted() -> None:
    found = six_months([
        leave("2026-06-03"),
        leave("2026-06-04", status="Pending"),
        leave("2026-06-05", status="Rejected"),
        leave("2026-06-08", status="Cancelled"),
    ])
    assert found.days == 1
    assert found.requests == {"Approved": 1, "Pending": 1, "Rejected": 1, "Cancelled": 1}


def test_days_by_type_month_and_weekday() -> None:
    found = six_months([
        leave("2026-05-22"),                                   # Fri
        leave("2026-06-08", kind="Sick leave"),                # Mon
        leave("2026-06-10", kind="Sick leave", half=True),     # Wed
    ])
    assert found.by_type == {"Casual leave": (1, 1), "Sick leave": (1.5, 2)}
    months = dict(found.by_month)
    assert months[d("2026-05-01")] == 1 and months[d("2026-06-01")] == 1.5
    assert found.by_weekday == {"Mon": 1, "Tue": 0, "Wed": 0.5, "Thu": 0, "Fri": 1}


def test_records_from_the_hrms_are_read_and_unreadable_ones_skipped() -> None:
    leaves = trend.leaves_of([
        {"leave_type": "CL", "leave_mode": "FIRST HALF", "leave_date_from": "2026-06-03",
         "leave_date_to": "2026-06-03", "status": "Approved"},
        {"leave_type": "Sick Leave", "leave_mode": "Full Day",
         "leave_date_from": "2026-06-08T00:00:00Z", "leave_date_to": "", "status": "Approved"},
        {"leave_type": "CL", "leave_date_from": "garbage", "status": "Approved"},
    ], kind_of=lambda code: {"CL": "Casual leave"}.get(code, code))
    assert leaves == [
        Leave("Casual leave", d("2026-06-03"), d("2026-06-03"), True, "Approved"),
        Leave("Sick Leave", d("2026-06-08"), d("2026-06-08"), False, "Approved"),
    ]


def test_upcoming_leave_is_listed_and_never_counted() -> None:
    found = six_months([
        leave("2026-10-26"),
        leave("2026-11-02", status="Pending"),
        leave("2026-11-09", status="Rejected"),
    ])
    assert found.days == 0
    assert [u.start for u in found.upcoming] == [d("2026-10-26"), d("2026-11-02")]


# --- long breaks, bridge days and repeats ----------------------------------------


def test_a_friday_off_makes_a_long_weekend() -> None:
    (found,) = six_months([leave("2026-05-22")]).long_breaks
    assert (found.off_from, found.off_to, found.length) == (d("2026-05-22"), d("2026-05-24"), 3)
    assert not found.bridge


def test_a_midweek_day_or_a_half_day_is_no_break() -> None:
    assert six_months([leave("2026-05-20")]).long_breaks == []
    assert six_months([leave("2026-05-22", half=True)]).long_breaks == []


def test_a_day_between_a_holiday_and_the_weekend_is_a_bridge_day() -> None:
    (found,) = six_months([leave("2026-10-02")]).long_breaks
    assert (found.off_from, found.off_to, found.length) == (d("2026-10-01"), d("2026-10-04"), 4)
    assert found.bridge


def test_a_holiday_on_one_side_only_is_no_bridge() -> None:
    # Thu 1 Oct off, Fri 2 Oct a holiday, Wed 30 Sep worked: a long weekend, but
    # nothing is bridged — the day before it was worked.
    (found,) = six_months([leave("2026-10-01")], holidays={d("2026-10-02")}).long_breaks
    assert (found.off_from, found.off_to, found.length) == (d("2026-10-01"), d("2026-10-04"), 4)
    assert not found.bridge


def test_a_week_away_is_one_break_with_both_weekends() -> None:
    (found,) = six_months([leave("2026-06-15", "2026-06-19")]).long_breaks
    assert (found.off_from, found.off_to, found.length) == (d("2026-06-13"), d("2026-06-21"), 9)


def test_the_same_type_on_the_same_weekday_three_times_is_a_repeat() -> None:
    mondays = ["2026-06-08", "2026-08-03", "2026-09-07"]
    found = six_months([leave(m, kind="Sick leave") for m in mondays])
    assert found.repeats == [trend.Repeat("Sick leave", "Mon", tuple(d(m) for m in mondays))]


def test_twice_or_a_week_away_is_no_repeat() -> None:
    found = six_months([
        leave("2026-06-08", kind="Sick leave"),
        leave("2026-08-03", kind="Sick leave"),
        leave("2026-09-07", "2026-09-11", kind="Sick leave"),
    ])
    assert found.repeats == []


# --- what is notable -------------------------------------------------------------


def test_too_few_days_says_so_and_nothing_else() -> None:
    found = six_months([leave("2026-05-22"), leave("2026-05-29"), leave("2026-06-05", half=True)])
    assert trend.notable(found) == ["Too few leave days in this period to show a pattern."]


def _fridays(n: int) -> list[Leave]:
    fridays = ["2026-05-08", "2026-05-22", "2026-06-05", "2026-06-19", "2026-07-03"]
    return [leave(f) for f in fridays[:n]]


def _wednesdays(n: int) -> list[Leave]:
    wednesdays = ["2026-05-13", "2026-05-27", "2026-06-10", "2026-06-24"]
    return [leave(w) for w in wednesdays[:n]]


def test_mondays_and_fridays_are_notable_from_four_and_sixty_percent() -> None:
    met = trend.notable(six_months(_fridays(4) + _wednesdays(2)))  # 4 of 6
    assert any("4 of 6 leave days were on a Monday or Friday" in line for line in met)
    assert any("about 2.4" in line for line in met)


def test_mondays_and_fridays_just_under_the_share_are_not() -> None:
    missed = trend.notable(six_months(_fridays(4) + _wednesdays(3)))  # 4 of 7
    assert not any("Monday or Friday" in line for line in missed)


def test_three_fridays_are_not_enough() -> None:
    missed = trend.notable(six_months(_fridays(3)))
    assert not any("Monday or Friday" in line for line in missed)


@pytest.mark.parametrize(
    ("now", "before", "said"),
    [(8, 5, True), (8, 6, False), (10, 7, False), (3, 0, True), (3, 6, True)],
)
def test_a_change_needs_three_days_and_half_again(now: int, before: int, said: bool) -> None:
    wednesdays = [f"2026-{m:02d}-{day:02d}" for m, day in
                  [(5, 13), (5, 20), (5, 27), (6, 3), (6, 10), (6, 17), (6, 24), (7, 1),
                   (7, 8), (7, 15)]]
    earlier = [f"2025-{m:02d}-{day:02d}" for m, day in
               [(11, 5), (11, 12), (11, 19), (11, 26), (12, 3), (12, 10), (12, 17)]]
    found = six_months([leave(w) for w in wednesdays[:now]] +
                       [leave(e) for e in earlier[:before]])
    assert any("in the same length of time before" in line
               for line in trend.notable(found)) is said


def test_three_long_breaks_are_notable_and_two_are_not() -> None:
    three = trend.notable(six_months(_fridays(3)))
    assert any("3 leaves joined a weekend or holiday" in line for line in three)
    two = trend.notable(six_months(_fridays(2) + _wednesdays(1)))
    assert not any("joined a weekend" in line for line in two)


# --- what the assistant reads ------------------------------------------------------


def test_the_lines_state_the_period_first_and_the_notable_last() -> None:
    found = six_months([
        leave("2026-05-22"),
        leave("2026-06-08", kind="Sick leave"),
        leave("2026-08-03", kind="Sick leave"),
        leave("2026-09-07", kind="Sick leave"),
        leave("2026-09-14", "2026-09-15"),
        leave("2026-10-02", kind="Floating leave"),
        leave("2026-07-08", status="Rejected"),
        leave("2026-10-26"),
        leave("2025-11-05"),
    ])
    left = "Leave left now: 4/12 days — Casual 4/12."
    out = trend.lines(found, "Anu Thomas (H032)", balance=left)
    assert out[0] == ("Anu Thomas (H032) — leave from Mon 13 Apr 2026 to Tue 13 Oct 2026 "
                      "(the last 6 months).")
    assert "Requests: 7 — 6 approved · 1 rejected." in out
    assert "Leave taken: 7 days, approved leave only." in out
    assert ("By type: Casual leave 3 days (2 requests) · Sick leave 3 days (3 requests) · "
            "Floating leave 1 day (1 request).") in out
    assert "By month: Apr 0 · May 1 · Jun 1 · Jul 0 · Aug 1 · Sep 3 · Oct 1 so far." in out
    assert "By quarter: Apr–Jun 2 from 13 Apr · Jul–Sep 4 · Oct–Dec 1 so far." in out
    assert "Weekdays: Mon 4 · Tue 1 · Wed 0 · Thu 0 · Fri 2." in out
    assert ("Before: 1 day in 1 request from Mon 13 Oct 2025 to Sun 12 Apr 2026.") in out
    assert any(line.startswith("Long breaks: 6 — 22 May (Fri 22 May–Sun 24 May, 3 days off)")
               and "2 Oct, a bridge day (Thu 1 Oct–Sun 4 Oct, 4 days off)" in line
               for line in out)
    assert "Upcoming, not counted: Casual leave Mon 26 Oct 2026, approved." in out
    assert out.index(left) < out.index("Notable:")
    notable = out[out.index("Notable:") + 1:]
    assert "- Single-day sick leave on a Monday 3 times: 8 Jun, 3 Aug, 7 Sep." in notable
    assert "- 6 of 7 leave days were on a Monday or Friday; an even spread would be about 2.8." \
        in notable


def test_a_period_with_no_leave_says_so() -> None:
    out = trend.lines(six_months([]), "Anu Thomas (H032)")
    assert "Requests: 0 — no leave requests in this period." in out
    # Written at none, so the card keeps its tiles (EC-D274).
    assert "Leave taken: 0 days, approved leave only." in out
    assert "Change: the same as the time before." in out
    assert "Long breaks: 0." in out
    assert not any(line.startswith(("By month", "Weekdays")) for line in out)
    assert out[-1] == "- Too few leave days in this period to show a pattern."
