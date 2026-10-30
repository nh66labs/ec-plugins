"""The wording of HR's onboarding answers, on its own: names and dates as people write
them, never field names or braces, and a person is "they"."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp import say


def test_a_status_lays_out_the_steps_and_the_orientation_in_words() -> None:
    said = say.onboarding_status({
        "employee": "Ada Lovelace", "employee_code": "A1", "status": "completed",
        "progress": "9 of 9 steps done", "started": "Mon 2 Nov", "completed": "Fri 6 Nov",
        "steps": [
            {"key": "setup_email", "step": "Setup email sent", "status": "completed"},
            {"key": "slack_invite", "step": "Slack invitation sent", "status": "not_required"},
            {"key": "documents_uploaded", "step": "Documents uploaded", "status": "in_progress",
             "note": "still needed: PAN card"},
        ],
        "reminders_latest": [{"when": "Tue 3 Nov, 10:00", "channel": "slack", "status": "sent",
                              "steps": ["x"]}],
        "orientation": {"status": "confirmed", "confirmed_for": "Thu 5 Nov, 11:00 (IST)",
                        "place": "Room 2"},
    })
    assert said.splitlines()[0] == (
        "Ada Lovelace (A1) — onboarding complete: 9 of 9 steps done. "
        "Started Mon 2 Nov, completed Fri 6 Nov."
    )
    assert "• Slack invitation sent — not required" in said
    assert "• Documents uploaded — in progress: still needed: PAN card" in said
    assert "HR orientation: Thu 5 Nov, 11:00 (IST), at Room 2." in said
    assert "Latest reminders: Tue 3 Nov, 10:00 by slack (sent)." in said
    assert "documents_uploaded" not in said and "{" not in said


def test_proposed_times_are_listed_as_waiting_for_hr() -> None:
    said = say.onboarding_status({
        "employee": "Ada", "employee_code": "A1", "status": "in_progress", "progress": "6 of 9",
        "steps": [],
        "orientation": {
            "status": "requested", "proposed_times": ["Tue 3 Nov, 11:00", "Wed 4 Nov, 15:00"],
        },
    })
    assert (
        "they proposed Tue 3 Nov, 11:00; Wed 4 Nov, 15:00 — waiting for HR to confirm one"
        in said
    )


def test_a_person_is_never_given_a_pronoun_by_name() -> None:
    said = say.orientation_completed({
        "employee": "Daniel Jacob", "orientation": "completed", "onboarding_complete": True,
    })
    assert said == (
        "Daniel Jacob's HR orientation is marked as completed. "
        "Their onboarding is now complete — every step is done."
    )
    for pronoun in (" his ", " her ", " he ", " she "):
        assert pronoun not in f" {said} ".lower()


def test_anything_that_is_not_an_answer_is_an_error_not_a_guess() -> None:
    for worded in (
        say.orientation_confirmed, say.orientation_completed, say.onboarding_list,
        say.onboarding_status, say.step_change, say.onboarding_started,
    ):
        with pytest.raises(ToolError):
            worded("not a dict")
    with pytest.raises(ToolError):
        say.orientation_confirmed({})
    with pytest.raises(ToolError):
        say.orientation_completed({"employee": "Ada", "orientation": "pending"})


def test_a_missing_field_is_left_out_never_written_as_none() -> None:
    said = say.onboarding_list({"in_progress": [{"employee": "Priya", "progress": None}]})
    assert "None" not in said and "Priya" in said
    said = say.onboarding_status({"employee": "Priya", "steps": [{"step": "Email"}]})
    assert "None" not in said
    said = say.orientation_confirmed({"when": "Tue 3 Nov, 11:00", "rescheduled": True})
    assert "None" not in said and "Tue 3 Nov, 11:00" in said


def test_odd_shapes_do_not_break_the_answer() -> None:
    said = say.onboarding_list({"in_progress": [
        "not a row", {"employee": "Priya", "needs_hr": "wait for docs"},
    ]})
    assert "waiting on HR: wait for docs" in said
    said = say.onboarding_status({
        "employee": "Priya", "status": "cancelled", "steps": ["junk"],
        "orientation": {"proposed_times": [{"at": "x"}]},
    })
    assert "cancelled" in said and "in progress" not in said


def test_an_unknown_told_state_is_not_called_unreachable() -> None:
    said = say.orientation_confirmed({"when": "Tue 3 Nov", "place": "Room 2",
                                      "employee_told": "queued"})
    assert "could not tell whether they were told" in said
    assert "could not be reached" not in said


def test_the_reschedule_says_only_what_changed() -> None:
    both_same = say.orientation_confirmed({
        "employee": "Ada", "when": "Tue 3 Nov", "place": "Room 2",
        "rescheduled": True, "was_when": "Tue 3 Nov",
    })
    assert "changed from" not in both_same
    place = say.orientation_confirmed({
        "employee": "Ada", "when": "Tue 3 Nov", "place": "Room 2",
        "rescheduled": True, "was_when": "Tue 3 Nov", "was_place": "Room 1",
    })
    assert "from Room 1 to Room 2" in place


def test_a_done_write_without_a_name_is_still_reported_as_done() -> None:
    assert "completed" in say.orientation_completed({"orientation": "completed"})
