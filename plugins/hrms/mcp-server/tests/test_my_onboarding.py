"""An employee's own onboarding, against a fake HRMS: the lines it is laid out
as, whose each step is, what the profile and documents still need (named, with
a link, never asked for), and confirming the profile."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from hrms_mcp import my_onboarding
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)


def _step(key: str, label: str, status: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "required": True}


MINE = {
    "employee_id": "emp-0042",
    "status": "in_progress",
    "progress": {"done": 2, "total": 7, "percent": 28},
    "steps": [
        _step("setup_email", "Setup email sent", "completed"),
        _step("slack_invite", "Slack invitation sent", "in_progress"),
        _step("slack_joined", "Joined Slack", "pending"),
        _step("first_login", "Signed in to HR-Assist", "completed"),
        _step("documents_extra", "Extra check", "not_required"),
        _step("documents_uploaded", "Documents uploaded", "in_progress"),
        _step("profile_confirmed", "Profile confirmed", "pending"),
        _step("orientation_scheduled", "HR orientation scheduled", "pending"),
    ],
    "profile_gaps": ["date of birth", "PAN"],
    "missing_documents": ["Aadhaar card"],
}


def test_the_lines_say_what_is_done_whose_the_rest_is_and_what_is_missing() -> None:
    said = my_onboarding.onboarding_lines(MINE, "https://hr.example.com")
    assert said.startswith("Your onboarding: 2 of 7 steps done.")
    assert "Done: Setup email sent, Signed in to HR-Assist." in said
    assert "Still to do, by you: Joined Slack, Documents uploaded, Profile confirmed, " \
        "HR orientation scheduled." in said
    assert "Still to do, by HR: Slack invitation sent." in said
    assert "Extra check" not in said  # not required: neither done nor left
    assert "fill in your date of birth, PAN" in said
    assert "https://hr.example.com/employees/emp-0042/edit" in said
    assert "Documents still needed: Aadhaar card" in said
    assert "https://hr.example.com/employees/emp-0042" in said


def test_without_the_dashboard_address_the_page_is_named_once() -> None:
    said = my_onboarding.onboarding_lines(MINE, "")
    assert "fill in your date of birth, PAN on My Profile → Edit Profile in HR-Assist" in said
    assert "upload them on My Profile → Documents in HR-Assist" in said
    assert "http" not in said


def test_a_step_with_no_label_is_still_listed() -> None:
    ob = {**MINE, "steps": [{"key": "first_login", "status": "pending"}, {"status": "pending"}]}
    said = my_onboarding.onboarding_lines(ob, "")
    assert "Still to do, by you: first_login" in said
    assert "a step" in said


def test_two_steps_with_one_label_are_each_listed_under_their_own() -> None:
    steps = [
        {"key": "first_login", "label": "Same", "status": "pending"},
        {"key": "slack_invite", "label": "Same", "status": "pending"},
    ]
    said = my_onboarding.onboarding_lines({**MINE, "steps": steps}, "")
    assert "Still to do, by you: Same." in said
    assert "Still to do, by HR: Same." in said


def test_the_hrms_rewording_a_message_changes_nothing(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    """The plugin goes by the code at the start of a refusal, never by its wording."""
    hrms.errors["get_my_onboarding"] = "[no_onboarding] Onboarding not found for you."
    assert text(call(client, "get_my_onboarding")) == my_onboarding.NO_ONBOARDING
    hrms.errors["confirm_my_profile"] = "[profile_incomplete] PAN, IFSC code"
    hrms.results["get_user_session"] = {"employee_id": "emp-0042"}
    said = text(call(client, "confirm_my_profile"))
    assert "Fill in your PAN, IFSC code" in said
    assert "Nothing was confirmed." in said
    # Without the code, wording alone is not taken to mean anything.
    hrms.errors["get_my_onboarding"] = "this employee has no onboarding"
    assert call(client, "get_my_onboarding")["isError"]


def test_a_complete_profile_is_offered_for_confirming() -> None:
    said = my_onboarding.onboarding_lines({**MINE, "profile_gaps": []}, "")
    assert "Your profile is complete: you can confirm it now." in said
    assert "fill in" not in said


def test_a_finished_onboarding_says_so() -> None:
    said = my_onboarding.onboarding_lines({**MINE, "status": "completed"}, "")
    assert said == "Your onboarding is complete — every step is done."


def test_get_my_onboarding_answers_and_names_no_one(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["get_my_onboarding"] = MINE
    result = call(client, "get_my_onboarding")
    assert not result.get("isError")
    assert "2 of 7 steps done" in text(result)
    [sent] = [c for c in hrms.calls() if c["name"] == "get_my_onboarding"]
    assert sent["arguments"] == {}


def test_no_onboarding_is_an_answer_not_a_failure(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["get_my_onboarding"] = "[no_onboarding] this employee has no onboarding"
    result = call(client, "get_my_onboarding")
    assert not result.get("isError")
    assert text(result) == my_onboarding.NO_ONBOARDING


def test_confirming_says_so_with_the_progress(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["confirm_my_profile"] = {**MINE, "progress": {"done": 3, "total": 7}}
    result = call(client, "confirm_my_profile")
    assert not result.get("isError")
    assert text(result) == "Your profile is confirmed. Onboarding: 3 of 7 steps done."


def test_an_incomplete_profile_is_refused_with_where_to_fill_it_in(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.errors["confirm_my_profile"] = "[profile_incomplete] date of birth, PAN"
    hrms.results["get_user_session"] = {"employee_id": "emp-0042"}
    result = call(client, "confirm_my_profile")
    assert result["isError"]
    said = text(result)
    assert "Fill in your date of birth, PAN on My Profile → Edit Profile in HR-Assist, " \
        "then confirm. Nothing was confirmed." in said


def test_the_tools_carry_no_person_and_confirming_is_a_write(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools["get_my_onboarding"]["annotations"]["readOnlyHint"] is True
    assert tools["confirm_my_profile"]["annotations"]["readOnlyHint"] is False
    for name in ("get_my_onboarding", "confirm_my_profile"):
        assert not tools[name]["inputSchema"].get("properties"), name
    said = " ".join(tools["get_my_onboarding"]["description"].split())
    assert "never ask anyone to type those into the chat" in said
    said = " ".join(tools["confirm_my_profile"]["description"].split())
    assert "propose it only when that says the profile is complete" in said
