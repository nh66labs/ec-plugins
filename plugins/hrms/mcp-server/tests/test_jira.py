"""Warning about work a leave would leave undone, from Jira.

The rules on their own (``workload``); the reader against a fake Jira, which
must skip rather than fail; and the whole check through ``preview_leave``, with
the person's session and projects from the fake HRMS.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from hrms_mcp import jira as jira_module
from hrms_mcp.app import create_app
from hrms_mcp.config import Settings
from hrms_mcp.jira import Jira, project_keys, ticket_of
from hrms_mcp.workload import (
    ProjectWork,
    Sprint,
    Ticket,
    own_work,
    their_work,
    tight_sprints,
)
from tests.test_server import EC_TOKEN, FakeHrms, call, text

FRI, MON = date(2026, 10, 2), date(2026, 10, 5)


def _t(key: str, who: str = "", category: str = "new", **over: Any) -> Ticket:
    status = {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}[category]
    return Ticket(key=key, summary=f"Task {key}", status=status, category=category,
                  assignee_name=who, **over)


def _sprint(open_count: int, done: int, ends: date = MON, who: str = "Anu") -> Sprint:
    tickets = [
        _t(f"ECP-{i}", who if i == 1 else "", "indeterminate") for i in range(1, open_count + 1)
    ]
    tickets += [_t(f"ECP-{100 + i}", category="done") for i in range(done)]
    return Sprint(name="Sprint 14", ends=ends, tickets=tickets)


# -- the rules -------------------------------------------------------------------------


def test_the_persons_own_open_tickets_are_named_in_progress_first() -> None:
    work = [ProjectWork("ECP", open_tickets=[
        _t("ECP-3", "Ravi"),
        _t("ECP-1", "ravi", "indeterminate", due=FRI),
        _t("ECP-2", "Anu"),
        _t("ECP-4", "Ravi", "done"),
    ])]
    said = own_work(work, "Ravi", "", FRI, FRI)
    assert said == (
        "You have 2 open Jira tickets: ECP-1 Task ECP-1 (In Progress, due Fri 2 Oct) and "
        "ECP-3 Task ECP-3 (To Do) — worth handing over before you go."
    )
    assert own_work(work, "Priya", "", FRI, FRI) == ""


def test_more_than_three_are_counted_not_listed() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t(f"ECP-{i}", "Ravi") for i in range(5)])]
    said = own_work(work, "Ravi", "", FRI, FRI)
    assert said.count("(To Do)") == 3
    assert "and 2 more" in said


def test_the_person_is_found_by_email_before_name() -> None:
    mine = _t("ECP-1", "R. K.", assignee_email="ravi@acme.test")
    work = [ProjectWork("ECP", open_tickets=[mine])]
    assert own_work(work, "Ravi", "RAVI@acme.test", FRI, FRI).startswith("You have 1 open")


def test_a_tight_sprint_with_a_teammate_off_names_their_open_tickets() -> None:
    work = [ProjectWork("ECP", sprint=_sprint(open_count=6, done=4))]
    (said,) = tight_sprints(work, ["Anu"], FRI, FRI, tight_days=3, tight_share=0.3)
    assert said == (
        "ECP's sprint “Sprint 14” ends on Mon 5 Oct with 6 of 10 tickets still to do or in "
        "progress, including Anu's ECP-1 Task ECP-1 (In Progress)."
    )


def test_the_one_person_off_is_found_in_a_tight_sprint_by_their_jira_account() -> None:
    sprint = _sprint(open_count=6, done=4, who="navaneeth")
    sprint.tickets[0] = _t("ECP-1", "navaneeth", "indeterminate", assignee_account="acc-nav")
    work = [ProjectWork("ECP", sprint=sprint)]
    (said,) = tight_sprints(
        work, ["Navaneeth K"], FRI, FRI, tight_days=3, tight_share=0.3, account="acc-nav"
    )
    assert said.endswith(", including Navaneeth K's ECP-1 Task ECP-1 (In Progress).")


@pytest.mark.parametrize(
    ("off", "sprint", "why"),
    [
        ([], _sprint(6, 4), "nobody else is off"),
        (["Anu"], _sprint(2, 8), "most of it is done"),
        (["Anu"], _sprint(6, 4, ends=date(2026, 10, 9)), "it ends a week later"),
        (["Anu"], _sprint(6, 4, ends=date(2026, 9, 30)), "it ended before the leave"),
    ],
)
def test_a_sprint_that_is_not_tight_or_no_one_off_is_not_warned_of(
    off: list[str], sprint: Sprint, why: str
) -> None:
    work = [ProjectWork("ECP", sprint=sprint)]
    assert tight_sprints(work, off, FRI, FRI, tight_days=3, tight_share=0.3) == [], why


def test_a_manager_is_shown_the_applicants_open_tickets_or_told_there_are_none() -> None:
    work = [ProjectWork("ECP", open_tickets=[
        _t("ECP-7", "Anu", "indeterminate"), _t("ECP-8", "Ravi"), _t("ECP-9", "Anu", "done"),
    ])]
    assert their_work(work, "Anu", FRI, FRI) == (
        "Anu has 1 open Jira ticket in ECP: ECP-7 Task ECP-7 (In Progress)."
    )
    assert their_work(work, "Priya", FRI, FRI, account="acc-priya") == (
        "Priya has no open Jira tickets in ECP."
    )


def test_none_is_not_said_when_their_jira_account_is_unknown() -> None:
    work = [ProjectWork("PAY", open_tickets=[_t("PAY-3", "navaneeth", "indeterminate")])]
    said = their_work(work, "Navaneeth K", FRI, FRI)
    assert "has no open" not in said
    assert said.startswith("No open Jira tickets in PAY were found for Navaneeth K.")
    assert "account could not be confirmed" in said


def test_none_is_not_said_when_not_every_open_ticket_was_read() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t("ECP-8", "Ravi")], complete=False)]
    said = their_work(work, "Anu", FRI, FRI, account="acc-anu")
    assert "has no open" not in said
    assert "only some of the open tickets in ECP could be read" in said
    found = [ProjectWork("ECP", open_tickets=[_t("ECP-7", "Anu")], complete=False)]
    assert their_work(found, "Anu", FRI, FRI).startswith("Anu has 1 open Jira ticket in ECP")
    assert "may not be listed" in their_work(found, "Anu", FRI, FRI)


def test_a_manager_is_shown_up_to_ten_of_them() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t(f"ECP-{i}", "Anu") for i in range(12)])]
    said = their_work(work, "Anu", FRI, FRI)
    assert said.count("(To Do)") == 10
    assert said.endswith("and 2 more.")


def test_a_person_is_matched_by_jira_account_before_any_name() -> None:
    theirs = _t("ECP-7", "navaneeth", "indeterminate", assignee_account="acc-nav")
    namesake = _t("ECP-8", "Navaneeth K", assignee_account="acc-other")
    work = [ProjectWork("ECP", open_tickets=[theirs, namesake])]
    assert their_work(work, "Navaneeth K", FRI, FRI, account="acc-nav") == (
        "Navaneeth K has 1 open Jira ticket in ECP: ECP-7 Task ECP-7 (In Progress)."
    )
    assert own_work(work, "Navaneeth K", "", FRI, FRI, account="acc-nav").startswith(
        "You have 1 open Jira ticket: ECP-7"
    )


# -- reading Jira ----------------------------------------------------------------------


def _settings(**over: Any) -> Settings:
    values = dict(
        hrms_mcp_url="http://hrms.test/api/v1/mcp", hrms_api_key="api-key",
        hrms_lookup_url="http://hrms.test/lookup", hrms_lookup_token="lookup-token",
        ec_token=EC_TOKEN, jira_url="https://acme.atlassian.test",
        jira_email="bot@acme.test", jira_api_token="jira-token",
        jira_projects="EC Platform=ECP; Payroll = PAY",
    )
    values.update(over)
    return Settings(_env_file=None, **values)


def _issue(
    key: str, who: str, category: str = "indeterminate", account: str = ""
) -> dict[str, Any]:
    assignee = {"displayName": who, **({"accountId": account} if account else {})}
    return {"key": key, "fields": {
        "summary": f"Task {key}",
        "status": {"name": "In Progress", "statusCategory": {"key": category}},
        "assignee": assignee, "duedate": None,
    }}


class FakeJira:
    def __init__(self) -> None:
        self.open = [_issue("ECP-1", "Anu"), _issue("ECP-2", "Ravi")]
        self.sprint = [_issue(f"ECP-{i}", "Anu" if i == 1 else "Bala") for i in range(1, 7)] + [
            _issue(f"ECP-{i}", "Bala", "done") for i in range(7, 11)
        ]
        self.refuse = False
        self.paged = False
        self.paths: list[str] = []
        #: Who Jira's user search finds, by the query it was given.
        self.users: dict[str, list[dict[str, Any]]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.headers.get("Authorization", "").startswith("Basic ") is False or self.refuse:
            return httpx.Response(401)
        path = request.url.path
        if path == "/rest/api/3/search/jql":
            if self.paged:
                token = request.url.params.get("nextPageToken")
                first = token is None
                return httpx.Response(200, json={
                    "issues": self.open[:1] if first else self.open[1:],
                    "isLast": not first, **({"nextPageToken": "p2"} if first else {}),
                })
            return httpx.Response(200, json={"issues": self.open})
        if path == "/rest/agile/1.0/board":
            return httpx.Response(200, json={"values": [{"id": 7}]})
        if path == "/rest/agile/1.0/board/7/sprint":
            return httpx.Response(200, json={"values": [
                {"id": 70, "name": "Sprint 14", "endDate": "2026-10-05T10:00:00.000Z"},
            ]})
        if path == "/rest/agile/1.0/sprint/70/issue":
            if self.paged:
                start = int(request.url.params.get("startAt", 0))
                return httpx.Response(200, json={
                    "issues": self.sprint[start:start + 4], "total": len(self.sprint),
                })
            return httpx.Response(200, json={"issues": self.sprint})
        if path == "/rest/api/3/user/search":
            return httpx.Response(200, json=self.users.get(request.url.params["query"], []))
        return httpx.Response(404)


def test_the_operators_map_names_the_jira_projects() -> None:
    assert project_keys(_settings(), ["ec platform", "Payroll", "Other"]) == ["ECP", "PAY"]


def test_a_ticket_is_read_from_jiras_fields() -> None:
    ticket = ticket_of(_issue("ECP-1", "Anu"))
    assert (ticket.key, ticket.status, ticket.category, ticket.assignee_name) == (
        "ECP-1", "In Progress", "indeterminate", "Anu",
    )


def test_a_project_is_read_with_its_active_sprint() -> None:
    fake = FakeJira()
    (work,) = asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"]))
    assert [t.key for t in work.open_tickets] == ["ECP-1", "ECP-2"]
    assert work.sprint is not None and work.sprint.ends == MON
    assert len(work.sprint.tickets) == 10


def test_every_page_of_open_work_and_the_sprint_is_read() -> None:
    fake = FakeJira()
    fake.paged = True
    (work,) = asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"]))
    assert [t.key for t in work.open_tickets] == ["ECP-1", "ECP-2"]
    assert work.sprint is not None and len(work.sprint.tickets) == 10


def test_open_work_cut_off_at_the_limit_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jira_module, "_LIMIT", 1)
    fake = FakeJira()
    fake.paged = True
    (work,) = asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"]))
    assert [t.key for t in work.open_tickets] == ["ECP-1"]
    assert work.complete is False
    monkeypatch.setattr(jira_module, "_LIMIT", 1000)
    (work,) = asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"]))
    assert work.complete is True


def test_a_jira_that_refuses_is_skipped_not_failed() -> None:
    fake = FakeJira()
    fake.refuse = True
    assert asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"])) == []


def test_an_account_is_found_by_email_only_when_jira_is_sure() -> None:
    fake = FakeJira()
    fake.users = {
        "nav@acme.test": [
            {"accountId": "acc-nav", "displayName": "navaneeth", "emailAddress": "nav@acme.test"}
        ],
        "hid@acme.test": [{"accountId": "acc-hid", "displayName": "hid"}],
        "two@acme.test": [{"accountId": "a1"}, {"accountId": "a2"}],
        "jo@acme.test": [{"accountId": "acc-jo-au", "emailAddress": "jo@acme.test.au"}],
        "sam@acme.test": [
            {"accountId": "acc-sam", "emailAddress": "SAM@acme.test"},
            {"accountId": "acc-sam2", "emailAddress": "sam@acme.test.au"},
            {"accountId": "acc-hidden"},
        ],
    }
    jira = Jira(_settings(), transport=httpx.MockTransport(fake.handler))
    assert asyncio.run(jira.account_of("nav@acme.test")) == "acc-nav"
    assert asyncio.run(jira.account_of("two@acme.test")) == "", "ambiguous"
    assert asyncio.run(jira.account_of("jo@acme.test")) == "", "another email is someone else"
    assert asyncio.run(jira.account_of("sam@acme.test")) == "acc-sam", "the exact email wins"
    assert asyncio.run(jira.account_of("hid@acme.test")) == "", "a hidden email may be another's"
    assert asyncio.run(jira.account_of("nobody@acme.test")) == ""
    fake.refuse = True
    assert asyncio.run(jira.account_of("nav@acme.test")) == "", "a refusal is no account"


def test_unconfigured_jira_is_never_called() -> None:
    assert Jira(_settings(jira_api_token="")).configured is False


# -- the whole check -------------------------------------------------------------------


@pytest.fixture()
def jira() -> FakeJira:
    return FakeJira()


@pytest.fixture()
def checked(jira: FakeJira):
    hrms = FakeHrms()
    hrms.results["get_user_session"] = {"authenticated": True, "name": "Ravi",
                                        "company_email": "ravi@acme.test"}
    hrms.results["get_my_projects"] = [{"name": "EC Platform"}]
    hrms.results["preview_leave"] = {
        "effective_days": 1, "created": False,
        "facts": {"team_leaves": {"team_size": 4, "overlap_count": 1, "on_leave_names": ["Anu"]}},
        "briefing": [
            "Team: 1 teammate(s) on leave that day: [Anu].",
            "Jira: Ticket ECP-2 is due on 2026-10-02.",
            "Balance: 10.0 Casual Leave day(s) available; this request uses 1.0.",
        ],
    }
    app = create_app(
        _settings(),
        hrms_transport=httpx.MockTransport(hrms.handler),
        jira_transport=httpx.MockTransport(jira.handler),
    )
    with TestClient(app, base_url="http://hrms-mcp:8000") as client:
        yield client, hrms


def _preview(client: TestClient) -> str:
    return text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    }))


def test_the_check_warns_once_with_the_teammate_the_sprint_and_the_persons_tickets(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, _ = checked
    said = _preview(client)
    warning = said.splitlines()[0]
    assert warning == (
        "Heads-up: Anu has already applied for leave on Fri 2 Oct 2026, so it may be difficult "
        "to approve. ECP's sprint “Sprint 14” ends on Mon 5 Oct with 6 of 10 tickets still to "
        "do or in progress, including Anu's ECP-1 Task ECP-1 (In Progress). You have 1 open "
        "Jira ticket: ECP-2 Task ECP-2 (In Progress) — worth handing over before you go. "
        "Do you still want to apply?"
    )
    assert "Jira: Ticket" not in said, "said once, in the warning"
    assert "Balance: 10.0" in said and "Nothing has been filed." in said


def test_a_jira_that_does_not_answer_leaves_the_hrms_check_as_it_was(
    checked: tuple[TestClient, FakeHrms], jira: FakeJira
) -> None:
    client, _ = checked
    jira.refuse = True
    said = _preview(client)
    assert said.splitlines()[0] == (
        "Heads-up: Anu has already applied for leave on Fri 2 Oct 2026, so it may be difficult "
        "to approve. Do you still want to apply?"
    )
    assert "Jira: Ticket ECP-2 is due on 2026-10-02." in said, "the HRMS's own Jira line stays"


# -- a project manager asking about someone's leave -----------------------------------------


@pytest.fixture()
def managing(jira: FakeJira):
    hrms = FakeHrms()
    hrms.results["get_user_session"] = {"authenticated": True, "name": "Priya"}
    hrms.results["get_my_projects"] = [{"name": "EC Platform"}]
    hrms.results["get_leave_routing"] = {
        "leave_id": "lv-9", "employee": "Anu",
        "briefing": [
            'Calendar: "Payments sync" (10:00) — you attend.',
            "Team: 1 teammate(s) already on leave that day: [Bala].",
            "Jira: Ticket ECP-1 is due on 2026-10-01.",
            "Balance: 9.0 Casual Leave day(s) available; this request uses 1.0.",
        ],
    }
    app = create_app(
        _settings(),
        hrms_transport=httpx.MockTransport(hrms.handler),
        jira_transport=httpx.MockTransport(jira.handler),
    )
    with TestClient(app, base_url="http://hrms-mcp:8000") as client:
        yield client, hrms


def _impact(client: TestClient, request_id: str = "lv-9") -> dict:
    return call(client, "get_leave_request_impact", {"request_id": request_id})


def test_a_manager_asking_about_a_request_is_told_what_the_applicant_leaves_undone(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, _ = managing
    assert text(_impact(client)).splitlines() == [
        "Anu — Casual leave, Thu 1 Oct 2026, full day, Pending. Reason: family function.",
        "Anu has 1 open Jira ticket in ECP: ECP-1 Task ECP-1 (In Progress).",
        "Only the asker's own Jira projects were checked (ECP); any other projects Anu "
        "works on were not.",
        "ECP's sprint “Sprint 14” ends on Mon 5 Oct with 6 of 10 tickets still to do or in "
        "progress, including Anu's ECP-1 Task ECP-1 (In Progress).",
        'Calendar: "Payments sync" (10:00) — they attend.',
        "Team: 1 teammate(s) already on leave that day: [Bala].",
        "Balance: 9.0 Casual Leave day(s) available; this request uses 1.0.",
    ]


def test_an_approved_request_can_be_asked_about_too(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    hrms.results["get_team_leaves"][0]["status"] = "Approved"
    said = text(_impact(client))
    assert said.startswith("Anu — Casual leave, Thu 1 Oct 2026, full day, Approved.")


def test_a_request_not_on_the_askers_team_is_not_described(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    refused = _impact(client, "someone-elses")
    assert refused["isError"] is True
    assert "get_leave_routing" not in [c["name"] for c in hrms.calls()]


def test_someone_who_manages_nobody_cannot_ask_about_others_leave(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    hrms.errors["get_team_leaves"] = "access denied: only project managers and admins can view"
    assert "only project managers and HR" in text(_impact(client))


def test_without_jira_the_manager_is_told_it_was_not_checked_and_keeps_the_hrms_lines(
    managing: tuple[TestClient, FakeHrms], jira: FakeJira
) -> None:
    client, _ = managing
    jira.refuse = True
    said = text(_impact(client))
    assert "Jira was not checked, so their open tickets are unknown." in said
    assert "Jira: Ticket ECP-1 is due on 2026-10-01." in said


def test_the_applicant_is_found_in_jira_by_their_hrms_email_whatever_jira_calls_them(
    managing: tuple[TestClient, FakeHrms], jira: FakeJira
) -> None:
    client, hrms = managing
    hrms.results["resolve_employee"] = [
        {"id": "emp-7", "name": "Anu", "company_email": "anu@acme.test"},
        {"id": "emp-8", "name": "Anu Mathew", "company_email": "anu.m@acme.test"},
    ]
    jira.users = {
        "anu@acme.test": [
            {"accountId": "acc-anu", "displayName": "anu.k", "emailAddress": "anu@acme.test"}
        ]
    }
    jira.open = [
        _issue("ECP-5", "anu.k", account="acc-anu"),
        _issue("ECP-1", "Anu", account="acc-someone-else"),
    ]
    said = text(_impact(client))
    assert "Anu has 1 open Jira ticket in ECP: ECP-5 Task ECP-5 (In Progress)." in said
    assert "Only the asker's own Jira projects were checked (ECP)" in said


def test_the_employee_is_warned_of_tickets_jira_files_under_another_name(
    checked: tuple[TestClient, FakeHrms], jira: FakeJira
) -> None:
    client, _ = checked
    jira.users = {
        "ravi@acme.test": [
            {"accountId": "acc-ravi", "displayName": "ravi.k", "emailAddress": "ravi@acme.test"}
        ]
    }
    jira.open = [_issue("ECP-3", "ravi.k", account="acc-ravi")]
    assert "You have 1 open Jira ticket: ECP-3 Task ECP-3 (In Progress)" in _preview(client)
