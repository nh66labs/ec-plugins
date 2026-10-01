"""The HRMS plugin's MCP server, against a fake HRMS that records what reached it.

The paths that matter most: the signed identity reaches the HRMS exactly as it
arrived; no tool asks for — or sends — an id naming the person; every schema is
one Enterprise Claw will bind; and nobody without the token gets anything.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from hrms_mcp.app import create_app
from hrms_mcp.config import Settings

EC_TOKEN = "ec-token-value"
IDENTITY = {"user_id": "emp-0042", "turn_id": "turn-1", "signature": "c0ffee"}
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class FakeHrms:
    """Answers as the HRMS's MCP endpoint and lookup do, and keeps every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.errors: dict[str, str] = {}
        self.results: dict[str, Any] = {
            "get_user_session": {
                "authenticated": True, "name": "Ravi", "role": "employee",
                "current_date": "2026-09-29", "current_day": "Tuesday",
                "timezone": "Asia/Kolkata",
                "this_week": {"monday": "2026-09-28", "thursday": "2026-10-01"},
                "next_week": {"monday": "2026-10-05", "tuesday": "2026-10-06"},
            },
            "get_employee_project_managers": {
                "count": 1, "project_managers": [{"id": "pm-1", "name": "Priya"}],
            },
            "preview_leave": {"effective_days": 1, "briefing": "No clashes.", "created": False},
            "apply_leave": {"leave_id": "new-1", "status": "Pending"},
            "get_team_leaves": [
                {"leave_id": "lv-9", "employee_id": "emp-7", "type": "Casual Leave",
                 "mode": "Full Day", "date_from": "2026-10-01", "date_to": "2026-10-01",
                 "status": "Pending", "reason": "family function"},
            ],
            "get_leave_routing": {"leave_id": "lv-9", "employee": "Ravi"},
            "approve_leave": {"action_taken": "approved"},
            "reject_leave": {"action_taken": "rejected"},
            "cancel_leave": {"success": True},
            "get_holidays": [
                {"date": "2026-10-02", "name": "Gandhi Jayanti", "type": "Public"},
                {"date": "2026-10-20", "name": "Dussehra", "type": "Public"},
            ],
            "get_leave_balance": [
                {"type": "Casual Leave", "total_quota": 12, "used": 2, "available": 10},
            ],
            "get_leaves": [
                {"id": "lv-1", "leave_type": "CL", "leave_mode": "FULL DAY",
                 "leave_date_from": "2026-10-01",
                 "leave_date_to": "2026-10-01", "status": "Pending", "reason": "family function"},
            ],
        }
        self.error: dict[str, Any] | None = None
        self.is_error_text = ""
        self.people = {"ravi@acme.test": {"saas_user_id": "emp-0042", "persona_id": "employee",
                                          "fields": {"phone": "private"}}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/lookup":
            if request.headers.get("Authorization") != "Bearer lookup-token":
                return httpx.Response(401)
            person = self.people.get(request.url.params.get("value", ""))
            return httpx.Response(200, json=person) if person else httpx.Response(404)
        if request.headers.get("X-API-Key") != "api-key":
            return httpx.Response(401, json={"error": {"code": -32001}})
        params = json.loads(request.content)["params"]
        name = params["name"]
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": 1}
        if name in self.errors:
            envelope["result"] = {"isError": True, "content": [{"text": self.errors[name]}]}
        elif name == "get_team_leaves":
            wanted = params["arguments"].get("status", "Pending")
            rows = [r for r in self.results[name] if r["status"] == wanted]
            envelope["result"] = {"content": [{"type": "text", "text": json.dumps(rows)}]}
        elif self.error:
            envelope["error"] = self.error
        elif self.is_error_text:
            envelope["result"] = {"isError": True, "content": [{"text": self.is_error_text}]}
        else:
            answer = self.results.get(name, [])
            # A result may depend on the arguments (a plan by its quarter).
            if callable(answer):
                answer = answer(params["arguments"])
            text = json.dumps(answer)
            envelope["result"] = {"content": [{"type": "text", "text": text}]}
        return httpx.Response(200, json=envelope)

    def calls(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)["params"] for r in self.requests if r.url.path.endswith("/mcp")
        ]


@pytest.fixture()
def hrms() -> FakeHrms:
    return FakeHrms()


