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
    "get_leave_request_impact", "get_leave_trend", "get_leave_calendar",
    "get_my_mbo_context", "get_my_mbo_plan",
    "start_wfh_request", "preview_wfh", "start_comp_off_request", "preview_comp_off",
    "get_new_employee_options", "check_new_employee", "where_to_upload_documents",
    "get_my_onboarding", "list_orientation_requests",
    "list_onboardings", "get_onboarding_status",
}
WRITES = {
    "apply_leave", "cancel_my_leave", "approve_leave", "reject_leave", "save_my_mbo_plan",
    "apply_wfh", "apply_comp_off", "create_employee", "confirm_my_profile",
    "confirm_orientation",
    "retry_onboarding_step", "mark_onboarding_step_not_required", "require_onboarding_step",
    "start_onboarding",
}


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
    assert "Never ask about projects, managers or approvers" in " ".join(said.split())


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
    assert "Gandhi Jayanti (public)" in said  # an optional one is not a day off
    assert "Casual Leave 10 of 12 days left" in said
    assert "Priya will approve it." in said


def test_several_managers_are_all_named_as_approvers(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    said = text(call(client, "get_leave_context"))
    assert (
        "It goes to Priya and Arun; all of them must approve it, and one rejection rejects it."
    ) in said
    assert "which of them" not in said and "?" not in said  # never a choice


def test_preview_files_nothing_and_says_so(client: TestClient, hrms: FakeHrms) -> None:
    said = text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-01", "day_portion": "Full Day",
    }))
    assert said.startswith("Casual leave on Thu 1 Oct 2026, full day — 1 day.")
    assert "Nothing is filed until you confirm." in said
    # Reads only: the check, its approvers, and Jira through the HRMS.
    assert {c["name"] for c in hrms.calls()} == {
        "preview_leave", "get_employee_project_managers", "get_user_session", "get_jira_work",
    }
    [checked] = [c for c in hrms.calls() if c["name"] == "preview_leave"]
    assert checked["arguments"] == {
        "leave_type": "Casual Leave", "leave_mode": "Full Day",
        "date_from": "2026-10-01", "date_to": "2026-10-01",
    }


def test_the_person_is_shown_their_open_tickets_as_their_manager_will_see_them(
    client: TestClient, hrms: FakeHrms
) -> None:
    pending = (
        "*Pending in Jira (2)* — open tickets assigned to Ravi:\n"
        "• *ECP-4* Payments sync — In Progress, due Oct 02 — during this leave\n"
        "• *ECP-9* Ledger export — To Do"
    )
    hrms.results["preview_leave"] = {
        "effective_days": 1, "created": False, "briefing": [], "pending_jira": pending,
    }
    said = text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    }))
    assert pending in said


def test_no_pending_list_is_said_when_the_hrms_sends_none(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": 1, "created": False, "briefing": [], "pending_jira": "",
    }
    said = text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    }))
    assert "Pending in Jira" not in said


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
        "Heads-up: Anu has already applied for leave on the same day. "
        "Do you still want to proceed?"
    )
    assert "Team:" not in said  # said once, not twice
    assert "Calendar: no meetings that day." in said and "Balance: 10.0" in said
    assert "Nothing is filed until you confirm." in said


def _two_managers(hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2,
        "project_managers": [{"id": "a", "name": "Abhijith K K"},
                             {"id": "b", "name": "Muhammed Nizar K"}],
    }


def _check(client: TestClient, **more: str) -> str:
    return text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-09", "day_portion": "Full Day",
        **more,
    }))


def test_the_check_speaks_to_the_person_in_plain_sentences(
    client: TestClient, hrms: FakeHrms
) -> None:
    _two_managers(hrms)
    hrms.results["preview_leave"] = {
        "effective_days": 1,
        "facts": {"balance": {"leave_type": "Casual Leave", "available": 5.0, "requested": 1.0}},
        "briefing": ["Balance: 5.0 Casual Leave day(s) available; this request uses 1.0."],
    }
    assert _check(client).splitlines() == [
        "Casual leave on Fri 9 Oct 2026, full day — 1 day.",
        "You have 5 days of casual leave left — 4 days after this one.",
        "",
        "It goes to Abhijith K K and Muhammed Nizar K; all of them must approve it, "
        "and one rejection rejects it.",
        "Nothing is filed until you confirm.",
    ]


def test_the_check_never_waits_for_an_approver_to_be_chosen(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 3,
        "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"},
                             {"id": "c", "name": "Maya"}],
    }
    for older in ({}, {"approver": "Arun"}, {"approver": "Nobody"}):  # an older card's
        said = _check(client, **older).splitlines()
        assert said[-2:] == [
            "It goes to Priya, Arun and Maya; all of them must approve it, "
            "and one rejection rejects it.",
            "Nothing is filed until you confirm.",
        ]
        assert not any("chosen" in line or "?" in line for line in said)
    assert all("approver" not in c["arguments"] for c in hrms.calls())  # never sent


def test_the_hrms_balance_line_goes_however_it_words_the_same(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": 1,
        "facts": {"balance": {"leave_type": "Casual Leave", "available": 5.0, "requested": 1.0}},
        "briefing": ["Balance: 5 Work-from-home days available; this request uses 1 day."],
    }
    assert "Balance:" not in _check(client)


def test_the_check_words_half_days_ranges_and_a_short_balance(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": 0.5,
        "facts": {"balance": {"leave_type": "Sick Leave", "available": 0.0, "requested": 0.5}},
    }
    said = text(call(client, "preview_leave", {
        "leave_type": "Sick Leave", "date_from": "2026-10-05", "date_to": "2026-10-06",
        "day_portion": "First Half",
    }))
    assert said.startswith(
        "Heads-up: You have no sick leave left, and this needs half a day. "
        "Do you still want to proceed?\n\n"
        "Sick leave from Mon 5 Oct 2026 to Tue 6 Oct 2026, first half — half a day."
    )


def test_a_balance_short_by_some_says_by_how_much(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": 3,
        "facts": {"balance": {"leave_type": "Casual Leave", "available": 1.0, "requested": 3.0}},
    }
    assert _check(client).startswith(
        "Heads-up: You have 1 day of casual leave left, and this needs 3 days — "
        "2 days more than you have. Do you still want to proceed?"
    )
    hrms.results["preview_leave"]["facts"]["balance"]["available"] = 0.5
    assert "You have half a day of casual leave left" in _check(client)


