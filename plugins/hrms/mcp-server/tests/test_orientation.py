"""HR's orientation tools against a fake HRMS: what is waiting is laid out with
each time as a date and a time; confirming sends only what HR said, never guesses
a place, and an employee is told only HR can; and the employee's own view says
where their orientation stands."""

from __future__ import annotations

from starlette.testclient import TestClient

from hrms_mcp import my_onboarding, orientation
from hrms_mcp.onboarding import NOT_HR_ADMIN
from tests.test_my_onboarding import MINE
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

WAITING = {"requests": [{
    "employee": "Ethan Parker", "employee_code": "H101",
    "options": [
        {"label": "Tue 3 Nov, 11:00 (IST)", "date": "2026-11-03", "time": "11:00"},
        {"label": "Wed 4 Nov, 15:00 (IST)", "date": "2026-11-04", "time": "15:00"},
    ],
}]}

CONFIRMED = {
    "employee": "Ethan Parker", "when": "Tue 3 Nov, 11:00 (IST)", "place": "Room 2",
    "employee_told": "sent",
}
ARGS = {"employee": "Ethan", "date": "2026-11-03", "time": "11:00", "place": "Room 2"}


def _sent(fake: FakeHrms, tool: str) -> list[dict]:
    return [c["arguments"] for c in fake.calls() if c["name"] == tool]