@pytest.fixture()
def client(hrms: FakeHrms):
    settings = Settings(
        hrms_mcp_url="http://hrms.test/api/v1/mcp",
        hrms_api_key="api-key",
        hrms_lookup_url="http://hrms.test/lookup",
        hrms_lookup_token="lookup-token",
        ec_token=EC_TOKEN,
        _env_file=None,
    )
    app = create_app(settings, hrms_transport=httpx.MockTransport(hrms.handler))
    with TestClient(app, base_url="http://hrms-mcp:8000") as test_client:
        yield test_client


def rpc(client: TestClient, method: str, params: dict | None = None, token: str = EC_TOKEN):
    return client.post(
        "/mcp",
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def call(client: TestClient, name: str, arguments: dict | None = None, identity=IDENTITY):
    params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
    if identity is not None:
        params["_identity"] = identity
    return rpc(client, "tools/call", params).json()["result"]


def text(result: dict) -> str:
    return "".join(part.get("text", "") for part in result["content"])


# --- what Enterprise Claw sees ---------------------------------------------------


def _bindable(node: Any, path: str = "arguments") -> list[str]:
    """The platform's schema rules: one scalar type per node, no unions, no refs."""
    problems = []
    if isinstance(node, dict):
        for word in ("$ref", "anyOf", "oneOf", "allOf"):
            if word in node:
                problems.append(f"{path} uses {word}")
        if "type" in node and not isinstance(node["type"], str):
            problems.append(f"{path} has several types")
        for name, child in (node.get("properties") or {}).items():
            if "type" not in child:
                problems.append(f"{path}.{name} has no type")
            problems += _bindable(child, f"{path}.{name}")
    return problems


READS = {
    "get_holidays", "get_my_leave_balance", "list_my_leaves", "get_leave_context",
    "preview_leave", "list_leave_requests_to_decide", "start_leave_request",
    "get_leave_request_impact",
    "get_my_mbo_context", "get_my_mbo_plan",
}
WRITES = {"apply_leave", "cancel_my_leave", "approve_leave", "reject_leave", "save_my_mbo_plan"}


def test_every_tool_is_bindable_says_whether_it_writes_and_names_no_person(
    client: TestClient,
) -> None:
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == READS | WRITES
    for tool in tools:
        assert _bindable(tool["inputSchema"]) == [], tool["name"]
        # A platform that confirms writes must be able to tell them apart.
        assert tool["annotations"]["readOnlyHint"] is (tool["name"] in READS), tool["name"]
        assert not {"employee_id", "approver_id", "caller_id"} & set(
            tool["inputSchema"].get("properties", {})
        )


def test_a_rejection_cannot_be_sent_without_a_reason(client: TestClient) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "reason" in tools["reject_leave"]["inputSchema"]["required"]
    assert "reason" in tools["apply_leave"]["inputSchema"]["required"]
    assert "day_portion" in tools["apply_leave"]["inputSchema"]["required"]


def test_the_handshake_carries_the_instructions(client: TestClient) -> None:
    result = rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "enterprise-claw", "version": "2"}},
    ).json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    said = result["instructions"]
    assert "never ask for their employee id" in said
    assert "Call start_leave_request" in said
    assert "do not ask again" in said


