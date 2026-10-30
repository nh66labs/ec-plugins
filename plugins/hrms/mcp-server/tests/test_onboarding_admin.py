"""HR's onboarding tools against a fake HRMS: they carry no wording of their own —
HR's request goes through as said and the HRMS's data comes back untouched; an
employee is told only HR can, as an answer for the reads and a refusal for the
changes; and the guide for each is in its description, which is where it lives."""

from __future__ import annotations

from starlette.testclient import TestClient

from hrms_mcp import onboarding_admin
from hrms_mcp.onboarding import NOT_HR_ADMIN
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

LIST = {"in_progress": [{
    "employee": "Ethan Parker", "employee_code": "H101", "progress": "5 of 8 steps done",
    "current_step": "HR orientation scheduled",
    "needs_hr": ["Setup email sent failed: no personal email"],
}]}
STATUS = {
    "employee": "Ethan Parker", "employee_code": "H101", "status": "in_progress",
    "progress": "5 of 8 steps done", "started": "Mon 2 Nov, 10:00 (IST)",
    "steps": [{"key": "setup_email", "step": "Setup email sent", "status": "failed",
               "required": True, "error": "no personal email on the profile"}],
}
CHANGED = {"employee": "Ethan Parker", "progress": "5 of 8 steps done",
           "step": "Setup email sent", "status": "pending"}

TOOLS = (
    "list_onboardings", "get_onboarding_status", "retry_onboarding_step",
    "mark_onboarding_step_not_required", "require_onboarding_step", "start_onboarding",
)


def _sent(fake: FakeHrms, tool: str) -> list[dict]:
    return [c["arguments"] for c in fake.calls() if c["name"] == tool]