def test_waiting_requests_show_each_time_as_a_date_and_a_time(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["list_orientation_requests"] = WAITING
    said = text(call(client, "list_orientation_requests"))
    assert "Ethan Parker (H101) is free at:" in said
    assert "1. Tue 3 Nov, 11:00 (IST) — date 2026-11-03, time 11:00" in said
    assert "2. Wed 4 Nov, 15:00 (IST) — date 2026-11-04, time 15:00" in said
    assert "Confirm Ethan's orientation for Tuesday 11am in Room 2." in said


def test_nothing_waiting_is_an_answer(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["list_orientation_requests"] = {"requests": []}
    result = call(client, "list_orientation_requests")
    assert not result.get("isError")
    assert text(result) == orientation.NO_REQUESTS


def test_an_employee_is_told_only_hr_can_as_an_answer(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["list_orientation_requests"] = "access denied: admin only"
    result = call(client, "list_orientation_requests")
    assert not result.get("isError")
    assert text(result) == NOT_HR_ADMIN


def test_confirming_sends_only_what_hr_said(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["confirm_orientation"] = CONFIRMED
    padded = {**ARGS, "employee": "  Ethan  ", "place": " Room 2 "}
    result = call(client, "confirm_orientation", padded)
    assert not result.get("isError"), text(result)
    # A sentence, not the HRMS's data: HR never reads field names or braces.
    said = text(result)
    assert (
        "Ethan Parker's HR orientation is confirmed for Tue 3 Nov, 11:00 (IST), at Room 2."
        in said
    )
    assert "They have been told." in said
    assert "{" not in said and "employee_told" not in said
    [sent] = _sent(hrms, "confirm_orientation")
    assert sent == ARGS | {"employee": "Ethan"}
    assert not {"employee_id", "caller_id"} & set(sent)


def test_a_place_is_never_guessed(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    for place in ("", "   "):
        result = call(client, "confirm_orientation", {**ARGS, "place": place})
        assert result["isError"]
        assert "Ask HR for a room or a meeting link" in text(result)
    assert _sent(hrms, "confirm_orientation") == []


def test_confirming_as_an_employee_is_refused(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["confirm_orientation"] = "access denied: admin only"
    result = call(client, "confirm_orientation", ARGS)
    assert result["isError"]
    assert NOT_HR_ADMIN in text(result)


def test_the_hrms_refusals_reach_hr_as_they_were_said(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.errors["confirm_orientation"] = (
        "that could be any of Ethan Parker (H101), Ethan Quinn (H102) — which one?"
    )
    result = call(client, "confirm_orientation", ARGS)
    assert result["isError"]
    assert "which one?" in text(result)


def test_the_tools_carry_no_person_and_the_guide_is_in_the_description(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools["list_orientation_requests"]["annotations"]["readOnlyHint"] is True
    assert tools["confirm_orientation"]["annotations"]["readOnlyHint"] is False
    required = set(tools["confirm_orientation"]["inputSchema"]["required"])
    assert required == {"employee", "date", "time", "place"}
    for name in ("list_orientation_requests", "confirm_orientation"):
        properties = set(tools[name]["inputSchema"].get("properties", {}))
        assert not {"employee_id", "caller_id"} & properties, name
    said = " ".join(tools["confirm_orientation"]["description"].split())
    for rule in (
        "relay it as it is",
        "never show data, field names, tool names or JSON",
        "so do",
        "Call list_orientation_requests first",
        "tell them, citing it, and stop",
        "Never guess a place",
        "call get_leave_context and take the date from it",
        'never ask "shall I go ahead?" yourself',
        "Safe to repeat",
    ):
        assert rule in said, rule


# --- marking it held ---------------------------------------------------------------

HELD = {
    "employee": "Ethan Parker", "orientation": "completed", "onboarding_complete": True,
    "progress": "8 of 8 steps done",
}


def test_marking_it_held_sends_only_the_person_and_answers_in_a_sentence(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["complete_orientation"] = HELD
    result = call(client, "complete_orientation", {"employee": "  Ethan  "})
    assert not result.get("isError"), text(result)
    said = text(result)
    assert said == (
        "Ethan Parker's HR orientation is marked as completed. "
        "Their onboarding is now complete — every step is done."
    )
    assert "{" not in said and "onboarding_complete" not in said
    [sent] = _sent(hrms, "complete_orientation")
    assert sent == {"employee": "Ethan"}


def test_marking_it_held_as_an_employee_is_refused(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["complete_orientation"] = "access denied: admin only"
    result = call(client, "complete_orientation", {"employee": "Ethan"})
    assert result["isError"] and NOT_HR_ADMIN in text(result)


def test_the_refusals_for_marking_it_held_reach_hr_as_they_were_said(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.errors["complete_orientation"] = (
        "the orientation has not been scheduled yet — confirm a date, time and place first"
    )
    result = call(client, "complete_orientation", {"employee": "Ethan"})
    assert result["isError"] and "not been scheduled" in text(result)


def test_marking_it_held_before_the_rest_is_done_says_what_is_open(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["complete_orientation"] = {
        **HELD, "onboarding_complete": False, "progress": "7 of 9 steps done",
    }
    said = text(call(client, "complete_orientation", {"employee": "Ethan"}))
    assert "is marked as completed" in said
    assert "Their onboarding is not complete yet: 7 of 9 steps done." in said


def test_an_answer_that_does_not_say_it_was_marked_is_an_error_not_a_guess(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["complete_orientation"] = {"employee": "Ethan Parker"}
    assert call(client, "complete_orientation", {"employee": "Ethan"})["isError"]


def test_marking_it_held_is_a_write_that_waits_until_it_has_happened(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    tool = tools["complete_orientation"]
    assert tool["annotations"]["readOnlyHint"] is False
    assert set(tool["inputSchema"]["required"]) == {"employee"}
    assert not {"employee_id", "caller_id"} & set(tool["inputSchema"].get("properties", {}))
    said = " ".join(tool["description"].split())
    for rule in (
        "HAS happened",
        "never call it, before HR says the orientation has been held",
        'never ask "shall I go ahead?" yourself',
        "whether their onboarding is now complete",
    ):
        assert rule in said, rule
    # Confirming a time schedules it; the guide must not say it finishes the onboarding.
    confirm = " ".join(tools["confirm_orientation"]["description"].split())
    assert "does not complete the onboarding" in confirm
    assert "HR marks it completed" in confirm


# --- the employee's own view -----------------------------------------------------


def _with(orient: dict | None, orientation_status: str = "pending", own_done: bool = True) -> dict:
    steps = []
    for step in MINE["steps"]:
        if step["key"] in ("first_login", "documents_uploaded", "profile_confirmed"):
            step = {**step, "status": "completed" if own_done else step["status"]}
        if step["key"] == "orientation_scheduled":
            step = {**step, "status": orientation_status}
        steps.append(step)
    return {
        **MINE, "steps": steps, "profile_gaps": [], "missing_documents": [], "orientation": orient,
    }


def test_once_their_steps_are_done_they_are_pointed_at_the_pickers() -> None:
    said = my_onboarding.onboarding_lines(_with(None), "https://hr.example.com")
    assert "Choose two or more times for your HR orientation with the date and time pickers" in said
    assert "https://hr.example.com/employees/emp-0042" in said
    assert "Still to do, by you: Joined Slack, HR orientation scheduled." in said


def test_before_that_orientation_is_not_yet_asked_of_them() -> None:
    said = my_onboarding.onboarding_lines(_with(None, own_done=False), "")
    assert "Choose two or more times" not in said


def test_after_proposing_it_is_hrs_move() -> None:
    labels = ["Tue 3 Nov, 11:00 (IST)", "Wed 4 Nov, 15:00 (IST)"]
    orient = {"status": "requested", "slot_labels": labels}
    said = my_onboarding.onboarding_lines(_with(orient, "in_progress"), "")
    assert f"You've proposed times for your HR orientation ({'; '.join(labels)})" in said
    assert "by HR: " in said and "HR orientation scheduled" in said.split("by HR:")[1]
    assert "Choose two or more times" not in said


def test_once_confirmed_they_are_told_when_and_where() -> None:
    orient = {"status": "confirmed", "confirmed_label": "Tue 3 Nov, 11:00 (IST)", "place": "Room 2"}
    said = my_onboarding.onboarding_lines(_with(orient, "completed"), "")
    assert "Your HR orientation: Tue 3 Nov, 11:00 (IST) — Room 2." in said
    assert "Choose two or more times" not in said


def test_hr_is_told_whether_the_employee_was_reached(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    """Only a delivered message is said to have reached them; anything else says to tell them."""
    cases = (("sent", True), ("failed", False), ("skipped", False), ("not_sent", False))
    for state, reached in cases:
        hrms.results["confirm_orientation"] = {**CONFIRMED, "employee_told": state}
        said = text(call(client, "confirm_orientation", ARGS))
        assert ("They have been told." in said) is reached, state
        assert ("let them know yourself" in said) is (not reached), state


def test_the_calendar_is_mentioned_only_when_it_happened_or_failed(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    cases = {
        "added": "It is on your calendar, and a calendar invitation has been sent to them.",
        "updated": "The calendar event was updated, and they have been sent the change.",
    }
    for state, sentence in cases.items():
        hrms.results["confirm_orientation"] = {**CONFIRMED, "calendar": state}
        assert sentence in text(call(client, "confirm_orientation", ARGS)), state
    hrms.results["confirm_orientation"] = {
        **CONFIRMED, "calendar": "failed", "calendar_error": "Google refused the delegation",
    }
    said = text(call(client, "confirm_orientation", ARGS))
    assert "I could not put it on the calendar (Google refused the delegation)" in said
    assert "please add it yourself" in said
    for quiet in ("not_set_up", "unchanged", ""):
        hrms.results["confirm_orientation"] = {**CONFIRMED, "calendar": quiet}
        said = text(call(client, "confirm_orientation", ARGS))
        assert "calendar" not in said, quiet


def test_a_changed_time_reads_as_rescheduled_and_a_changed_place_as_a_new_place(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["confirm_orientation"] = {
        **CONFIRMED, "when": "Wed 4 Nov, 15:00 (IST)", "rescheduled": True,
        "was_when": "Tue 3 Nov, 11:00 (IST)", "was_place": "Room 2",
    }
    said = text(call(client, "confirm_orientation", ARGS))
    assert "rescheduled from Tue 3 Nov, 11:00 (IST) to Wed 4 Nov, 15:00 (IST), at Room 2." in said
    assert "is confirmed for" not in said
    hrms.results["confirm_orientation"] = {
        **CONFIRMED, "place": "Room 5", "rescheduled": True,
        "was_when": "Tue 3 Nov, 11:00 (IST)", "was_place": "Room 2",
    }
    said = text(call(client, "confirm_orientation", ARGS))
    assert (
        "stays on Tue 3 Nov, 11:00 (IST), but the place has changed from Room 2 to Room 5"
        in said
    )
    assert "rescheduled" not in said