# --- only Enterprise Claw ------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "wrong"])
def test_without_the_token_nothing_is_served(
    client: TestClient, hrms: FakeHrms, token: str
) -> None:
    assert rpc(client, "tools/list", token=token).status_code == 401
    response = client.get(
        "/people/lookup", params={"value": "ravi@acme.test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert hrms.requests == []


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health").text == "ok"


# --- calls, as the person asking --------------------------------------------------------


def test_a_call_needs_no_session(client: TestClient) -> None:
    """Enterprise Claw sends each call on its own, with no initialize first."""
    assert "Gandhi Jayanti" in text(call(client, "get_holidays", {"year": 2026, "month": 10}))


def test_the_identity_reaches_the_hrms_unchanged_with_the_api_key(
    client: TestClient, hrms: FakeHrms
) -> None:
    call(client, "get_my_leave_balance")
    [sent] = hrms.calls()
    assert sent["_identity"] == IDENTITY
    assert sent["name"] == "get_leave_balance"
    assert "employee_id" not in sent["arguments"]
    assert hrms.requests[0].headers["X-API-Key"] == "api-key"


def test_the_arguments_are_translated_for_the_hrms(client: TestClient, hrms: FakeHrms) -> None:
    call(client, "get_holidays", {"year": 2026, "month": 10})
    call(client, "get_holidays", {"year": 2026})
    call(client, "list_my_leaves", {"status": "Pending"})
    holidays_month, holidays_year, leaves = hrms.calls()
    assert holidays_month["arguments"] == {"year": "2026", "month": "10"}
    assert holidays_year["arguments"] == {"year": "2026"}
    assert leaves == {**leaves, "name": "get_leaves", "arguments": {"status": "Pending"}}


def test_answers_are_short_lines(client: TestClient) -> None:
    assert text(call(client, "get_holidays", {"year": 2026, "month": 10})) == (
        "Fri 2 Oct 2026 — Gandhi Jayanti (public)\nTue 20 Oct 2026 — Dussehra (public)"
    )
    assert text(call(client, "get_my_leave_balance")) == (
        "Casual Leave: 10 of 12 days available (2 used)"
    )
    assert text(call(client, "list_my_leaves")) == (
        "Casual leave, Thu 1 Oct 2026, full day — Pending: family function (request lv-1)"
    )


def test_no_identity_reaches_no_hrms(client: TestClient, hrms: FakeHrms) -> None:
    result = call(client, "get_my_leave_balance", identity=None)
    assert result["isError"] is True
    assert "Tell the server who is asking" in text(result)
    assert hrms.requests == []


def test_an_identity_the_hrms_refuses_is_said_plainly(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.error = {"code": -32001, "message": "identity verification failed"}
    result = call(client, "get_my_leave_balance")
    assert result["isError"] is True
    assert "did not accept who is asking" in text(result)


def test_a_refusal_inside_a_success_is_a_refusal(client: TestClient, hrms: FakeHrms) -> None:
    hrms.is_error_text = "employee not found"
    result = call(client, "list_my_leaves")
    assert result["isError"] is True
    assert "employee not found" in text(result)


def test_an_unreachable_hrms_is_said_plainly(client: TestClient, hrms: FakeHrms) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    hrms.handler = down  # type: ignore[method-assign]
    settings = Settings(
        hrms_mcp_url="http://hrms.test/api/v1/mcp", hrms_api_key="api-key",
        hrms_lookup_url="http://hrms.test/lookup", hrms_lookup_token="lookup-token",
        ec_token=EC_TOKEN, _env_file=None,
    )
    app = create_app(settings, hrms_transport=httpx.MockTransport(down))
    with TestClient(app, base_url="http://hrms-mcp:8000") as down_client:
        result = call(down_client, "get_my_leave_balance")
    assert result["isError"] is True
    assert "could not be reached" in text(result)


def test_a_month_out_of_range_is_refused_before_the_hrms(
    client: TestClient, hrms: FakeHrms
) -> None:
    result = call(client, "get_holidays", {"year": 2026, "month": 13})
    assert result["isError"] is True
    assert hrms.requests == []


def test_arguments_and_identity_never_reach_the_log(
    client: TestClient, hrms: FakeHrms, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="hrms_mcp")
    call(client, "list_my_leaves", {"status": "Pending"})
    hrms.is_error_text = "no such leave"
    call(client, "list_my_leaves")
    logged = caplog.text
    assert "hrms get_leaves answered" in logged
    for secret in ("c0ffee", "emp-0042", "Pending", "family function"):
        assert secret not in logged


# --- the lookup ---------------------------------------------------------------------


def test_the_lookup_forwards_with_the_hrms_token_and_returns_two_fields(
    client: TestClient, hrms: FakeHrms
) -> None:
    response = client.get(
        "/people/lookup", params={"value": "ravi@acme.test"},
        headers={"Authorization": f"Bearer {EC_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"saas_user_id": "emp-0042", "persona_id": "employee"}
    assert hrms.requests[0].headers["Authorization"] == "Bearer lookup-token"


def test_an_unknown_person_is_not_found(client: TestClient) -> None:
    response = client.get(
        "/people/lookup", params={"value": "stranger@acme.test"},
        headers={"Authorization": f"Bearer {EC_TOKEN}"},
    )
    assert response.status_code == 404


# --- leave context and preview --------------------------------------------------------


def test_the_context_gives_dates_balance_and_approver_without_asking(
    client: TestClient,
) -> None:
    said = text(call(client, "get_leave_context"))
    assert "You are Ravi" in said
    assert "Today is Tuesday 2026-09-29, in Asia/Kolkata." in said
    assert "Thursday 2026-10-01" in said and "Tuesday 2026-10-06" in said
    assert "Gandhi Jayanti" in said
    assert "Casual Leave 10 of 12 days left" in said
    assert "Approver: Priya" in said


def test_several_managers_are_named_so_the_assistant_asks_only_then(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    assert "Ask which one should approve" in text(call(client, "get_leave_context"))


def test_preview_files_nothing_and_says_so(client: TestClient, hrms: FakeHrms) -> None:
    said = text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-01", "day_portion": "Full Day",
    }))
    assert said.startswith("Casual leave, Thu 1 Oct 2026, full day: counts as 1 day.")
    assert "Nothing has been filed." in said
    assert [c["name"] for c in hrms.calls()] == ["preview_leave", "get_employee_project_managers"]
    assert hrms.calls()[0]["arguments"] == {
        "leave_type": "Casual Leave", "leave_mode": "Full Day",
        "date_from": "2026-10-01", "date_to": "2026-10-01",
    }


def _overlap(client: TestClient, hrms: FakeHrms, names: list[str]) -> str:
    hrms.results["preview_leave"] = {
        "effective_days": 1,
        "created": False,
        "facts": {"team_leaves": {"team_size": 4, "overlap_count": len(names),
                                  "on_leave_names": names}},
        "briefing": [
            "Calendar: no meetings that day.",
            *([f"Team: {len(names)} teammate(s) on leave that day: {names}."] if names else []),
            "Balance: 10.0 Casual Leave day(s) available; this request uses 1.0.",
        ],
    }
    return text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    }))


