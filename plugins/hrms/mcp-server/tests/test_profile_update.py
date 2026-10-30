"""Asking for the profile form, against a fake HRMS: the form is sent in Slack and
the asker told so; when it cannot be sent they are told where else to go; no
personal detail is ever asked for or carried by the tool."""

from __future__ import annotations

from starlette.testclient import TestClient

from hrms_mcp import profile_update
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)


def test_the_form_sent_in_slack_is_said_with_the_bank_rule(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["start_profile_update"] = {"form": "sent"}
    result = call(client, "start_profile_update")
    assert not result.get("isError")
    said = text(result)
    assert "Update my profile" in said
    assert "straight to HR-Assist, not through this chat" in said
    assert "emailed to your company address" in said
    [sent] = [c for c in hrms.calls() if c["name"] == "start_profile_update"]
    assert sent["arguments"] == {}


def test_when_the_form_cannot_be_sent_the_asker_is_told_where_else(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-0042"}
    outcomes = (("not_in_slack", "aren't in the company Slack"), ("slack_off", "switched off"))
    for outcome, why in outcomes:
        hrms.results["start_profile_update"] = {"form": outcome}
        result = call(client, "start_profile_update")
        assert not result.get("isError")
        said = text(result)
        assert why in said
        assert "My Profile → Edit Profile" in said
        assert "Please don't type them into this chat" in said


def test_an_unknown_answer_or_a_failure_points_to_my_profile_not_a_claim_it_was_sent(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-0042"}
    for answer in ({"form": "maybe"}, {}):
        hrms.results["start_profile_update"] = answer
        result = call(client, "start_profile_update")
        assert not result.get("isError")
        assert "My Profile → Edit Profile" in text(result)
        assert "I've sent you" not in text(result)
    hrms.results.pop("start_profile_update")
    hrms.errors["start_profile_update"] = "slack api failed"
    result = call(client, "start_profile_update")
    assert not result.get("isError")
    assert "My Profile → Edit Profile" in text(result)


def test_the_tool_carries_no_person_and_sending_is_a_write(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    tool = tools["start_profile_update"]
    assert tool["annotations"]["readOnlyHint"] is False
    assert not tool["inputSchema"].get("properties")
    said = " ".join(tool["description"].split())
    # The guide that matters: never in the chat, always the form.
    assert "NEVER ask for, accept or repeat those details in the chat" in said
    assert "enter it in the form instead" in said
    assert profile_update.START_PROFILE_UPDATE.strip().startswith("Sends the asking employee")