def test_the_hrms_balance_line_stays_when_it_says_more(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": 1,
        "facts": {"balance": {"leave_type": "Sick Leave", "available": 0.0, "requested": 1.0}},
        "briefing": [
            "Balance: 0.0 Sick Leave day(s) available; the excess will be marked LOP.",
        ],
    }
    assert "the excess will be marked LOP" in _check(client)


def test_a_boolean_from_the_hrms_is_not_a_number(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["preview_leave"] = {
        "effective_days": True,
        "facts": {"balance": {"leave_type": "Casual Leave", "available": True, "requested": 1}},
    }
    said = _check(client)
    assert said.startswith("Casual leave on Fri 9 Oct 2026, full day.")
    assert "left" not in said


def test_a_manager_with_no_name_still_approves(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 1, "project_managers": [{"id": "a", "name": ""}],
    }
    said = _check(client)
    assert "Your project manager will approve it." in said
    assert "Nothing is filed until you confirm." in said


def test_no_teammate_off_means_no_warning(client: TestClient, hrms: FakeHrms) -> None:
    said = _overlap(client, hrms, [])
    assert "Heads-up" not in said
    assert said.startswith("Casual leave on Fri 2 Oct 2026, full day")


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


def test_a_managers_lookup_that_fails_never_stops_the_filing(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.errors["get_employee_project_managers"] = "The HRMS could not list managers."
    said = text(call(client, "apply_leave", APPLY))
    assert said == (
        "Filed: Casual leave, Fri 2 Oct 2026, full day — reason: family function. "
        "Status: Pending."
    )
    assert "apply_leave" in [c["name"] for c in hrms.calls()]


def test_a_pending_vote_is_not_mistaken_for_one_already_cast(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Pending"), ("Arun", "Pending")])
    )
    hrms.results["approve_leave"] = {"status": "Pending", "message": "Leave approved."}
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert not said.startswith("You already approved")


def test_a_malformed_managers_answer_never_fails_the_filing(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_employee_project_managers"] = ["not", "a", "record"]
    said = text(call(client, "apply_leave", APPLY))
    assert said.startswith("Filed: Casual leave")
    assert said.endswith("Status: Pending.")


def test_applying_twice_files_once(client: TestClient, hrms: FakeHrms) -> None:
    """A confirmed action may be delivered again; the HRMS would file a second
    request, so the server looks first."""
    already = {**APPLY, "date_from": "2026-10-01"}
    said = text(call(client, "apply_leave", already))
    assert said.startswith("Already filed: Casual leave, Thu 1 Oct 2026, full day — Pending.")
    assert "apply_leave" not in [c["name"] for c in hrms.calls()]


def test_several_managers_are_all_waited_for_and_none_is_sent(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 3,
        "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"},
                             {"id": "c", "name": "Maya"}],
    }
    for older in ({}, {"approver": "Arun"}, {"approver": "Nobody"}):  # an older card's
        said = text(call(client, "apply_leave", {**APPLY, **older}))
        assert said.endswith(
            "Status: Pending. It is waiting for Priya, Arun and Maya — it is approved once "
            "all of them approve, and rejected if any of them rejects."
        )
    filed = [c["arguments"] for c in hrms.calls() if c["name"] == "apply_leave"]
    assert len(filed) == 3
    assert not any({"project_manager_id", "approver"} & set(f) for f in filed)


def test_with_no_manager_hr_decides(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {"count": 0, "project_managers": []}
    assert text(call(client, "apply_leave", APPLY)).endswith(
        "Status: Pending. You have no project manager on record, so HR will decide it."
    )


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


def _every_one(approvals: list[tuple[str, str]], **more: Any) -> dict[str, Any]:
    """What the HRMS adds to a request that every project manager must approve."""
    return {
        "approval_rule": "all",
        "approvals": [
            {"approver_id": f"pm-{n}", "approver_name": name, "decision": decision}
            for n, (name, decision) in enumerate(approvals)
        ],
        "approvals_required": len(approvals),
        "approvals_done": sum(d == "Approved" for _, d in approvals),
        **more,
    }


def test_the_employee_is_told_who_has_approved_and_who_not_yet(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending"), ("Maya", "Pending")])
    )
    assert text(call(client, "list_my_leaves")) == (
        "Casual leave, Thu 1 Oct 2026, full day — Pending: family function"
        " — approved by Priya; waiting for Arun and Maya (request lv-1)"
    )
    hrms.results["get_leaves"][0].update(approval_rule="either")  # first decision is final
    assert "waiting for" not in text(call(client, "list_my_leaves"))


def test_a_manager_who_approved_is_told_who_is_left(client: TestClient, hrms: FakeHrms) -> None:
    team = hrms.results["get_team_leaves"][0]
    votes = [("Priya", "Approved"), ("Arun", "Pending"), ("Maya", "Pending")]
    team.update(_every_one(votes))
    assert text(call(client, "list_leave_requests_to_decide")) == (
        "Ravi — Casual leave, Thu 1 Oct 2026, full day, Pending. "
        "Reason: family function. Approved by Priya; waiting for Arun and Maya. (request lv-9)"
    )
    team.update(_every_one(votes, my_decision="Approved"))
    assert "Reason: family function. You approved; waiting for Arun and Maya. (request" in text(
        call(client, "list_leave_requests_to_decide")
    )


def test_a_manager_still_to_decide_is_called_you(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_user_session"]["name"] = "Arun"
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending"), ("Maya", "Pending")])
    )
    assert "Approved by Priya; waiting for you and Maya. (request" in text(
        call(client, "list_leave_requests_to_decide")
    )


def test_an_approval_that_others_still_wait_on_is_said_as_recorded(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending"), ("Maya", "Pending")])
    )
    hrms.results["approve_leave"] = {
        "final": False, "status": "Pending", "approved_by": ["Priya", "Arun"],
        "waiting_for": ["Maya"], "action_taken": "approval_recorded",
    }
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == (
        "Your approval of Ravi's casual leave for Thu 1 Oct 2026, full day is recorded. "
        "It is approved once every project manager approves; still waiting for Maya."
    )


def test_the_last_approval_approves_it(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Approved"), ("Maya", "Pending")])
    )
    hrms.results["approve_leave"] = {
        "final": True, "status": "Approved", "approved_by": ["Priya", "Arun", "Maya"],
        "waiting_for": [], "action_taken": "approved",
    }
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == "Approved Ravi's casual leave for Thu 1 Oct 2026, full day. The HRMS tells Ravi."


def test_approving_twice_as_one_of_several_votes_once(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending")], my_decision="Approved")
    )
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == (
        "You already approved Ravi's casual leave for Thu 1 Oct 2026, full day; "
        "still waiting for Arun. A vote cannot be changed. Nothing changed."
    )
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_a_manager_who_approved_cannot_then_reject(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending")], my_decision="Approved")
    )
    said = text(call(client, "reject_leave", {
        "request_id": "lv-9", "employee": "Ravi", "reason": "release week",
    }))
    assert said.startswith("You already approved Ravi's casual leave")
    assert "A vote cannot be changed. Nothing changed." in said
    assert "reject_leave" not in [c["name"] for c in hrms.calls()]


def test_a_withdrawn_approver_is_told_they_no_longer_decide(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Withdrawn"), ("Arun", "Pending")], my_decision="Withdrawn")
    )
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == (
        "You are no longer an approver of Ravi's casual leave for Thu 1 Oct 2026, "
        "full day. Nothing changed."
    )
    assert "approve_leave" not in [c["name"] for c in hrms.calls()]


def test_a_rejection_the_hrms_already_had_is_not_said_as_new(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending")])
    )
    hrms.results["reject_leave"] = {"action_taken": "already_decided", "status": "Pending"}
    said = text(call(client, "reject_leave", {
        "request_id": "lv-9", "employee": "Ravi", "reason": "release week",
    }))
    assert not said.startswith("Rejected")
    assert said.endswith("Nothing changed.")


def test_a_vote_the_hrms_already_had_is_done_not_failed(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending")])
    )
    hrms.results["approve_leave"] = {
        "action_taken": "already_decided",
        "status": "Pending",
        "message": "You have already decided this leave request; it is waiting for the other "
        "approvers. Nothing changed.",
    }
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    # Read again, the request shows no vote of theirs: say it was decided,
    # never that they approved it.
    assert said == (
        "Ravi's casual leave for Thu 1 Oct 2026, full day was already decided. Nothing changed."
    )


def test_an_answer_that_is_not_a_record_still_reads_as_approved(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["approve_leave"] = "ok"
    said = text(call(client, "approve_leave", {"request_id": "lv-9", "employee": "Ravi"}))
    assert said == "Approved Ravi's casual leave for Thu 1 Oct 2026, full day. The HRMS tells Ravi."


def test_a_rejection_tells_the_other_approvers_too(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_team_leaves"][0].update(
        _every_one([("Priya", "Approved"), ("Arun", "Pending")])
    )
    said = text(call(client, "reject_leave", {
        "request_id": "lv-9", "employee": "Ravi", "reason": "Pending deployment.",
    }))
    assert said.endswith("The HRMS tells Ravi and the other approvers.")


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
    assert names == ["leave_type", "day_portion", "reason"]
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


def test_several_managers_are_never_a_choice(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_employee_project_managers"] = {
        "count": 2, "project_managers": [{"id": "a", "name": "Priya"}, {"id": "b", "name": "Arun"}],
    }
    form = _form(call(client, "start_leave_request", {"date_from": "2026-09-30"}))
    assert [q["name"] for q in form["questions"]] == ["leave_type", "day_portion", "reason"]
    complete = _form(call(client, "start_leave_request", {
        "date_from": "2026-10-01", "leave_type": "Casual Leave", "day_portion": "Full Day",
        "reason": "family function", "approver": "Arun",  # an older call's; ignored
    }))
    assert complete["questions"] == []
    assert "approver" not in complete["action"]["arguments"]


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


# --- an HRMS admin asks about someone's leave trend ---------------------------------

ANU = {"id": "emp-7", "name": "Anu Thomas", "employee_id": "H032",
       "company_email": "anu@acme.test"}
ANUSREE = {"id": "emp-8", "name": "Anusree P", "employee_id": "H101",
           "company_email": "anusree@acme.test"}


def _as(hrms: FakeHrms, role: str) -> None:
    hrms.results["get_user_session"] = {
        **hrms.results["get_user_session"], "role": role, "employee_id": "emp-0042",
        "employee_code": "H001", "company_email": "ravi@acme.test",
        "current_date": "2026-10-13",
    }


def _anus_leave(hrms: FakeHrms, owner: str = "emp-7") -> None:
    hrms.results["resolve_employee"] = [ANU]
    hrms.results["get_holidays"] = [{"date": "2026-10-01", "name": "Holiday", "type": "Public"}]
    hrms.results["get_leave_balance"] = [
        {"type": "Casual Leave", "total_quota": 12, "used": 8, "available": 4},
    ]
    hrms.results["get_leaves"] = [
        {"id": f"lv-{n}", "employee_id": owner, "leave_type": kind, "leave_mode": "Full Day",
         "leave_date_from": day, "leave_date_to": day, "status": "Approved", "value": 1,
         "reason": "hospital visit"}
        for n, (kind, day) in enumerate([
            ("Sick Leave", "2026-06-08"), ("Sick Leave", "2026-08-03"),
            ("Sick Leave", "2026-09-07"), ("Casual Leave", "2026-05-22"),
            ("Floating Leave", "2026-10-02"),
        ])
    ]


def _called(hrms: FakeHrms, name: str) -> list[dict[str, Any]]:
    return [c["arguments"] for c in hrms.calls() if c["name"] == name]


def test_an_hrms_admin_is_given_the_trend_of_the_person_named(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert said.startswith(
        "Anu Thomas (H032) — leave from Mon 13 Apr 2026 to Tue 13 Oct 2026 (the last 6 months)."
    )
    assert "Leave taken: 5 days, approved leave only." in said
    assert "Leave left now: 4/12 days — Casual 4/12." in said
    assert "- Single-day sick leave on a Monday 3 times: 8 Jun, 3 Aug, 7 Sep." in said
    # Their leave, asked for by their id; their holidays for every year counted.
    assert _called(hrms, "get_leaves") == [{"employee_id": "emp-7"}]
    assert _called(hrms, "get_leave_balance") == [{"employee_id": "emp-7"}]
    assert {a["year"] for a in _called(hrms, "get_holidays")} == {"2025", "2026"}


def test_a_trend_never_carries_a_reason_or_an_id(client: TestClient, hrms: FakeHrms) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert "hospital" not in said
    assert "lv-" not in said and "emp-7" not in said


@pytest.mark.parametrize("role", ["employee", "project_manager"])
def test_anyone_but_an_admin_asking_about_someone_else_is_refused_before_the_hrms(
    client: TestClient, hrms: FakeHrms, role: str
) -> None:
    _as(hrms, role)
    _anus_leave(hrms)
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert said == "Only an HRMS administrator can look at another employee's leave pattern."
    assert _called(hrms, "get_leaves") == []
    assert _called(hrms, "resolve_employee") == []


@pytest.mark.parametrize("meant", ["me", "Ravi", "h001", "RAVI@acme.test"])
def test_anyone_may_ask_about_their_own(client: TestClient, hrms: FakeHrms, meant: str) -> None:
    _as(hrms, "employee")
    _anus_leave(hrms, owner="emp-0042")
    said = text(call(client, "get_leave_trend", {"employee": meant}))
    assert said.startswith("Ravi (H001) — leave from")
    assert _called(hrms, "get_leaves") == [{"employee_id": "emp-0042"}]
    assert _called(hrms, "resolve_employee") == []


def test_leave_that_is_not_the_persons_is_never_shown_as_theirs(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms, owner="emp-0042")  # the HRMS answered with the asker's own
    result = call(client, "get_leave_trend", {"employee": "Anu"})
    assert result["isError"] is True
    assert "not this person's" in text(result)
    assert "Anu Thomas" not in text(result)


def test_leave_that_names_no_one_is_the_persons_asked_about(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    for record in hrms.results["get_leaves"]:
        del record["employee_id"]
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert said.startswith("Anu Thomas (H032) — leave from")
    assert "Leave taken: 5 days, approved leave only." in said


def test_several_matches_are_asked_about_by_name_and_code(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    hrms.results["resolve_employee"] = [ANU, ANUSREE]
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert said.startswith('Several people match "Anu": Anu Thomas (H032), Anusree P (H101).')
    assert _called(hrms, "get_leaves") == []


@pytest.mark.parametrize("meant", ["anu thomas", "H032", "anu@acme.test"])
def test_an_exact_name_code_or_email_wins_over_a_partial_match(
    client: TestClient, hrms: FakeHrms, meant: str
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    hrms.results["resolve_employee"] = [ANU, ANUSREE]
    said = text(call(client, "get_leave_trend", {"employee": meant}))
    assert said.startswith("Anu Thomas (H032) — leave from")


def test_nobody_matching_is_said_plainly(client: TestClient, hrms: FakeHrms) -> None:
    _as(hrms, "admin")
    hrms.errors["resolve_employee"] = "no employee found matching: Zed"
    said = text(call(client, "get_leave_trend", {"employee": "Zed"}))
    assert said == 'The HRMS has no employee matching "Zed".'


def test_the_named_period_is_counted_from_the_hrms_today(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    said = text(call(client, "get_leave_trend", {"employee": "Anu", "period": "last quarter"}))
    assert said.startswith(
        "Anu Thomas (H032) — leave from Wed 1 Jul 2026 to Wed 30 Sep 2026 (last quarter)."
    )


def test_a_period_that_cannot_be_counted_reaches_no_leave(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    result = call(client, "get_leave_trend",
                  {"employee": "Anu", "period": "between dates", "date_from": "2020-01-01"})
    assert result["isError"] is True
    assert "at most 24 months" in text(result)
    assert _called(hrms, "get_leaves") == []


def test_a_balance_the_hrms_will_not_give_leaves_the_trend(
    client: TestClient, hrms: FakeHrms
) -> None:
    _as(hrms, "admin")
    _anus_leave(hrms)
    hrms.errors["get_leave_balance"] = "balance unavailable"
    said = text(call(client, "get_leave_trend", {"employee": "Anu"}))
    assert "Leave taken: 5 days" in said
    assert "Leave left now" not in said


def test_the_trend_tells_the_assistant_to_say_only_what_it_counted(client: TestClient) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    told = " ".join(tools["get_leave_trend"]["description"].split())
    for rule in ("never suggest a reason, a health matter, a motive",
                 "never compare them with other employees",
                 'never "he" or "she"',
                 "last six months, with its dates",
                 "lay it out with present",
                 "copying every figure exactly as the result writes it",
                 "never add a column up yourself"):
        assert rule in told


def test_the_trend_sends_no_card_of_its_own(client: TestClient, hrms: FakeHrms) -> None:
    """The assistant lays the trend out with present (EC-D274): the result is
    words only, carrying every figure the card copies."""
    _as(hrms, "admin")
    _anus_leave(hrms)
    result = call(client, "get_leave_trend", {"employee": "Anu"})
    assert not (result.get("_meta") or {}).get("ec/blocks")
    said = text(result)
    assert said.splitlines()[1].startswith("Pattern: Mostly single days")
    for figure in ("Change: ", "Monday or Friday: ", "By month and type: ", "Leave left now: "):
        assert figure in said
    assert "hospital" not in said


@pytest.mark.parametrize("role", ["employee", "project_manager"])
def test_a_refusal_carries_no_card(client: TestClient, hrms: FakeHrms, role: str) -> None:
    _as(hrms, role)
    _anus_leave(hrms)
    result = call(client, "get_leave_trend", {"employee": "Anu"})
    assert not (result.get("_meta") or {}).get("ec/blocks")


# --- working from home: never leave; the mode and reason, as the HRMS's own form ------


def _week(used: int, limit: int, asked: int = 1) -> dict[str, Any]:
    """The HRMS's preview_wfh answer: within its policy when it sets no limit."""
    within = limit <= 0 or used + asked <= limit
    return {
        "within_policy": within,
        "facts": {
            "requested_days": asked, "weekly_used_before_this": used, "weekly_limit": limit,
            "within_policy": within, "team_leaves": {"on_leave_names": []},
        },
    }


# --- working from home: the form -------------------------------------------------


def test_the_form_asks_the_wfh_mode_and_reason_and_never_a_leave_type(
    client: TestClient,
) -> None:
    result = call(client, "start_wfh_request", {"date_from": "2026-10-26"})
    form = _form(result)
    assert form["prompt"] == "Work from home on Mon 26 Oct 2026 — choose the rest:"
    assert form["action"] == {
        "tool": "apply_wfh",
        "arguments": {"date_from": "2026-10-26", "date_to": "2026-10-26"},
    }
    assert form["check"] == {"tool": "preview_wfh"}
    assert [q["name"] for q in form["questions"]] == ["wfh_mode", "reason"]
    assert [o["value"] for o in form["questions"][0]["options"]] == [
        "Full Day", "First Half", "Second Half",
    ]
    assert "do not call apply_wfh" in text(result)


def test_a_wfh_mode_said_is_not_asked_again(client: TestClient) -> None:
    form = _form(call(client, "start_wfh_request", {
        "date_from": "2026-10-26", "wfh_mode": "first half",
    }))
    assert [q["name"] for q in form["questions"]] == ["reason"]
    assert form["action"]["arguments"]["wfh_mode"] == "First Half"
    assert form["prompt"] == "Work from home on Mon 26 Oct 2026, first half — choose the rest:"


def test_a_complete_wfh_request_asks_nothing(client: TestClient) -> None:
    form = _form(call(client, "start_wfh_request", {
        "date_from": "2026-10-26", "date_to": "2026-10-27", "wfh_mode": "Full Day",
        "reason": "plumber visiting",
    }))
    assert form["questions"] == []
    assert form["prompt"] == "Work from home from Mon 26 Oct 2026 to Tue 27 Oct 2026, full day."
    assert form["action"]["arguments"] == {
        "date_from": "2026-10-26", "date_to": "2026-10-27",
        "wfh_mode": "Full Day", "reason": "plumber visiting",
    }


def test_no_wfh_date_is_for_the_assistant_to_ask(client: TestClient) -> None:
    result = call(client, "start_wfh_request", {"date_from": ""})
    assert result["isError"] is True and "start date" in text(result)


def test_the_form_reads_no_jira(client: TestClient, hrms: FakeHrms) -> None:
    call(client, "start_wfh_request", {"date_from": "2026-10-26"})
    assert _called(hrms, "get_jira_work") == []


def test_wfh_is_never_started_as_leave(client: TestClient, hrms: FakeHrms) -> None:
    for named in ("Work From Home", "wfh", "work-from-home", "Remote"):
        result = call(client, "start_leave_request", {
            "date_from": "2026-10-26", "leave_type": named,
        })
        assert result["isError"] is True, named
        assert "start_wfh_request" in text(result)
    assert _called(hrms, "get_leave_balance") == []


def test_wfh_said_only_in_the_reason_is_refused_by_its_flag(
    client: TestClient, hrms: FakeHrms
) -> None:
    result = call(client, "start_leave_request", {
        "date_from": "2026-10-26", "reason": "plumber visiting", "work_from_home": True,
    })
    assert result["isError"] is True and "start_wfh_request" in text(result)
    assert _called(hrms, "get_leave_balance") == []


def test_a_leave_whose_reason_says_remote_is_still_leave(client: TestClient) -> None:
    form = _form(call(client, "start_leave_request", {
        "date_from": "2026-10-26", "reason": "travelling to a remote village",
    }))
    assert form["action"]["tool"] == "apply_leave"


def test_the_instructions_send_wfh_to_its_own_request() -> None:
    from hrms_mcp.tools import INSTRUCTIONS

    said = " ".join(INSTRUCTIONS.split())
    assert "WFH and comp-off are not leave: start_wfh_request, and start_comp_off_request" in said


@pytest.mark.parametrize(("tool", "date_to", "said"), [
    ("start_wfh_request", "2026-10-3", "not a date"),
    ("preview_wfh", "2026-10-25", "before the start date"),
    ("apply_wfh", "2026-10-25", "before the start date"),
])
def test_a_wfh_end_date_is_refused_rather_than_guessed(
    client: TestClient, hrms: FakeHrms, tool: str, date_to: str, said: str
) -> None:
    result = call(client, tool, {
        "date_from": "2026-10-26", "date_to": date_to, "wfh_mode": "Full Day",
        "reason": "plumber visiting",
    })
    assert result["isError"] is True and said in text(result)
    assert _called(hrms, "apply_wfh") == []


def test_wfh_dates_reach_the_hrms_written_out(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["apply_wfh"] = {"wfh_id": "w-1", "status": "Pending"}
    call(client, "apply_wfh", {
        "date_from": "20261026", "date_to": "2026-10-27", "wfh_mode": "Full Day",
        "reason": "plumber visiting",
    })
    filed = _called(hrms, "apply_wfh")
    assert [(f["date_from"], f["date_to"]) for f in filed] == [("2026-10-26", "2026-10-27")]


# --- the check before Confirm ---------------------------------------------------


def test_within_the_weekly_limit_the_check_says_no_count(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_wfh"] = _week(used=0, limit=2)
    said = text(call(client, "preview_wfh", {
        "date_from": "2026-10-26", "wfh_mode": "Full Day", "reason": "plumber visiting",
    }))
    assert said == (
        "Work from home on Mon 26 Oct 2026, full day — reason: plumber visiting.\n\n"
        "Priya will approve it.\nNothing is filed until you confirm."
    )
    assert _called(hrms, "preview_wfh") == [{"date_from": "2026-10-26", "date_to": "2026-10-26"}]
    assert _called(hrms, "apply_wfh") == []


def test_past_the_weekly_limit_is_a_heads_up_not_a_refusal(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_wfh"] = _week(used=2, limit=2)
    said = text(call(client, "preview_wfh", {"date_from": "2026-10-26", "wfh_mode": "First Half"}))
    assert said.startswith(
        "Heads-up: This goes over the weekly WFH limit for the week of "
        "Mon 26 Oct 2026; your managers see it as an exception. "
        "Do you still want to proceed?\n\n"
        "Work from home on Mon 26 Oct 2026, first half.\n\n"
    )


def test_the_hrms_verdict_decides_not_a_sum_of_its_counts(
    client: TestClient, hrms: FakeHrms
) -> None:
    """Three days asked for, against a limit of two: the HRMS counts requests
    and says it is within, so nothing is said."""
    week = _week(used=0, limit=2, asked=3)
    week["within_policy"] = week["facts"]["within_policy"] = True
    hrms.results["preview_wfh"] = week
    said = text(call(client, "preview_wfh", {
        "date_from": "2026-10-30", "date_to": "2026-11-01", "wfh_mode": "Full Day",
    }))
    assert "Heads-up" not in said


def test_no_limit_says_nothing_of_one(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["preview_wfh"] = _week(used=3, limit=0)
    said = text(call(client, "preview_wfh", {"date_from": "2026-10-26", "wfh_mode": "Full Day"}))
    assert "limit" not in said and "Heads-up" not in said


def test_a_verdict_the_hrms_did_not_give_says_nothing(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["preview_wfh"] = {"facts": {"weekly_limit": 2, "within_policy": "no"}}
    said = text(call(client, "preview_wfh", {"date_from": "2026-10-26", "wfh_mode": "Full Day"}))
    assert said.startswith("Work from home on Mon 26 Oct 2026, full day.\n\nPriya")


def test_teammates_off_are_not_said_for_wfh(client: TestClient, hrms: FakeHrms) -> None:
    week = _week(used=0, limit=2)
    week["facts"]["team_leaves"] = {"on_leave_names": ["Anu"]}
    hrms.results["preview_wfh"] = week
    said = text(call(client, "preview_wfh", {"date_from": "2026-10-26", "wfh_mode": "Full Day"}))
    assert "Anu" not in said


# --- filing --------------------------------------------------------------------------


def test_apply_files_it_and_says_who_it_waits_for(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["apply_wfh"] = {"wfh_id": "w-1", "status": "Pending"}
    said = text(call(client, "apply_wfh", {
        "date_from": "2026-10-26", "wfh_mode": "Second Half", "reason": " plumber visiting ",
    }))
    assert said == (
        "Filed: Work from home, Mon 26 Oct 2026, second half — reason: plumber visiting. "
        "Status: Pending. It is waiting for Priya to approve."
    )
    # As the HRMS names it, and never a project manager: it goes to them all.
    assert _called(hrms, "apply_wfh") == [{
        "date_from": "2026-10-26", "date_to": "2026-10-26",
        "leave_mode": "Second Half", "reason": "plumber visiting",
    }]


def test_applying_for_wfh_twice_files_once(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_wfh_history"] = [
        {"wfh_id": "w-1", "date_from": "2026-10-26", "date_to": "2026-10-26",
         "mode": "Full Day", "status": "Pending", "reason": "plumber visiting"},
    ]
    said = text(call(client, "apply_wfh", {
        "date_from": "2026-10-26", "wfh_mode": "Full Day", "reason": "plumber visiting",
    }))
    assert said.startswith("Already filed: work from home, Mon 26 Oct 2026, full day — Pending.")
    assert _called(hrms, "apply_wfh") == []


def test_a_cancelled_one_on_the_same_day_does_not_stop_a_new_one(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_wfh_history"] = [
        {"wfh_id": "w-1", "date_from": "2026-10-26", "date_to": "2026-10-26",
         "mode": "Full Day", "status": "Cancelled"},
    ]
    call(client, "apply_wfh", {
        "date_from": "2026-10-26", "wfh_mode": "Full Day", "reason": "plumber visiting",
    })
    assert len(_called(hrms, "apply_wfh")) == 1


def test_a_wfh_request_without_a_reason_is_refused(client: TestClient, hrms: FakeHrms) -> None:
    result = call(client, "apply_wfh", {
        "date_from": "2026-10-26", "wfh_mode": "Full Day", "reason": "  ",
    })
    assert result["isError"] is True and "reason" in text(result)
    assert _called(hrms, "apply_wfh") == []


def test_the_mode_and_reason_are_required_to_file(client: TestClient) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    required = tools["apply_wfh"]["inputSchema"]["required"]
    assert {"date_from", "wfh_mode", "reason"} <= set(required)
    assert "leave_type" not in tools["apply_wfh"]["inputSchema"]["properties"]


# --- comp-off: a holiday or weekend worked, as the HRMS's own form takes it -----------


ELIGIBLE = {
    "eligible": True,
    "eligible_work_dates": ["2026-10-02", "2026-10-10"],
    "holidays": ["Gandhi Jayanti", "Weekend"],
}


def _today_is(hrms: FakeHrms, day: str = "2026-11-20") -> None:
    hrms.results["get_user_session"] = {**hrms.results["get_user_session"], "current_date": day}


def test_with_no_day_worked_the_form_offers_the_unclaimed_ones(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_comp_off_eligibility"] = ELIGIBLE
    result = call(client, "start_comp_off_request", {})
    form = _form(result)
    assert form["prompt"] == "Comp-off — choose the rest:"
    assert [q["name"] for q in form["questions"]] == ["work_date", "reason"]
    assert form["questions"][0]["options"] == [
        {"value": "2026-10-10", "label": "Sat 10 Oct 2026 · Weekend"},
        {"value": "2026-10-02", "label": "Fri 2 Oct 2026 · Gandhi Jayanti"},
    ]
    assert form["action"] == {"tool": "apply_comp_off", "arguments": {}}
    assert form["check"] == {"tool": "preview_comp_off"}
    assert "do not call apply_comp_off" in text(result)


def test_with_nothing_unclaimed_the_assistant_asks_the_day(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_comp_off_eligibility"] = {"eligible": False, "eligible_work_dates": []}
    result = call(client, "start_comp_off_request", {})
    assert result["isError"] is True and "Ask them which day they worked" in text(result)


def test_a_day_worked_that_was_named_asks_only_the_reason(
    client: TestClient, hrms: FakeHrms
) -> None:
    form = _form(call(client, "start_comp_off_request", {"work_date": "2026-10-02"}))
    assert form["prompt"] == "Comp-off for working on Fri 2 Oct 2026 — choose the rest:"
    assert [q["name"] for q in form["questions"]] == ["reason"]
    assert form["action"]["arguments"] == {"work_date": "2026-10-02"}
    assert _called(hrms, "get_comp_off_eligibility") == []


def test_a_complete_comp_off_asks_nothing_and_never_asks_the_day_off(
    client: TestClient,
) -> None:
    form = _form(call(client, "start_comp_off_request", {
        "work_date": "2026-10-02", "off_date": "2026-11-02", "reason": "release support",
    }))
    assert form["questions"] == []
    assert form["prompt"] == "Comp-off for working on Fri 2 Oct 2026, off on Mon 2 Nov 2026."
    bare = _form(call(client, "start_comp_off_request", {
        "work_date": "2026-10-02", "reason": "release support",
    }))
    assert bare["questions"] == []


def test_a_comp_off_date_that_is_not_one_is_refused(client: TestClient) -> None:
    result = call(client, "start_comp_off_request", {"work_date": "2 Oct"})
    assert result["isError"] is True and "day worked" in text(result)
    result = call(client, "apply_comp_off", {
        "work_date": "2026-10-02", "off_date": "next week", "reason": "release support",
    })
    assert result["isError"] is True and "not a date" in text(result)


def test_a_comp_off_is_never_started_as_leave(client: TestClient, hrms: FakeHrms) -> None:
    for asked in ({"leave_type": "Comp Off"}, {"leave_type": "compensatory leave"},
                  {"comp_off": True}):
        result = call(client, "start_leave_request", {"date_from": "2026-11-02", **asked})
        assert result["isError"] is True, asked
        assert "start_comp_off_request" in text(result)
    assert _called(hrms, "get_leave_balance") == []


def test_the_comp_off_check_names_the_holiday_and_who_approves(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms, "2026-10-22")
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-11-02", "reason": "release support",
    }))
    assert said == (
        "Comp-off for working on Fri 2 Oct 2026 (Gandhi Jayanti), off on Mon 2 Nov 2026 "
        "— reason: release support.\n\n"
        "Priya will approve it.\nNothing is filed until you confirm."
    )
    assert _called(hrms, "apply_comp_off") == []


def test_a_weekend_worked_is_claimable_and_no_day_off_is_said_as_later(
    client: TestClient, hrms: FakeHrms,
) -> None:
    _today_is(hrms)
    said = text(call(client, "preview_comp_off", {"work_date": "2026-10-10"}))
    assert said.startswith(
        "Comp-off for working on Sat 10 Oct 2026 (Weekend).\n"
        "The day off is not chosen yet: it can be set in the HRMS later."
    )
    assert "Heads-up" not in said


def test_a_working_day_worked_is_a_heads_up_not_a_refusal(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms)
    said = text(call(client, "preview_comp_off", {"work_date": "2026-10-08"}))
    assert said.startswith(
        "Heads-up: Thu 8 Oct 2026 is not a holiday or a weekend; your managers see it "
        "as an exception. Do you still want to proceed?"
    )


def test_a_day_off_already_off_or_before_the_day_worked_is_warned_of(
    client: TestClient, hrms: FakeHrms,
) -> None:
    _today_is(hrms)
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-10", "off_date": "2026-10-02",
    }))
    assert "The day off, Fri 2 Oct 2026, is before the day worked." in said
    assert "The day off, Fri 2 Oct 2026, is already off: Gandhi Jayanti." in said
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-11-07",
    }))
    assert "The day off, Sat 7 Nov 2026, is already off: a weekend." in said


def test_an_optional_holiday_is_not_already_off(client: TestClient, hrms: FakeHrms) -> None:
    _today_is(hrms)
    hrms.results["get_holidays"] = [
        {"date": "2026-11-02", "name": "Diwali", "type": "Optional", "is_optional": True},
    ]
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-10", "off_date": "2026-11-02",
    }))
    assert "already off" not in said


def test_a_comp_off_already_filed_is_said_in_the_check(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms)
    hrms.results["get_comp_off_history"] = [
        {"id": 7, "work_date": "2026-10-02", "off_date": None, "status": "Pending"},
    ]
    said = text(call(client, "preview_comp_off", {"work_date": "2026-10-02"}))
    assert said.startswith("Heads-up: You already have a comp-off for Fri 2 Oct 2026 — Pending.")


def test_apply_files_the_comp_off_and_says_who_it_waits_for(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["apply_comp_off"] = {"comp_off_id": 8, "work_date": "2026-10-02",
                                      "status": "Pending"}
    said = text(call(client, "apply_comp_off", {
        "work_date": "2026-10-02", "reason": " release support ",
    }))
    assert said == (
        "Filed: Comp-off for working on Fri 2 Oct 2026 — reason: release support. "
        "Status: Pending. It is waiting for Priya to approve."
    )
    # No day off, and no project manager: the HRMS sends it to them all.
    assert _called(hrms, "apply_comp_off") == [
        {"work_date": "2026-10-02", "reason": "release support"},
    ]


def test_applying_for_a_comp_off_twice_files_once(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_comp_off_history"] = [
        {"id": 7, "work_date": "2026-10-02", "off_date": "2026-11-02", "status": "Approved"},
        {"id": 6, "work_date": "2026-10-10", "status": "Rejected"},
    ]
    said = text(call(client, "apply_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-11-02", "reason": "release support",
    }))
    assert said.startswith("Already filed: comp-off for working on Fri 2 Oct 2026")
    assert _called(hrms, "apply_comp_off") == []
    call(client, "apply_comp_off", {"work_date": "2026-10-10", "reason": "go-live"})
    assert len(_called(hrms, "apply_comp_off")) == 1


def test_a_comp_off_without_a_reason_is_refused(client: TestClient, hrms: FakeHrms) -> None:
    result = call(client, "apply_comp_off", {"work_date": "2026-10-02", "reason": " "})
    assert result["isError"] is True and "reason" in text(result)
    assert _called(hrms, "apply_comp_off") == []


def test_a_day_worked_still_to_come_is_a_heads_up(client: TestClient, hrms: FakeHrms) -> None:
    _today_is(hrms, "2026-10-20")
    said = text(call(client, "preview_comp_off", {"work_date": "2026-12-26"}))
    assert said.startswith("Heads-up: Sat 26 Dec 2026 is still to come.")


def test_a_check_goes_on_when_the_hrms_will_not_say_today(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.errors["get_user_session"] = "no session"
    said = text(call(client, "preview_comp_off", {"work_date": "2026-10-02"}))
    assert said.startswith("Comp-off for working on Fri 2 Oct 2026 (Gandhi Jayanti).")


def test_already_filed_is_said_as_it_is_on_file(client: TestClient, hrms: FakeHrms) -> None:
    hrms.results["get_comp_off_history"] = [
        {"id": 7, "work_date": "2026-10-02", "off_date": None, "status": "Pending"},
    ]
    said = text(call(client, "apply_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-11-02", "reason": "release support",
    }))
    assert said == (
        "Already filed: comp-off for working on Fri 2 Oct 2026 — Pending. Nothing new was filed."
    )


def test_the_days_worked_offered_are_the_latest_whatever_the_order(
    client: TestClient, hrms: FakeHrms
) -> None:
    days = [f"2026-{m:02d}-{d:02d}" for m, d in [(10, 10), (1, 3), (12, 5)] +
            [(5, n) for n in range(1, 11)]]
    hrms.results["get_comp_off_eligibility"] = {
        "eligible": True, "eligible_work_dates": days, "holidays": ["Weekend"] * len(days),
    }
    form = _form(call(client, "start_comp_off_request", {}))
    offered = [o["value"] for o in form["questions"][0]["options"]]
    assert offered[:2] == ["2026-12-05", "2026-10-10"] and len(offered) == 10
    assert "2026-01-03" not in offered
    # The ones left out are said, to the person and to the assistant.
    assert form["prompt"].endswith(
        "The latest 10 days worked are shown; for one of the 3 older ones, say its date."
    )
    assert "3 older days worked are not among the choices" in text(
        call(client, "start_comp_off_request", {})
    )


def test_with_ten_days_or_fewer_none_is_said_to_be_left_out(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_comp_off_eligibility"] = ELIGIBLE
    result = call(client, "start_comp_off_request", {})
    assert "older" not in _form(result)["prompt"] and "older" not in text(result)


def test_names_that_do_not_line_up_with_the_days_are_not_shown(
    client: TestClient, hrms: FakeHrms
) -> None:
    """Shown with no name rather than the wrong one; a day listed twice, once."""
    hrms.results["get_comp_off_eligibility"] = {
        "eligible": True,
        "eligible_work_dates": ["2026-10-02", "2026-10-10", "2026-10-02"],
        "holidays": ["Gandhi Jayanti", "Weekend"],
    }
    form = _form(call(client, "start_comp_off_request", {}))
    assert form["questions"][0]["options"] == [
        {"value": "2026-10-10", "label": "Sat 10 Oct 2026"},
        {"value": "2026-10-02", "label": "Fri 2 Oct 2026"},
    ]


def test_a_day_off_already_past_is_a_heads_up(client: TestClient, hrms: FakeHrms) -> None:
    _today_is(hrms)
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-11-03",
    }))
    assert "The day off, Tue 3 Nov 2026, has already passed." in said


def test_a_day_worked_on_an_optional_holiday_is_claimable(
    client: TestClient, hrms: FakeHrms
) -> None:
    """As the HRMS judges it: any holiday on its calendar."""
    _today_is(hrms)
    hrms.results["get_holidays"] = [
        {"date": "2026-11-02", "name": "Diwali", "type": "Optional", "is_optional": True},
    ]
    said = text(call(client, "preview_comp_off", {"work_date": "2026-11-02"}))
    assert said.startswith("Comp-off for working on Mon 2 Nov 2026 (Diwali).")


def test_a_check_goes_on_when_a_read_it_can_do_without_fails(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms)
    for tool in ("get_holidays", "get_employee_project_managers", "get_comp_off_history"):
        hrms.errors[tool] = "not available"
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-02", "off_date": "2026-12-05",
    }))
    assert "The holiday calendar could not be read" in said
    assert "not a holiday or a weekend" not in said and "already off" not in said
    assert said.endswith(
        "Your project managers will approve it.\nNothing is filed until you confirm."
    )


