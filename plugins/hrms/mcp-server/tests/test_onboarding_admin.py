"""HR's onboarding tools against a fake HRMS: they carry no wording of their own —
HR's request goes through as said and the HRMS's data comes back untouched; an
employee is told only HR can, as an answer for the reads and a refusal for the
changes; and the guide for each is in its description, which is where it lives."""

from __future__ import annotations

import json

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
    "current_step": "HR orientation scheduled", "needs_hr": ["Jira: has no Jira account yet"],
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


def test_the_data_comes_back_untouched(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["list_onboardings"] = LIST
    hrms.results["get_onboarding_status"] = STATUS
    assert json.loads(text(call(client, "list_onboardings"))) == LIST
    assert json.loads(text(call(client, "get_onboarding_status", {"employee": "Ethan"}))) == STATUS


def test_a_request_goes_through_as_hr_said_it(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    for tool in ("retry_onboarding_step", "require_onboarding_step",
                 "mark_onboarding_step_not_required", "start_onboarding"):
        hrms.results[tool] = CHANGED
    call(client, "get_onboarding_status", {"employee": "  Ethan  "})
    call(client, "retry_onboarding_step", {"employee": " Ethan Parker ", "step": " setup email "})
    call(client, "require_onboarding_step", {"employee": "Ethan", "step": "Jira"})
    call(client, "mark_onboarding_step_not_required",
         {"employee": "Ethan", "step": "jira", "reason": " internal project "})
    call(client, "mark_onboarding_step_not_required", {"employee": "Ethan", "step": "jira"})
    call(client, "start_onboarding", {"employee": "Priya"})
    assert _sent(hrms, "get_onboarding_status") == [{"employee": "Ethan"}]
    assert _sent(hrms, "retry_onboarding_step") == [
        {"employee": "Ethan Parker", "step": "setup email"}
    ]
    assert _sent(hrms, "require_onboarding_step") == [{"employee": "Ethan", "step": "Jira"}]
    assert _sent(hrms, "mark_onboarding_step_not_required") == [
        {"employee": "Ethan", "step": "jira", "reason": "internal project"},
        {"employee": "Ethan", "step": "jira", "reason": ""},
    ]
    assert _sent(hrms, "start_onboarding") == [{"employee": "Priya"}]


def test_a_change_returns_what_the_hrms_said(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["retry_onboarding_step"] = CHANGED
    result = call(client, "retry_onboarding_step", {"employee": "Ethan", "step": "setup email"})
    assert not result.get("isError")
    assert json.loads(text(result)) == CHANGED


def test_an_employee_is_told_only_hr_can(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    for tool in TOOLS:
        hrms.errors[tool] = "access denied: admin only"
    for tool in ("list_onboardings", "get_onboarding_status"):
        result = call(client, tool, {"employee": "Ethan"} if tool != "list_onboardings" else {})
        assert not result.get("isError"), tool  # an answer, which the reply can cite
        assert text(result) == NOT_HR_ADMIN, tool
    for tool, args in (
        ("retry_onboarding_step", {"employee": "Ethan", "step": "setup email"}),
        ("mark_onboarding_step_not_required", {"employee": "Ethan", "step": "jira"}),
        ("require_onboarding_step", {"employee": "Ethan", "step": "jira"}),
        ("start_onboarding", {"employee": "Ethan"}),
    ):
        result = call(client, tool, args)
        assert result["isError"], tool
        assert NOT_HR_ADMIN in text(result), tool


def test_the_hrms_refusals_reach_hr_in_its_words(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["retry_onboarding_step"] = "only a failed step can be retried"
    result = call(client, "retry_onboarding_step", {"employee": "Ethan", "step": "jira"})
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
    assert "One short line per person" in said["list_onboardings"]
    assert "An empty list means nobody is being onboarded" in said["list_onboardings"]
    assert "never show a `key` or an id" in said["get_onboarding_status"]
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
    for internal in ("setup_email", "slack_joined", "jira_access", "documents_uploaded",
                     "profile_confirmed", "orientation_scheduled", "first_login"):
        assert internal not in source, internal