def test_the_answers_are_plain_sentences_not_data(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["list_onboardings"] = LIST
    hrms.results["get_onboarding_status"] = STATUS
    listed = text(call(client, "list_onboardings"))
    assert listed == (
        "1 person is being onboarded:\n"
        "• Ethan Parker (H101) — 5 of 8 steps done; now on: HR orientation scheduled; "
        "waiting on HR: Setup email sent failed: no personal email."
    )
    status = text(call(client, "get_onboarding_status", {"employee": "Ethan"}))
    assert status.startswith(
        "Ethan Parker (H101) — onboarding in progress: 5 of 8 steps done. "
        "Started Mon 2 Nov, 10:00 (IST)."
    )
    assert "• Setup email sent — failed: no personal email on the profile" in status
    for said in (listed, status):
        assert "{" not in said and '"' not in said
        for internal in ("employee_code", "current_step", "needs_hr", "setup_email"):
            assert internal not in said, internal


def test_nobody_being_onboarded_is_said_in_words(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["list_onboardings"] = {"in_progress": []}
    assert text(call(client, "list_onboardings")) == "No one is being onboarded right now."


def test_a_request_goes_through_as_hr_said_it(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    for tool in ("retry_onboarding_step", "require_onboarding_step",
                 "mark_onboarding_step_not_required", "start_onboarding"):
        hrms.results[tool] = CHANGED
    call(client, "get_onboarding_status", {"employee": "  Ethan  "})
    call(client, "retry_onboarding_step", {"employee": " Ethan Parker ", "step": " setup email "})
    call(client, "require_onboarding_step", {"employee": "Ethan", "step": "documents"})
    call(client, "mark_onboarding_step_not_required",
         {"employee": "Ethan", "step": "documents", "reason": " internal project "})
    call(client, "mark_onboarding_step_not_required", {"employee": "Ethan", "step": "documents"})
    call(client, "start_onboarding", {"employee": "Priya"})
    assert _sent(hrms, "get_onboarding_status") == [{"employee": "Ethan"}]
    assert _sent(hrms, "retry_onboarding_step") == [
        {"employee": "Ethan Parker", "step": "setup email"}
    ]
    assert _sent(hrms, "require_onboarding_step") == [{"employee": "Ethan", "step": "documents"}]
    assert _sent(hrms, "mark_onboarding_step_not_required") == [
        {"employee": "Ethan", "step": "documents", "reason": "internal project"},
        {"employee": "Ethan", "step": "documents", "reason": ""},
    ]
    assert _sent(hrms, "start_onboarding") == [{"employee": "Priya"}]


def test_a_change_says_what_happened_in_a_sentence(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["retry_onboarding_step"] = CHANGED
    result = call(client, "retry_onboarding_step", {"employee": "Ethan", "step": "setup email"})
    assert not result.get("isError")
    assert text(result) == (
        "Setup email sent for Ethan Parker is back on the list to be done. "
        "Onboarding: 5 of 8 steps done."
    )
    hrms.results["start_onboarding"] = {
        "employee": "Priya", "started": True, "progress": "0 of 9 steps done",
        "started_on": "Fri 30 Oct, 11:00 (IST)",
    }
    assert text(call(client, "start_onboarding", {"employee": "Priya"})) == (
        "Started Priya's onboarding on Fri 30 Oct, 11:00 (IST); the setup email is on its way. "
        "0 of 9 steps done."
    )
    hrms.results["start_onboarding"]["started"] = False
    assert "had already begun on Fri 30 Oct, 11:00 (IST) — nothing was changed" in text(
        call(client, "start_onboarding", {"employee": "Priya"})
    )


def test_an_employee_is_told_only_hr_can(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    for tool in TOOLS:
        hrms.errors[tool] = "access denied: admin only"
    for tool in ("list_onboardings", "get_onboarding_status"):
        result = call(client, tool, {"employee": "Ethan"} if tool != "list_onboardings" else {})
        assert not result.get("isError"), tool  # an answer, which the reply can cite
        assert text(result) == NOT_HR_ADMIN, tool
    for tool, args in (
        ("retry_onboarding_step", {"employee": "Ethan", "step": "setup email"}),
        ("mark_onboarding_step_not_required", {"employee": "Ethan", "step": "documents"}),
        ("require_onboarding_step", {"employee": "Ethan", "step": "documents"}),
        ("start_onboarding", {"employee": "Ethan"}),
    ):
        result = call(client, tool, args)
        assert result["isError"], tool
        assert NOT_HR_ADMIN in text(result), tool


def test_the_hrms_refusals_reach_hr_in_its_words(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["retry_onboarding_step"] = "only a failed step can be retried"
    result = call(client, "retry_onboarding_step", {"employee": "Ethan", "step": "documents"})
    assert result["isError"]
    assert "only a failed step can be retried" in text(result)
    hrms.errors["get_onboarding_status"] = (
        "that could be any of Ethan Parker (H101), Ethan Quinn (H102) — which one?"
    )
    result = call(client, "get_onboarding_status", {"employee": "Ethan"})
    assert result["isError"]
    assert "which one?" in text(result)


def test_the_tools_name_no_id_and_changes_are_writes(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    for name in TOOLS:
        properties = set(tools[name]["inputSchema"].get("properties", {}))
        assert not {"employee_id", "caller_id", "id", "approver_id"} & properties, name
    for name in ("list_onboardings", "get_onboarding_status"):
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    for name in TOOLS[2:]:
        assert tools[name]["annotations"]["readOnlyHint"] is False, name
    assert set(tools["retry_onboarding_step"]["inputSchema"]["required"]) == {"employee", "step"}
    assert "reason" not in tools["mark_onboarding_step_not_required"]["inputSchema"]["required"]


def _descriptions(client: TestClient) -> dict[str, str]:  # noqa: F811
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    return {t["name"]: " ".join(t["description"].split()) for t in tools}


def test_the_guide_for_each_tool_reaches_the_model(client: TestClient) -> None:  # noqa: F811
    said = _descriptions(client)
    for name in TOOLS:
        assert said[name], f"{name} has no description"
        assert "HRMS admins only" in said[name], name
    for name in TOOLS:  # every answer is a finished sentence, never data
        assert "relay it as it is" in said[name], name
        assert "never show data, field names, tool names or JSON" in said[name], name
    assert "it fits several people the tool says so — ask HR which" in said["get_onboarding_status"]
    retry = said["retry_onboarding_step"]
    assert "pass them as said" in retry and "call get_onboarding_status" in retry
    assert "Only a failed step can be retried" in retry
    assert 'never ask "shall I go ahead?"' in retry
    assert "never ask for one" in said["mark_onboarding_step_not_required"]
    assert "undoes it" in said["mark_onboarding_step_not_required"]
    assert "use this only for an employee who was added another way" in said["start_onboarding"]


def test_no_change_tool_offers_to_complete_a_step(client: TestClient) -> None:  # noqa: F811
    said = _descriptions(client)
    for name in (
        "retry_onboarding_step", "mark_onboarding_step_not_required", "require_onboarding_step",
    ):
        assert onboarding_admin.NEVER_COMPLETE in said[name], name
        assert "only when it is verified" in said[name], name
    names = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert not {n for n in names if "complete" in n and "orientation" not in n}


def test_there_is_no_list_of_step_names_here() -> None:
    """HR's words are matched against the steps by the HRMS; none are kept here."""
    source = open(onboarding_admin.__file__, encoding="utf-8").read()
    for internal in ("setup_email", "slack_joined", "documents_uploaded",
                     "profile_confirmed", "orientation_scheduled", "first_login"):
        assert internal not in source, internal