def test_a_check_goes_on_when_the_managers_come_back_in_another_shape(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms, "2026-10-22")
    for shape in ([], "no managers", {"project_managers": "Priya"}):
        hrms.results["get_employee_project_managers"] = shape
        said = text(call(client, "preview_comp_off", {"work_date": "2026-10-02"}))
        assert said.endswith(
            "Your project managers will approve it.\nNothing is filed until you confirm."
        ), shape


def test_a_day_off_on_the_day_worked_is_a_heads_up(client: TestClient, hrms: FakeHrms) -> None:
    _today_is(hrms, "2026-10-01")
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-14", "off_date": "2026-10-14",
    }))
    assert "The day off, Wed 14 Oct 2026, is the day worked." in said
    assert "before the day worked" not in said


def test_a_comp_off_for_several_days_is_not_cut_to_one_quietly(client: TestClient) -> None:
    result = call(client, "start_leave_request", {
        "date_from": "2026-11-02", "date_to": "2026-11-04", "leave_type": "Comp Off",
    })
    assert result["isError"] is True
    assert "each day off needs a comp-off of its own" in text(result)


def test_a_day_off_given_before_the_day_worked_is_shown_on_the_form(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["get_comp_off_eligibility"] = ELIGIBLE
    form = _form(call(client, "start_comp_off_request", {"off_date": "2026-11-02"}))
    assert form["prompt"] == "Comp-off, off on Mon 2 Nov 2026 — choose the rest:"
    assert form["action"]["arguments"] == {"off_date": "2026-11-02"}


def test_a_public_holiday_is_not_hidden_by_an_optional_one_on_the_same_day(
    client: TestClient, hrms: FakeHrms
) -> None:
    _today_is(hrms, "2026-10-22")
    hrms.results["get_holidays"] = [
        {"date": "2026-11-02", "name": "Diwali", "type": "Public", "is_optional": False},
        {"date": "2026-11-02", "name": "Govardhan Puja", "type": "Optional", "is_optional": True},
    ]
    said = text(call(client, "preview_comp_off", {
        "work_date": "2026-10-10", "off_date": "2026-11-02",
    }))
    assert "The day off, Mon 2 Nov 2026, is already off: Diwali." in said


def test_a_comp_off_filed_with_no_status_is_said_as_pending(
    client: TestClient, hrms: FakeHrms
) -> None:
    hrms.results["apply_comp_off"] = {"comp_off_id": 12, "status": None}
    said = text(call(client, "apply_comp_off", {
        "work_date": "2026-10-02", "reason": "release support",
    }))
    assert "Status: Pending. It is waiting for Priya to approve." in said
