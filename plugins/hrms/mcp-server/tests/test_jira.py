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

from hrms_mcp.app import create_app
from hrms_mcp.config import Settings
from hrms_mcp.jira import Jira, project_keys, ticket_of
from hrms_mcp.workload import ProjectWork, Sprint, Ticket, own_work, tight_sprints
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


def _issue(key: str, who: str, category: str = "indeterminate") -> dict[str, Any]:
    return {"key": key, "fields": {
        "summary": f"Task {key}",
        "status": {"name": "In Progress", "statusCategory": {"key": category}},
        "assignee": {"displayName": who}, "duedate": None,
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


def test_a_jira_that_refuses_is_skipped_not_failed() -> None:
    fake = FakeJira()
    fake.refuse = True
    assert asyncio.run(Jira(_settings(), httpx.MockTransport(fake.handler)).work(["ECP"])) == []


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