def test_a_teammate_already_off_is_warned_of_first(client: TestClient, hrms: FakeHrms) -> None:
    said = _overlap(client, hrms, ["Anu"])
    assert said.splitlines()[0] == (
        "Heads-up: Anu has already applied for leave on Fri 2 Oct 2026, so it may be "
        "difficult to approve. Do you still want to apply?"
    )
    assert "Team:" not in said  # said once, not twice
    assert "Calendar: no meetings that day." in said and "Balance: 10.0" in said
    assert "Nothing has been filed." in said


def test_no_teammate_off_means_no_warning(client: TestClient, hrms: FakeHrms) -> None:
    said = _overlap(client, hrms, [])
    assert "Heads-up" not in said
    assert said.startswith("Casual leave, Fri 2 Oct 2026, full day")


def test_several_teammates_are_all_named(client: TestClient, hrms: FakeHrms) -> None:
    assert "Anu and Bala have already applied" in _overlap(client, hrms, ["Anu", "Bala"])
    three = _overlap(client, hrms, ["Anu", "Bala", "Chitra"])
    assert "3 teammates (Anu, Bala and Chitra) have" in three


def test_the_instructions_say_a_warning_is_not_a_refusal() -> None:
    from hrms_mcp.tools import INSTRUCTIONS

    said = " ".join(INSTRUCTIONS.split())
    assert "it is a warning, not a refusal" in said


# --- applying ---------------------------------------------------------------------------


APPLY = {
    "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    "reason": "family function",
}


def test_apply_files_the_request_and_says_who_approves(client: TestClient, hrms: FakeHrms) -> None:
    said = text(call(client, "apply_leave", APPLY))
    assert said == (
        "Filed: Casual leave, Fri 2 Oct 2026, full day — reason: family function. "
        "Status: Pending. It is waiting for Priya to approve."
    )
    [filed] = [c for c in hrms.calls() if c["name"] == "apply_leave"]
    assert filed["arguments"] == {
        "leave_type": "Casual Leave", "leave_mode": "Full Day", "date_from": "2026-10-02",
        "date_to": "2026-10-02", "reason": "family function",
    }
    assert filed["_identity"] == IDENTITY


