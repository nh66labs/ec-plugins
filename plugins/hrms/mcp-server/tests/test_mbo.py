"""The MBO tools, against the same fake HRMS as the leave tools.

What matters most: the context is gathered from the HRMS as the person asking
and read as lines; a save reaches the HRMS only as a complete set whose
weightages total 100, in the one field the HRMS takes; and every schema is one
Enterprise Claw binds and shows as readable lines on a confirm card.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hrms_mcp import mbo
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

PLAN = {
    "fiscal_year": "2026-27", "quarter": 3, "status": "Draft", "mbo_access_enabled": True,
    "can_edit": True, "mentor": "Mira", "total_weightage": 100,
    "mbos": [
        {"title": "Ship v2", "kpi": "Released by 15 Nov", "weightage": 60,
         "completion_percent": 0, "manager_rating": 0},
        {"title": "Cut P1 bugs", "kpi": "Below 5 open", "weightage": 40,
         "completion_percent": 0, "manager_rating": 0},
    ],
}
PREVIOUS = {
    "fiscal_year": "2026-27", "quarter": 2, "status": "Evaluated", "can_edit": False,
    "why_not_editable": "the Q2 2026-27 plan is Evaluated; only a Draft plan can be changed",
    "mbos": [{"title": "Learn the billing module", "kpi": "Own 3 tickets", "weightage": 100,
              "completion_percent": 90, "manager_rating": 80}],
}


#: What Enterprise Claw keeps of a server's instructions (its
#: ``INSTRUCTIONS_LIMIT``, EC-D180); the rest is cut without a word.
PLATFORM_INSTRUCTIONS_LIMIT = 4_000


@pytest.fixture()
def mbo_hrms(hrms: FakeHrms) -> FakeHrms:  # noqa: F811
    hrms.results.update({
        "get_my_profile": {
            "name": "Ravi", "employee_code": "EMP-042", "designation": "Software Engineer",
            "date_of_joining": "2024-01-15", "mentor": "Mira", "skills": ["Go", "React"],
            "previous_experiences": [{"company": "Acme", "role": "Intern"}],
        },
        "get_my_projects": [
            {"id": "p-1", "name": "Phoenix", "description": "Billing platform rewrite",
             "project_managers": [{"id": "pm-1", "name": "Priya"}]},
        ],
        "get_timesheet": [
            {"date": "2026-09-20", "project_name": "Phoenix", "hours": 6, "note": "invoice API"},
            {"date": "2026-09-21", "project_name": "Phoenix", "hours": 7, "note": "invoice API"},
            {"date": "2026-09-22", "activity_type_name": "Training", "hours": 2, "note": ""},
        ],
        "save_my_mbo_plan": PLAN,
    })
    return hrms


NEXT = {
    "fiscal_year": "2026-27", "quarter": 4, "status": "Not started", "can_edit": False,
    "why_not_editable": "MBO setting for Q4 2026-27 has not been opened — HR enables it",
    "mbos": [],
}


def _plans(fake: FakeHrms, current: dict, previous: dict, upcoming: dict = NEXT) -> None:
    """get_my_mbo_plan answers the current plan with no period given, else the
    quarter before or after it."""
    def answer(arguments: dict) -> dict:
        if not arguments.get("quarter"):
            return current
        return previous if arguments["quarter"] < current["quarter"] else upcoming

    fake.results["get_my_mbo_plan"] = answer


# --- what Enterprise Claw sees -----------------------------------------------------


def test_the_mbo_tools_are_listed_with_their_kind(client) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools["get_my_mbo_context"]["annotations"]["readOnlyHint"] is True
    assert tools["get_my_mbo_plan"]["annotations"]["readOnlyHint"] is True
    save = tools["save_my_mbo_plan"]
    assert save["annotations"]["readOnlyHint"] is False
    assert save["annotations"]["idempotentHint"] is True
    # One field per value, so a confirm card reads "Objective 1: …", not a record.
    properties = save["inputSchema"]["properties"]
    wanted = {"objective_1", "kpi_1", "weight_1", "objective_5", "kpi_5", "weight_5"}
    assert wanted <= set(properties)
    assert all(node["type"] in {"string", "number", "integer"} for node in properties.values())
    assert set(save["inputSchema"]["required"]) == {
        "fiscal_year", "quarter", "objective_1", "kpi_1", "weight_1",
    }


def test_the_handshake_carries_the_mbo_coaching(client) -> None:  # noqa: F811
    result = rpc(
        client, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "enterprise-claw", "version": "2"}},
    ).json()["result"]
    # What the platform keeps of it, not what is sent: the rules past its limit
    # never reach the model, which is how "never save" was once lost unnoticed.
    said = result["instructions"][:PLATFORM_INSTRUCTIONS_LIMIT]
    assert "get_my_mbo_context first" in said
    assert "exactly three MBOs" in said
    assert "genuinely different" in said
    assert "totalling exactly\n   100" in said
    assert "Never save before they have agreed" in said
    assert "Only ever discuss the person's own MBOs" in said



def test_the_instructions_fit_what_the_platform_keeps(client) -> None:  # noqa: F811
    result = rpc(
        client, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "enterprise-claw", "version": "2"}},
    ).json()["result"]
    assert len(result["instructions"]) <= PLATFORM_INSTRUCTIONS_LIMIT


# --- the context ------------------------------------------------------------------


def test_the_context_gathers_what_the_hrms_knows_as_lines(client, mbo_hrms) -> None:  # noqa: F811
    _plans(mbo_hrms, PLAN, PREVIOUS)
    said = text(call(client, "get_my_mbo_context"))

    assert "Designation: Software Engineer" in said and "Mentor: Mira" in said
    assert "This quarter's plan (Q3 2026-27, 16 Sep 2026 to 15 Dec 2026): Draft." in said
    assert "It is open" in said
    assert "Last quarter's plan (Q2 2026-27, 16 Jun 2026 to 15 Sep 2026): Evaluated." in said
    assert "Learn the billing module" in said and "manager rating 80" in said
    assert "Phoenix: Billing platform rewrite (managed by Priya)" in said
    assert "Phoenix: 13 hours" in said and "Training: 2 hours" in said
    assert "invoice API" in said
    assert "Next quarter's plan (Q4 2026-27, 16 Dec 2026 to 15 Mar 2027): Not started." in said


def test_the_context_ends_with_how_to_present_suggestions(client, mbo_hrms) -> None:  # noqa: F811
    _plans(mbo_hrms, PLAN, PREVIOUS)
    said = text(call(client, "get_my_mbo_context"))
    guide = said[said.index("How to present suggestions"):]

    # KPI dates are this quarter's, the open one.
    assert "between 16 Sep 2026 and 15 Dec 2026" in guide
    assert "three numbered MBOs" in guide and "total exactly 100" in guide
    assert "No tables" in guide and "no list of sources" in guide
    assert "Mention no other Slack activity" in guide
    assert guide.rstrip().endswith("save them as a Draft. Do not save yet.")


def test_the_guide_dates_kpis_in_next_quarter_when_only_it_is_open(
    client, mbo_hrms,  # noqa: F811
) -> None:
    closed = {**PLAN, "status": "Submitted", "can_edit": False}
    _plans(mbo_hrms, closed, PREVIOUS, {**NEXT, "can_edit": True})
    said = text(call(client, "get_my_mbo_context"))
    assert "between 16 Dec 2026 and 15 Mar 2027" in said


def test_the_guide_names_no_dates_when_no_plan_is_open(client, mbo_hrms) -> None:  # noqa: F811
    _plans(mbo_hrms, {**PLAN, "can_edit": False}, PREVIOUS)
    said = text(call(client, "get_my_mbo_context"))
    assert "*KPI* — a number or a date\n" in said


def test_the_context_asks_for_the_quarter_before_and_the_last_three_months(
    client, mbo_hrms,  # noqa: F811
) -> None:
    _plans(mbo_hrms, PLAN, PREVIOUS)
    call(client, "get_my_mbo_context")
    plans = [c["arguments"] for c in mbo_hrms.calls() if c["name"] == "get_my_mbo_plan"]
    assert plans == [{}, {"fiscal_year": "2026-27", "quarter": 2},
                     {"fiscal_year": "2026-27", "quarter": 4}]
    calls = {c["name"]: c["arguments"] for c in mbo_hrms.calls()}
    # The session's date (29 Sep 2026) less 90 days.
    assert calls["get_timesheet"] == {"start_date": "2026-07-01", "end_date": "2026-09-29"}


def test_every_call_is_made_as_the_person_and_names_no_one(client, mbo_hrms) -> None:  # noqa: F811
    _plans(mbo_hrms, PLAN, PREVIOUS)
    call(client, "get_my_mbo_context")
    for sent in mbo_hrms.calls():
        assert sent["_identity"]["user_id"] == "emp-0042"
        assert not {"employee_id", "caller_id", "approver_id"} & set(sent["arguments"])


def test_a_quarter_not_yet_opened_says_why(client, mbo_hrms) -> None:  # noqa: F811
    closed = {"fiscal_year": "2026-27", "quarter": 3, "status": "Not started", "can_edit": False,
              "why_not_editable": "MBO setting for Q3 2026-27 has not been opened — HR enables it",
              "mbos": []}
    _plans(mbo_hrms, closed, PREVIOUS)
    said = text(call(client, "get_my_mbo_context"))
    assert "Not started" in said and "HR enables it" in said and "No MBOs on it yet" in said


def test_no_identity_asks_the_hrms_nothing(client, mbo_hrms) -> None:  # noqa: F811
    result = call(client, "get_my_mbo_context", identity=None)
    assert result["isError"]
    assert mbo_hrms.calls() == []


# --- one plan ---------------------------------------------------------------------


def test_a_past_quarter_is_read_by_its_period(client, mbo_hrms) -> None:  # noqa: F811
    mbo_hrms.results["get_my_mbo_plan"] = PREVIOUS
    said = text(call(client, "get_my_mbo_plan", {"fiscal_year": "2026-27", "quarter": 2}))
    assert "Evaluated" in said and "self rating 90%" in said
    assert mbo_hrms.calls()[-1]["arguments"] == {"fiscal_year": "2026-27", "quarter": 2}


def test_the_current_quarter_sends_no_period(client, mbo_hrms) -> None:  # noqa: F811
    mbo_hrms.results["get_my_mbo_plan"] = PLAN
    call(client, "get_my_mbo_plan")
    assert mbo_hrms.calls()[-1]["arguments"] == {}


# --- the save ---------------------------------------------------------------------

AGREED = {
    "fiscal_year": "2026-27", "quarter": 3,
    "objective_1": "Ship v2", "kpi_1": "Released by 15 Nov", "weight_1": 60,
    "objective_2": "Cut P1 bugs", "kpi_2": "Below 5 open", "weight_2": 40,
}


def test_a_save_sends_the_agreed_set_as_the_hrms_takes_it(client, mbo_hrms) -> None:  # noqa: F811
    said = text(call(client, "save_my_mbo_plan", AGREED))
    assert "Saved 2 MBOs as a Draft." in said and "Submit it in the HRMS" in said
    sent = mbo_hrms.calls()[-1]
    assert sent["name"] == "save_my_mbo_plan"
    assert sent["arguments"]["fiscal_year"] == "2026-27" and sent["arguments"]["quarter"] == 3
    assert json.loads(sent["arguments"]["mbos"]) == [
        {"title": "Ship v2", "kpi": "Released by 15 Nov", "weightage": 60},
        {"title": "Cut P1 bugs", "kpi": "Below 5 open", "weightage": 40},
    ]


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"weight_2": 30}, "total 90"),
        ({"kpi_2": ""}, "objective and a KPI"),
        ({"weight_1": 0, "weight_2": 100}, "above 0"),
        ({"objective_3": "Mentor a junior", "kpi_3": "Weekly pairing", "weight_3": 0}, "above 0"),
        ({"objective_2": "", "kpi_2": "", "weight_2": 0, "objective_3": "x", "kpi_3": "y",
          "weight_3": 40}, "without gaps"),
        ({"quarter": 5}, "quarter must be 1-4"),
    ],
)
def test_an_incomplete_set_is_refused_before_the_hrms(
    client, mbo_hrms, change: dict, says: str,  # noqa: F811
) -> None:
    result = call(client, "save_my_mbo_plan", {**AGREED, **change})
    assert result["isError"]
    assert says in text(result)
    assert all(c["name"] != "save_my_mbo_plan" for c in mbo_hrms.calls())


def test_the_hrms_refusing_a_save_is_said_plainly(client, mbo_hrms) -> None:  # noqa: F811
    mbo_hrms.errors["save_my_mbo_plan"] = (
        "cannot save: the Q3 2026-27 plan is Submitted; only a Draft plan can be changed"
    )
    result = call(client, "save_my_mbo_plan", AGREED)
    assert result["isError"] and "only a Draft plan can be changed" in text(result)


def test_the_mbos_never_reach_the_log(client, mbo_hrms, caplog) -> None:  # noqa: F811
    caplog.set_level("DEBUG")
    call(client, "save_my_mbo_plan", AGREED)
    assert "Released by 15 Nov" not in caplog.text
    assert "emp-0042" not in caplog.text


# --- pure helpers -------------------------------------------------------------------


def test_the_quarter_before_crosses_the_fiscal_year() -> None:
    assert mbo.previous_quarter("2026-27", 3) == ("2026-27", 2)
    assert mbo.previous_quarter("2026-27", 1) == ("2025-26", 4)
    assert mbo.previous_quarter("2099-00", 1) == ("2098-99", 4)


def test_recent_work_keeps_distinct_notes_newest_first() -> None:
    entries = [
        {"date": f"2026-09-{d:02d}", "project_name": "Phoenix", "hours": 1, "note": f"note {d}"}
        for d in range(1, 13)
    ]
    lines = mbo.work_lines(entries, date(2026, 7, 1))
    assert lines[1] == "- Phoenix: 12 hours"
    notes = lines[-1]
    assert notes.startswith("Recent notes: note 12 | note 11")
    assert notes.count("|") == mbo.RECENT_NOTES - 1


def test_a_plan_period_is_its_own_dates_else_the_hrms_window() -> None:
    assert mbo.period_of({"period_start": "2026-07-01", "period_end": "2026-09-30",
                          "fiscal_year": "2026-27", "quarter": 2}) == "01 Jul 2026 to 30 Sep 2026"
    assert mbo.period_of({"fiscal_year": "2026-27", "quarter": 1}) == "01 Apr 2026 to 15 Jun 2026"
    assert mbo.period_of({"fiscal_year": "2026-27", "quarter": 4}) == "16 Dec 2026 to 15 Mar 2027"
    assert mbo.period_of({"fiscal_year": "?", "quarter": 9}) == ""


def test_the_quarter_after_crosses_the_fiscal_year() -> None:
    assert mbo.next_quarter("2026-27", 2) == ("2026-27", 3)
    assert mbo.next_quarter("2026-27", 4) == ("2027-28", 1)


def test_the_coaching_keeps_deadlines_inside_the_period() -> None:
    plan = {"fiscal_year": "2026-27", "quarter": 3, "can_edit": True}
    lines = mbo.presentation_lines(plan, date(2026, 9, 20))
    assert "  *KPI* — a number or a date between 16 Sep 2026 and 15 Dec 2026" in lines
    assert not any("period end" in line for line in lines)
    assert any(
        'call save_my_mbo_plan with the fiscal year and quarter of the plan worked on (the open'
        ' one: fiscal_year "2026-27", quarter 3)' in line and "show the whole set" in line
        for line in lines
    )
    assert "lay suggestions out as its result says" in mbo.INSTRUCTIONS


def test_the_coaching_warns_when_the_period_is_nearly_over_or_over() -> None:
    plan = {"fiscal_year": "2026-27", "quarter": 3, "can_edit": True}
    assert (
        "- Its period ends on 15 Dec 2026, 3 days from now: say so before suggesting, and keep"
        " targets to what fits." in mbo.presentation_lines(plan, date(2026, 12, 12))
    )
    assert "- Its period ended on 15 Dec 2026: say so before suggesting." in (
        mbo.presentation_lines(plan, date(2026, 12, 20))
    )
    assert (
        "- Its period ends today: say so before suggesting, and keep targets to what fits."
        in mbo.presentation_lines(plan, date(2026, 12, 15))
    )


def test_with_both_quarters_open_the_coaching_gives_each_ones_dates_and_save() -> None:
    this = {"fiscal_year": "2026-27", "quarter": 3, "can_edit": True}
    following = {"fiscal_year": "2026-27", "quarter": 4, "can_edit": True}
    said = "\n".join(mbo.presentation_lines(this, date(2026, 12, 12), other=following))
    assert "this quarter: 16 Sep 2026 to 15 Dec 2026; next: 16 Dec 2026 to 15 Mar 2027" in said
    assert 'next quarter\'s, if they named it: fiscal_year "2026-27", quarter 4' in said
    assert "- If working on this quarter: its period ends on 15 Dec 2026" in said


def test_a_plan_without_a_quarter_still_gives_the_coaching() -> None:
    plan = {"fiscal_year": "2026-27", "quarter": None, "can_edit": True}
    assert mbo.period_dates(plan) is None
    assert "  *KPI* — a number or a date" in mbo.presentation_lines(plan, date(2026, 9, 20))


def test_with_no_plan_open_the_coaching_offers_no_save() -> None:
    said = "\n".join(mbo.presentation_lines(None, date(2026, 9, 20)))
    assert "nothing can be saved until HR opens one" in said
    assert "save them as a Draft" not in said and "save_my_mbo_plan" not in said
