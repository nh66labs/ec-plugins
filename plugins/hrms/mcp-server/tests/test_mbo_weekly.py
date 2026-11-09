"""The Weekly Plan tools, against the same fake HRMS as the other MBO tools.

What matters most: the weeks come back to the assistant as lines with their dates,
so a week's task is read, not remembered; a save reaches the HRMS as one MBO's
whole set of weeks in the one field the HRMS takes, only for the MBO named by
number *and* title; everything the HRMS would refuse is refused before it is
asked; and every schema is one Enterprise Claw binds and shows as readable lines
on a confirm card.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from hrms_mcp import mbo, mbo_weekly
from tests.test_mbo import _plans  # noqa: F401 - the plan answers the context asks for
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    _bindable,
    call,
    client,
    hrms,
    rpc,
    text,
)

W6 = {"week": 6, "dates": "9–13 Nov", "task": "Freeze scope.", "status": "unmarked"}
W7 = {"week": 7, "dates": "16–20 Nov", "task": "Cut the release candidate.", "status": "done"}

PLAN = {
    "fiscal_year": "2026-27", "quarter": 3, "status": "Draft", "mbo_access_enabled": True,
    "can_edit": True, "mentor": "Mira", "total_weightage": 100,
    "weekly_plan_starts": "2026-10-05", "current_week": 6,
    "mbos": [
        {"title": "Ship v2", "kpi": "Released by 15 Nov", "weightage": 60,
         "completion_percent": 0, "manager_rating": 0,
         "baseline": "v1 only", "target": "v2 live by 15 Nov", "weekly_plan": [W6, W7]},
        {"title": "Cut P1 bugs", "kpi": "Below 5 open", "weightage": 40,
         "completion_percent": 0, "manager_rating": 0, "weekly_plan": []},
    ],
}


def _with(plan: dict, first: dict) -> dict:
    return {**plan, "mbos": [{**plan["mbos"][0], **first}, plan["mbos"][1]]}


@pytest.fixture()
def weekly_hrms(hrms: FakeHrms) -> FakeHrms:  # noqa: F811
    hrms.results.update({
        "get_my_mbo_plan": PLAN,
        "save_my_weekly_plan": PLAN,
        "mark_my_week": PLAN,
    })
    return hrms


SAVE = {
    "fiscal_year": "2026-27", "quarter": 3, "mbo_number": 1, "objective": "Ship v2",
    "week_6": "Freeze scope.", "week_7": "Cut the release candidate.",
}
MARK = {
    "fiscal_year": "2026-27", "quarter": 3, "mbo_number": 1, "objective": "Ship v2",
    "week": 6, "status": "done",
}


def _sent(fake: FakeHrms, name: str) -> list[dict[str, Any]]:
    return [c for c in fake.calls() if c["name"] == name]


# --- what Enterprise Claw sees ---------------------------------------------------


def test_the_weekly_tools_are_listed_with_their_kind(client) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    save, mark = tools["save_my_weekly_plan"], tools["mark_my_week"]
    assert save["annotations"]["readOnlyHint"] is False
    assert save["annotations"]["idempotentHint"] is True
    # Marking a week can be taken back, so it is not a destructive write.
    assert mark["annotations"]["readOnlyHint"] is False
    assert mark["annotations"]["destructiveHint"] is False
    assert mark["annotations"]["idempotentHint"] is True


def test_a_week_is_one_field_so_a_confirm_card_reads_as_lines(client) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    save = tools["save_my_weekly_plan"]["inputSchema"]
    properties = save["properties"]
    assert {f"week_{n}" for n in range(1, 13)} <= set(properties)
    assert {"baseline", "target", "mbo_number", "objective"} <= set(properties)
    assert all(node["type"] in {"string", "number", "integer"} for node in properties.values())
    assert set(save["required"]) == {"fiscal_year", "quarter", "mbo_number", "objective"}
    for name in ("save_my_weekly_plan", "mark_my_week"):
        assert _bindable(tools[name]["inputSchema"]) == [], name
    assert set(tools["mark_my_week"]["inputSchema"]["required"]) == {
        "fiscal_year", "quarter", "mbo_number", "objective", "week", "status",
    }


def test_the_descriptions_carry_the_rules_the_instructions_have_no_room_for(client) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    save = tools["save_my_weekly_plan"]["description"]
    for rule in (
        "only after their MBOs are saved and they have agreed",
        "say the tasks are your suggestion",
        "never a past week",
        "a week left out is cleared",
        "Call it once per MBO",
        "replaces a self rating",
    ):
        assert " ".join(rule.split()) in " ".join(save.split()), rule
    mark = " ".join(tools["mark_my_week"]["description"].split())
    assert "never mark a week on their behalf" in mark
    plan = " ".join(tools["get_my_mbo_plan"]["description"].split())
    assert "never make one up" in plan


def test_the_instructions_point_at_the_weekly_tool_and_still_fit(client) -> None:  # noqa: F811
    result = rpc(
        client, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "enterprise-claw", "version": "2"}},
    ).json()["result"]
    # What the platform keeps of them: a rule past its limit never reaches the model.
    assert "Weekly tasks: follow save_my_weekly_plan." in result["instructions"][:4_000]
    assert len(result["instructions"]) <= 4_000


# --- reading the weeks -------------------------------------------------------------


def test_the_plan_read_carries_baseline_target_weeks_and_today(client, weekly_hrms) -> None:  # noqa: F811
    lines = text(call(client, "get_my_mbo_plan", {})).split("\n")
    first = next(i for i, line in enumerate(lines) if line.startswith("1. Ship v2"))
    assert lines[first + 1:first + 4] == [
        "   Baseline: v1 only · Target: v2 live by 15 Nov",
        "   W6 (9–13 Nov) [unmarked]: Freeze scope.",
        "   W7 (16–20 Nov) [done]: Cut the release candidate.",
    ]
    # An MBO with no weeks has no week lines under it.
    assert lines[first + 4].startswith("2. Cut P1 bugs")
    assert lines[first + 5] == "Today falls in week W6 (9–13 Nov)."


def test_the_context_stays_short_and_has_no_weeks(client, weekly_hrms) -> None:  # noqa: F811
    _plans(weekly_hrms, PLAN, PLAN)
    said = text(call(client, "get_my_mbo_context", {}))
    assert "Ship v2" in said
    assert "W6" not in said and "Baseline" not in said and "Today falls" not in said


def test_an_hrms_that_sends_no_weekly_fields_reads_as_before(client, hrms) -> None:  # noqa: F811
    old = {
        "fiscal_year": "2026-27", "quarter": 3, "status": "Draft", "can_edit": True,
        "mbos": [{"title": "Ship v2", "kpi": "k", "weightage": 100,
                  "completion_percent": 0, "manager_rating": 0}],
    }
    hrms.results["get_my_mbo_plan"] = old
    said = text(call(client, "get_my_mbo_plan", {}))
    assert "1. Ship v2" in said
    assert "Baseline" not in said and "[" not in said and "Today falls" not in said


def test_the_saved_and_deleted_answers_are_not_changed_by_the_weeks() -> None:
    bare = [{k: v for k, v in m.items() if k != "weekly_plan"} for m in PLAN["mbos"]]
    assert mbo.plan_lines(PLAN, "Plan") == mbo.plan_lines({**PLAN, "mbos": bare}, "Plan")


def test_week_dates_are_worked_as_the_hrms_works_them() -> None:
    monday = date(2026, 10, 5)  # W1 of Q3 2026-27, which starts on a Thursday
    for week, want in {1: "5–9 Oct", 6: "9–13 Nov", 9: "30 Nov–4 Dec", 12: "21–25 Dec"}.items():
        assert mbo_weekly.week_dates(monday, week) == want
    assert mbo_weekly.week_dates(date(2027, 1, 4), 1) == "4–8 Jan"


@pytest.mark.parametrize(
    ("plan", "want"),
    [
        (
            {"current_week": 6, "weekly_plan_starts": "2026-10-05"},
            "Today falls in week W6 (9–13 Nov).",
        ),
        ({"current_week": 6}, "Today falls in week W6."),
        ({"current_week": 6, "weekly_plan_starts": "soon"}, "Today falls in week W6."),
        ({"weekly_plan_starts": "2026-10-05"}, None),  # before W1 / after W12: left out
        ({"current_week": 0}, None),
        ({"current_week": 13}, None),
        ({"current_week": True}, None),
        ({"current_week": "6"}, None),
    ],
)
def test_today_is_said_only_when_the_hrms_names_a_week(plan: dict, want: str | None) -> None:
    assert mbo_weekly.current_week_line(plan) == want


def test_weeks_lines_cope_with_partial_or_odd_data() -> None:
    assert mbo_weekly.weeks_lines({}) == []
    assert mbo_weekly.weeks_lines({"target": " T "}) == ["   Target: T"]
    assert mbo_weekly.weeks_lines({"weekly_plan": "nope"}) == []
    assert mbo_weekly.weeks_lines({"weekly_plan": [None, {"week": 3, "task": "x"}]}) == [
        "   W3 [unmarked]: x"
    ]


# --- the save ----------------------------------------------------------------------


def test_a_save_sends_one_mbos_weeks_as_the_hrms_takes_them(client, weekly_hrms) -> None:  # noqa: F811
    said = text(call(client, "save_my_weekly_plan", {
        **SAVE, "baseline": "v1 only", "target": "v2 live by 15 Nov", "week_9": "  Ship it.  ",
    }))
    assert said.split("\n")[0] == "Saved the weekly plan of MBO 1: Ship v2."
    assert "   W6 (9–13 Nov) [unmarked]: Freeze scope." in said
    assert said.endswith("The plan is still a Draft.")
    (sent,) = _sent(weekly_hrms, "save_my_weekly_plan")
    assert sent["arguments"]["fiscal_year"] == "2026-27" and sent["arguments"]["quarter"] == 3
    assert json.loads(sent["arguments"]["weekly_plan"]) == [{
        "mbo_number": 1, "title": "Ship v2",
        "baseline": "v1 only", "target": "v2 live by 15 Nov",
        "weeks": [
            {"week": 6, "task": "Freeze scope."},
            {"week": 7, "task": "Cut the release candidate."},
            {"week": 9, "task": "Ship it."},
        ],
    }]


def test_an_empty_baseline_or_target_is_not_sent(client, weekly_hrms) -> None:  # noqa: F811
    """So what the HRMS stores for them stays."""
    call(client, "save_my_weekly_plan", {**SAVE, "baseline": "  ", "target": ""})
    (entry,) = json.loads(_sent(weekly_hrms, "save_my_weekly_plan")[0]["arguments"]["weekly_plan"])
    assert "baseline" not in entry and "target" not in entry


def test_the_mbo_is_read_before_anything_is_written(client, weekly_hrms) -> None:  # noqa: F811
    call(client, "save_my_weekly_plan", SAVE)
    names = [c["name"] for c in weekly_hrms.calls() if c["name"].endswith("_plan")]
    assert names.index("get_my_mbo_plan") < names.index("save_my_weekly_plan")


def test_the_person_is_told_when_the_weeks_replace_a_self_rating(client, weekly_hrms) -> None:  # noqa: F811
    typed = _with(PLAN, {"completion_percent": 40})
    weekly_hrms.results["get_my_mbo_plan"] = typed
    weekly_hrms.results["save_my_weekly_plan"] = _with(PLAN, {"completion_percent": 0})
    said = text(call(client, "save_my_weekly_plan", SAVE))
    assert (
        "Its completion is now worked out from the weeks (0%), replacing the 40% self rating."
        in said
    )

    # Nothing to say when there was no self rating, or when it did not change.
    weekly_hrms.results["get_my_mbo_plan"] = PLAN
    assert "self rating" not in text(call(client, "save_my_weekly_plan", SAVE))
    weekly_hrms.results["get_my_mbo_plan"] = typed
    weekly_hrms.results["save_my_weekly_plan"] = typed
    assert "self rating" not in text(call(client, "save_my_weekly_plan", SAVE))


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"week_6": "", "week_7": "  "}, "at least one week"),
        ({"week_6": "x" * 1001}, "week 6 is too long"),
        ({"baseline": "x" * 1001}, "baseline is too long"),
        ({"target": "x" * 1001}, "target is too long"),
        ({"objective": "  "}, "objective (title) is needed"),
        ({"quarter": 5}, "quarter must be 1-4"),
        ({"quarter": 0}, "quarter must be 1-4"),
        ({"mbo_number": 0}, "counts from 1"),
        ({"mbo_number": 3}, "there is no MBO 3"),
        ({"objective": "Cut P1 bugs"}, "MBO 1 on the plan is 'Ship v2', not 'Cut P1 bugs'"),
    ],
)
def test_what_the_hrms_would_refuse_is_refused_before_it_is_asked(
    client, weekly_hrms, change: dict, says: str,  # noqa: F811
) -> None:
    result = call(client, "save_my_weekly_plan", {**SAVE, **change})
    assert result["isError"] and says in text(result)
    assert not _sent(weekly_hrms, "save_my_weekly_plan")


def test_a_plan_with_no_mbos_says_to_save_them_first(client, weekly_hrms) -> None:  # noqa: F811
    weekly_hrms.results["get_my_mbo_plan"] = {**PLAN, "mbos": []}
    result = call(client, "save_my_weekly_plan", SAVE)
    assert result["isError"] and "Save the MBOs first" in text(result)
    assert not _sent(weekly_hrms, "save_my_weekly_plan")


def test_the_title_matches_whatever_the_case_or_spacing(client, weekly_hrms) -> None:  # noqa: F811
    result = call(client, "save_my_weekly_plan", {**SAVE, "objective": "  ship V2 "})
    assert not result.get("isError")
    (entry,) = json.loads(_sent(weekly_hrms, "save_my_weekly_plan")[0]["arguments"]["weekly_plan"])
    assert entry["title"] == "ship V2"  # the HRMS compares the same way


def test_the_hrms_refusing_a_save_is_said_plainly(client, weekly_hrms) -> None:  # noqa: F811
    weekly_hrms.errors["save_my_weekly_plan"] = (
        "cannot save: the Q3 2026-27 plan is Submitted; "
        "only a Draft plan's weekly plan can be changed"
    )
    result = call(client, "save_my_weekly_plan", SAVE)
    assert result["isError"] and "only a Draft plan's weekly plan can be changed" in text(result)


# --- marking a week ------------------------------------------------------------------


def test_a_mark_names_the_mbo_by_number_and_title(client, weekly_hrms) -> None:  # noqa: F811
    weekly_hrms.results["mark_my_week"] = _with(PLAN, {"completion_percent": 50})
    said = text(call(client, "mark_my_week", MARK))
    assert said.split("\n") == [
        "Marked W6 of MBO 1 (Ship v2) as Done.",
        "This MBO's completion is now 50%.",
    ]
    (sent,) = _sent(weekly_hrms, "mark_my_week")
    assert sent["arguments"] == {
        "fiscal_year": "2026-27", "quarter": 3, "mbo_number": 1, "title": "Ship v2",
        "week": 6, "status": "done",
    }


@pytest.mark.parametrize(
    ("said", "sent", "words"),
    [
        ("done", "done", "Done"), ("Done", "done", "Done"),
        ("not_done", "not_done", "Not done"), ("Not done", "not_done", "Not done"),
        ("  NOT   DONE ", "not_done", "Not done"),
        ("unmarked", "unmarked", "not marked"),
    ],
)
def test_a_status_is_taken_as_a_person_says_it(client, weekly_hrms, said, sent, words) -> None:  # noqa: F811
    # W7 is done already, so only an unmarking differs from it.
    week = 6 if sent != "unmarked" else 7
    out = text(call(client, "mark_my_week", {**MARK, "week": week, "status": said}))
    assert f"as {words}." in out
    assert _sent(weekly_hrms, "mark_my_week")[0]["arguments"]["status"] == sent


def test_marking_what_is_already_marked_writes_nothing(client, weekly_hrms) -> None:  # noqa: F811
    said = text(call(client, "mark_my_week", {**MARK, "week": 7, "status": "done"}))
    assert said == "W7 of MBO 1 is already Done. Nothing changed."
    assert not _sent(weekly_hrms, "mark_my_week")


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"week": 9}, "Nothing is planned for week W9"),
        ({"week": 0}, "week must be between 1 and 12"),
        ({"week": 13}, "week must be between 1 and 12"),
        ({"status": "finished"}, 'status must be "done", "not_done" or "unmarked"'),
        ({"status": ""}, 'status must be "done"'),
        ({"objective": "Cut P1 bugs"}, "MBO 1 on the plan is 'Ship v2'"),
        ({"mbo_number": 2, "objective": "Cut P1 bugs"}, "Nothing is planned for week W6"),
        ({"mbo_number": 3}, "there is no MBO 3"),
        ({"quarter": 7}, "quarter must be 1-4"),
        ({"objective": ""}, "objective (title) is needed"),
    ],
)
def test_a_mark_that_cannot_be_right_is_refused_before_the_hrms(
    client, weekly_hrms, change: dict, says: str,  # noqa: F811
) -> None:
    result = call(client, "mark_my_week", {**MARK, **change})
    assert result["isError"] and says in text(result)
    assert not _sent(weekly_hrms, "mark_my_week")


def test_the_hrms_refusing_a_mark_is_said_plainly(client, weekly_hrms) -> None:  # noqa: F811
    weekly_hrms.errors["mark_my_week"] = (
        "cannot mark: the Q3 2026-27 plan is Submitted; "
        "only a Draft plan's weekly plan can be changed"
    )
    result = call(client, "mark_my_week", MARK)
    assert result["isError"] and "cannot mark" in text(result)