def test_applying_twice_files_once(client: TestClient, hrms: FakeHrms) -> None:
    """A confirmed action may be delivered again; the HRMS would file a second
    request, so the server looks first."""
    already = {**APPLY, "date_from": "2026-10-01"}
    said = text(call(client, "apply_leave", already))
    assert said.startswith("Already filed: Casual leave, Thu 1 Oct 2026, full day — Pending.")
    assert "apply_leave" not in [c["name"] for c in hrms.calls()]


def test_the_named_approver_is_sent_by_id(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    call(client, "apply_leave", {**APPLY, "approver": "arun"})
    [filed] = [c for c in hrms.calls() if c["name"] == "apply_leave"]
    assert filed["arguments"]["project_manager_id"] == "b"
    refused = call(client, "apply_leave", {**APPLY, "approver": "Maya"})
    assert refused["isError"] is True and "exactly one of their project managers" in text(refused)


def test_several_managers_and_no_approver_files_nothing(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    refused = call(client, "apply_leave", APPLY)
    assert refused["isError"] is True and "Priya, Arun" in text(refused)
    assert "apply_leave" not in [c["name"] for c in hrms.calls()]


def test_an_approver_is_matched_exactly_before_by_part_of_a_name() -> None:
    from hrms_mcp.tools import manager_named

    managers = [{"id": "k", "name": "Karuna"}, {"id": "a", "name": "Arun"}]
    assert manager_named(managers, "Arun") == {"id": "a", "name": "Arun"}, "not Karuna"
    assert manager_named(managers, "karu") == {"id": "k", "name": "Karuna"}
    assert manager_named(managers, "a") is None, "both contain it, so ask"
    assert manager_named(managers, "  ") is None


def test_floating_leave_is_known_by_its_code() -> None:
    from hrms_mcp.tools import same_kind

    assert same_kind("FL", "Floating Leave")
    assert not same_kind("FL", "Casual Leave")


def test_a_request_without_a_reason_is_refused(client: TestClient, hrms: FakeHrms) -> None:
    refused = call(client, "apply_leave", {**APPLY, "reason": "  "})
    assert refused["isError"] is True and "reason is needed" in text(refused)
    assert hrms.calls() == []


# --- cancelling ------------------------------------------------------------------------------


def test_cancel_withdraws_ones_own_request(client: TestClient, hrms: FakeHrms) -> None:
    assert text(call(client, "cancel_my_leave", {"request_id": "lv-1"})) == (
        "Cancelled: Casual leave, Thu 1 Oct 2026."
    )
    refused = call(client, "cancel_my_leave", {"request_id": "someone-elses"})
    assert refused["isError"] is True
    assert [c["name"] for c in hrms.calls()].count("cancel_leave") == 1


# --- deciding -----------------------------------------------------------------------------------


def test_the_list_to_decide_names_who_and_keeps_the_id_for_the_assistant(
    client: TestClient,
) -> None:
    said = text(call(client, "list_leave_requests_to_decide"))
    assert said == (
        "Ravi — Casual leave, Thu 1 Oct 2026, full day, Pending. "
        "Reason: family function. (request lv-9)"
    )


def test_someone_who_manages_nobody_is_told_so(client: TestClient, hrms: FakeHrms) -> None:
    hrms.errors["get_team_leaves"] = "access denied: only project managers and admins can view"
    said = text(call(client, "list_leave_requests_to_decide"))
    assert "only project managers and HR can approve or reject" in said


def test_reject_sends_the_reason_and_names_the_request(client: TestClient, hrms: FakeHrms) -> None:
    said = text(call(client, "reject_leave", {
        "request_id": "lv-9", "employee": "Ravi", "reason": "Pending deployment.",
    }))
    assert said == (
        "Rejected Ravi's casual leave for Thu 1 Oct 2026, full day. "
        "Reason given: Pending deployment. The HRMS tells Ravi."
    )
    [sent] = [c for c in hrms.calls() if c["name"] == "reject_leave"]
    assert sent["arguments"] == {"leave_id": "lv-9", "reason": "Pending deployment"}


def test_a_decision_naming_the_wrong_person_is_refused(client: TestClient, hrms: FakeHrms) -> None:
    """The card shows the employee's name beside the request; the two must agree."""
    refused = call(client, "approve_leave", {"request_id": "lv-9", "employee": "Arun"})
    assert refused["isError"] is True and "Ravi's, not Arun's" in text(refused)
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_a_name_that_only_contains_the_employees_is_refused(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_leave_routing"] = {"leave_id": "lv-9", "employee": "Ravindra"}
    refused = call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"})
    assert refused["isError"] is True and "Ravindra's, not Ravi's" in text(refused)
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_a_request_the_hrms_names_no_one_for_is_not_decided(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_leave_routing"] = {"leave_id": "lv-9"}
    refused = call(client, "approve_leave", {"request_id": "lv-9", "employee": "Someone"})
    assert refused["isError"] is True and "did not say whose" in text(refused)
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_a_rejected_request_says_why_to_its_employee(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_leaves"][0].update(status="Rejected", notes="pending deployment")
    assert text(call(client, "list_my_leaves")) == (
        "Casual leave, Thu 1 Oct 2026, full day — Rejected: family function"
        " — rejected because: pending deployment (request lv-1)"
    )


def test_a_decision_on_a_request_not_waiting_on_you_is_refused(
    client: TestClient, hrms: FakeHrms
) -> None:
    refused = call(client, "approve_leave", {"request_id": "not-mine", "employee": "Ravi"})
    assert refused["isError"] is True and "not a leave request waiting on" in text(refused)
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_deciding_twice_reports_it_rather_than_doing_it_again(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0]["status"] = "Approved"
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == (
        "Already approved: Ravi's casual leave for Thu 1 Oct 2026, full day. Nothing changed."
    )
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


# --- starting a request: the missing things as choices (EC-D184) ------------------------


def _form(result: dict) -> dict:
    return result["_meta"]["ec/form"]


def test_what_is_missing_comes_back_as_choices_from_the_hrms(client: TestClient) -> None:
    result = call(client, "start_leave_request", {"date_from": "2026-09-30"})
    form = _form(result)
    assert form["action"] == {
        "tool": "apply_leave",
        "arguments": {"date_from": "2026-09-30", "date_to": "2026-09-30"},
    }
    assert form["check"] == {"tool": "preview_leave"}
    names = [q["name"] for q in form["questions"]]
    assert names == ["leave_type", "day_portion", "reason"], "one PM, so no approver question"
    leave_type = form["questions"][0]
    assert leave_type["options"][0] == {"value": "Casual Leave", "label": "Casual Leave · 10 left"}
    assert form["prompt"] == "Leave for Wed 30 Sep 2026 — choose the rest:"
    assert "do not call apply_leave" in text(result)


def test_what_the_person_said_is_not_asked_again(client: TestClient) -> None:
    form = _form(call(client, "start_leave_request", {
        "date_from": "2026-09-30", "day_portion": "second half", "reason": "doctor's appointment",
    }))
    assert [q["name"] for q in form["questions"]] == ["leave_type"]
    assert form["action"]["arguments"] == {
        "date_from": "2026-09-30", "date_to": "2026-09-30",
        "day_portion": "Second Half", "reason": "doctor's appointment",
    }


def test_a_complete_request_asks_nothing(client: TestClient) -> None:
    form = _form(call(client, "start_leave_request", {
        "date_from": "2026-10-01", "leave_type": "casual leave", "day_portion": "Full Day",
        "reason": "family function",
    }))
    assert form["questions"] == []
    assert form["prompt"] == "Casual leave for Thu 1 Oct 2026, full day."


def test_several_managers_are_a_choice(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    form = _form(call(client, "start_leave_request", {"date_from": "2026-09-30"}))
    approver = next(q for q in form["questions"] if q["name"] == "approver")
    assert [o["value"] for o in approver["options"]] == ["Priya", "Arun"]


def test_no_date_is_for_the_assistant_to_ask(client: TestClient) -> None:
    result = call(client, "start_leave_request", {"date_from": ""})
    assert result["isError"] is True and "start date" in text(result)


def test_the_instructions_ask_for_every_answer_to_cite_its_result() -> None:
    """A list of holidays written without [1] is withheld as uncited — found
    live, three times in three."""
    from hrms_mcp.tools import INSTRUCTIONS

    said = " ".join(INSTRUCTIONS.split())
    assert "cites the result it came from by its number, like [1]" in said
    assert "a list of holidays or balances too" in said
