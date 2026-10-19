"""Warning about work a leave would leave undone, from Jira.

The rules on their own (``workload``); the HRMS's answer read (``jira_work``),
which must skip rather than fail; and the whole check through ``preview_leave``,
with the person's session and Jira work from the fake HRMS — which reads Jira
for this server, through the connections set up on its dashboard.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from hrms_mcp import forms
from hrms_mcp.app import create_app
from hrms_mcp.config import Settings
from hrms_mcp.forms import PROMPT_LIMIT
from hrms_mcp.jira_work import JiraReader, ticket_of, work_of
from hrms_mcp.workload import (
    ProjectWork,
    Sprint,
    Ticket,
    own_heads_up,
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


#: A leave on Thu 12 Nov, asked for six weeks ahead or three working days ahead.
NOV12, FAR, SOON = date(2026, 11, 12), date(2026, 10, 1), date(2026, 11, 9)


def _around_the_leave(who: str = "Ravi") -> list[ProjectWork]:
    """One open ticket of theirs for each place a ticket can stand against the leave."""
    return [ProjectWork("ECP", open_tickets=[
        _t("ECP-1", who, due=date(2026, 11, 12)),  # while they are away
        _t("ECP-2", who, due=date(2026, 11, 13)),  # the day they are back
        _t("ECP-3", who, due=date(2026, 11, 18)),  # four working days after: not touched
        _t("ECP-4", who, "indeterminate", due=date(2026, 11, 10)),  # before they go
        _t("ECP-5", who, due=date(2026, 11, 2)),  # overdue
        _t("ECP-6", who, "indeterminate"),  # in progress, no deadline
        _t("ECP-7", who),  # to do, no deadline
        _t("ECP-8", "Bala", due=date(2026, 11, 12)),  # someone else's
        _t("ECP-9", who, "done", due=date(2026, 11, 12)),  # done
    ])]


def test_tickets_due_well_before_a_leave_far_off_are_counted_not_named() -> None:
    work = [ProjectWork("ECP", open_tickets=[
        _t("ECP-1", "Ravi", "indeterminate", due=date(2026, 10, 30)),
        _t("ECP-2", "Ravi", due=date(2026, 10, 28)),
    ])]
    assert own_work(work, "Ravi", "", NOV12, NOV12, today=FAR) == (
        "", "None of your 2 open Jira tickets are due around your leave."
    )
    assert own_work(work[:0], "Ravi", "", NOV12, NOV12, today=FAR) == ("", "")


def test_a_leave_far_off_names_only_what_is_due_while_away_or_just_after() -> None:
    warning, note = own_work(_around_the_leave(), "Ravi", "", NOV12, NOV12, today=FAR)
    assert warning == (
        "You have 2 open Jira tickets to hand over before you go: ECP-1 Task ECP-1 (To Do, "
        "due Thu 12 Nov, while you're away) and ECP-2 Task ECP-2 (To Do, due Fri 13 Nov, the "
        "day you're back). 5 other open tickets of yours are not due around your leave."
    )
    assert note == ""


def test_a_leave_soon_also_names_work_due_before_it_and_work_in_progress() -> None:
    warning, _ = own_work(_around_the_leave(), "Ravi", "", NOV12, NOV12, today=SOON)
    assert warning == (
        "You have 5 open Jira tickets to hand over before you go: ECP-1 Task ECP-1 (To Do, "
        "due Thu 12 Nov, while you're away), ECP-2 Task ECP-2 (To Do, due Fri 13 Nov, the day "
        "you're back), ECP-5 Task ECP-5 (To Do, overdue since Mon 2 Nov) and 2 more. "
        "2 other open tickets of yours are not due around your leave."
    )


def test_a_ticket_with_no_due_date_takes_the_end_of_its_sprint() -> None:
    sprint = Sprint("Sprint 14", date(2026, 11, 13), [_t("ECP-1", "Ravi")])
    work = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi")], sprint=sprint)]
    assert own_work(work, "Ravi", "", NOV12, NOV12, today=FAR)[0] == (
        "You have 1 open Jira ticket to hand over before you go: ECP-1 Task ECP-1 (To Do, no "
        "due date; sprint “Sprint 14” ends Fri 13 Nov, the day you're back)."
    )


def test_a_sprint_left_open_past_its_end_is_missed_not_ahead() -> None:
    sprint = Sprint("Sprint 14", date(2026, 11, 2), [_t("ECP-1", "Ravi")])
    work = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi")], sprint=sprint)]
    assert own_work(work, "Ravi", "", NOV12, NOV12, today=SOON)[0] == (
        "You have 1 open Jira ticket to hand over before you go: ECP-1 Task ECP-1 (To Do, no "
        "due date; sprint “Sprint 14” ended Mon 2 Nov)."
    )


def test_a_deadline_on_the_weekend_straight_after_the_leave_falls_while_away() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi", due=date(2026, 10, 3))])]
    assert own_work(work, "Ravi", "", FRI, FRI)[0] == (
        "You have 1 open Jira ticket to hand over before you go: ECP-1 Task ECP-1 (To Do, "
        "due Sat 3 Oct, while you're away)."
    )


def test_own_work_says_when_not_every_open_ticket_was_read() -> None:
    caveat = (
        "Only some of the open tickets in ECP could be read, so some of yours may not be counted."
    )
    far = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi", due=date(2026, 10, 30))],
                       complete=False)]
    assert own_work(far, "Ravi", "", NOV12, NOV12, today=FAR) == (
        "", f"Your 1 open Jira ticket is not due around your leave. {caveat}"
    )
    near = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi", due=NOV12)], complete=False)]
    assert own_work(near, "Ravi", "", NOV12, NOV12, today=FAR)[0].endswith(caveat)
    assert own_work(far, "Bala", "", NOV12, NOV12, today=FAR) == ("", caveat)


def test_the_operator_sets_how_long_after_the_leave_still_counts() -> None:
    warning, _ = own_work(
        _around_the_leave(), "Ravi", "", NOV12, NOV12, today=FAR, after_days=4
    )
    assert "ECP-3 Task ECP-3 (To Do, due Wed 18 Nov, just after you're back)" in warning


def test_more_than_three_are_counted_not_listed() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t(f"ECP-{i}", "Ravi", due=FRI) for i in range(5)])]
    warning, _ = own_work(work, "Ravi", "", FRI, FRI)
    assert warning.count("while you're away)") == 3
    assert "and 2 more." in warning


def test_the_person_is_found_by_email_before_name() -> None:
    mine = _t("ECP-1", "R. K.", assignee_email="ravi@acme.test", due=FRI)
    work = [ProjectWork("ECP", open_tickets=[mine])]
    assert own_work(work, "Ravi", "RAVI@acme.test", FRI, FRI)[0].startswith("You have 1 open")


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


def test_a_manager_is_shown_what_the_leave_touches_first_then_the_rest() -> None:
    assert their_work(_around_the_leave("Anu"), "Anu", NOV12, NOV12, today=FAR) == (
        "Anu has 7 open Jira tickets in ECP; 2 are affected by this leave: ECP-1 Task ECP-1 "
        "(To Do, due Thu 12 Nov, while they're away) and ECP-2 Task ECP-2 (To Do, due Fri 13 "
        "Nov, the day they're back). Also open, not affected by the leave: ECP-5 Task ECP-5 "
        "(To Do, due Mon 2 Nov), ECP-4 Task ECP-4 (In Progress, due Tue 10 Nov), ECP-3 Task "
        "ECP-3 (To Do, due Wed 18 Nov), ECP-6 Task ECP-6 (In Progress) and ECP-7 Task ECP-7 "
        "(To Do)."
    )
    said = their_work(_around_the_leave("Anu"), "Anu", NOV12, NOV12, today=SOON)
    assert "; 5 are affected by this leave:" in said
    assert "ECP-4 Task ECP-4 (In Progress, due Tue 10 Nov, before they go)" in said


def test_a_manager_is_told_when_nothing_is_affected_or_there_is_nothing_open() -> None:
    work = [ProjectWork("ECP", open_tickets=[
        _t("ECP-7", "Anu", "indeterminate", due=date(2026, 10, 30)), _t("ECP-8", "Ravi"),
    ])]
    assert their_work(work, "Anu", NOV12, NOV12, today=FAR) == (
        "Anu's 1 open Jira ticket in ECP is not affected by this leave: ECP-7 Task ECP-7 "
        "(In Progress, due Fri 30 Oct)."
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
    assert their_work(found, "Anu", FRI, FRI).startswith("Anu's 1 open Jira ticket in ECP")
    assert "may not be listed" in their_work(found, "Anu", FRI, FRI)


def test_a_manager_is_shown_up_to_ten_of_them() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t(f"ECP-{i}", "Anu") for i in range(12)])]
    said = their_work(work, "Anu", FRI, FRI)
    assert said.count("(To Do)") == 10
    assert said.endswith("and 2 more.")
    touched = [ProjectWork("ECP", open_tickets=[
        _t(f"ECP-{i}", "Anu", due=FRI if i < 8 else None) for i in range(12)
    ])]
    said = their_work(touched, "Anu", FRI, FRI)
    assert said.count("while they're away)") == 8
    assert said.count("(To Do)") == 2, "the rest fill the room left"
    assert said.endswith("Also open, not affected by the leave: ECP-10 Task ECP-10 (To Do), "
                         "ECP-11 Task ECP-11 (To Do) and 2 more.")


def test_a_person_is_matched_by_jira_account_before_any_name() -> None:
    theirs = _t("ECP-7", "navaneeth", "indeterminate", assignee_account="acc-nav")
    namesake = _t("ECP-8", "Navaneeth K", assignee_account="acc-other")
    work = [ProjectWork("ECP", open_tickets=[theirs, namesake])]
    assert their_work(work, "Navaneeth K", FRI, FRI, account="acc-nav") == (
        "Navaneeth K's 1 open Jira ticket in ECP is not affected by this leave: ECP-7 Task "
        "ECP-7 (In Progress)."
    )
    assert own_work(work, "Navaneeth K", "", FRI, FRI, account="acc-nav") == (
        "", "Your 1 open Jira ticket is not due around your leave."
    )


# -- reading the HRMS's answer ----------------------------------------------------------


def _settings(**over: Any) -> Settings:
    values = dict(
        hrms_mcp_url="http://hrms.test/api/v1/mcp", hrms_api_key="api-key",
        hrms_lookup_url="http://hrms.test/lookup", hrms_lookup_token="lookup-token",
        ec_token=EC_TOKEN,
    )
    values.update(over)
    return Settings(_env_file=None, **values)


def _ticket(
    key: str, who: str = "", category: str = "indeterminate", due: str | None = None,
    account: str = "", summary: str = "",
) -> dict[str, Any]:
    status = {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}[category]
    return {"key": key, "summary": summary or f"Task {key}", "status": status,
            "status_category": category, "assignee_name": who, "assignee_account": account,
            "due_date": due}


class FakeJiraWork:
    """Answers ``get_jira_work`` as the HRMS does: in EC Platform (ECP), the
    open tickets of the person asked about — Ravi's own, or Anu's for her
    manager — and the active sprint with everyone's tickets in it."""

    def __init__(self) -> None:
        self.mine = [_ticket("ECP-2", "Ravi")]
        self.theirs = [_ticket("ECP-1", "Anu")]
        self.sprint = [_ticket(f"ECP-{i}", "Anu" if i == 1 else "Bala") for i in range(1, 7)] + [
            _ticket(f"ECP-{i}", "Bala", "done") for i in range(7, 11)
        ]
        self.complete = True
        self.not_linked: list[str] = []
        self.unreadable: list[dict[str, str]] = []
        self.projects = True
        #: Whose view a manager's read is: "managed", or "all" for HR.
        self.view = "managed"

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        own = not arguments.get("leave_id")
        account = "acc-ravi" if own else "acc-anu"
        projects = [{
            "name": "EC Platform", "key": "ECP", "account_id": account, "complete": self.complete,
            "open_tickets": self.mine if own else self.theirs,
            "sprint": {"name": "Sprint 14", "end_date": "2026-10-05", "issues": self.sprint},
        }] if self.projects else []
        return {"employee": "Ravi" if own else "Anu", "account_id": account, "projects": projects,
                "not_linked": self.not_linked, "unreadable": self.unreadable,
                "view": "own" if own else self.view}


def test_a_ticket_is_read_from_the_hrms_answer() -> None:
    ticket = ticket_of(_ticket("ECP-1", "Anu", due="2026-10-02", account="acc-anu"))
    assert (ticket.key, ticket.status, ticket.category, ticket.assignee_name, ticket.due) == (
        "ECP-1", "In Progress", "indeterminate", "Anu", FRI,
    )
    assert ticket.assignee_account == "acc-anu"


def test_a_project_is_read_with_its_active_sprint() -> None:
    work = work_of(FakeJiraWork()({}))
    assert work is not None and work.account == "acc-ravi"
    (project,) = work.projects
    assert [t.key for t in project.open_tickets] == ["ECP-2"]
    assert project.sprint is not None and project.sprint.ends == MON
    assert len(project.sprint.tickets) == 10 and project.complete is True


def test_the_persons_own_tickets_are_filed_under_their_jira_account() -> None:
    """The HRMS read them as theirs, whatever name Jira shows: the rules then
    match them by account, never by a name spelt differently."""
    fake = FakeJiraWork()
    fake.mine = [_ticket("ECP-3", "ravi.k")]
    (project,) = work_of(fake({})).projects
    assert project.open_tickets[0].assignee_account == "acc-ravi"
    assert project.sprint is not None
    assert {t.assignee_account for t in project.sprint.tickets} == {""}, "the sprint is everyone's"


def test_work_read_in_part_is_incomplete() -> None:
    fake = FakeJiraWork()
    fake.complete = False
    assert work_of(fake({})).projects[0].complete is False


def test_projects_not_linked_or_not_read_are_kept_as_unknown() -> None:
    fake = FakeJiraWork()
    fake.projects = False
    fake.not_linked = ["Payroll"]
    fake.unreadable = [{"name": "Ops", "key": "OPS", "reason": "Jira didn't answer"}]
    work = work_of(fake({"leave_id": "lv-9"}))
    assert work is not None and work.projects == []
    assert work.not_linked == ["Payroll"] and work.unreadable == [("Ops", "Jira didn't answer")]


def test_an_answer_naming_no_project_is_jira_not_in_use() -> None:
    fake = FakeJiraWork()
    fake.projects = False
    assert work_of(fake({})) is None
    assert work_of([]) is None and work_of("Service unavailable") is None


# -- the heads-up when the dates are named -----------------------------------------------


def test_the_heads_up_names_only_the_tickets_the_leave_touches() -> None:
    said = own_heads_up(_around_the_leave(), "Ravi", "", NOV12, NOV12, today=FAR)
    assert said == (
        "Heads-up: you have 2 open Jira tickets to hand over before you go: ECP-1 Task ECP-1 "
        "and ECP-2 Task ECP-2. You can still apply."
    )


def test_the_heads_up_says_a_handover_not_a_due_date_for_overdue_and_undated_tickets() -> None:
    soon = own_heads_up(_around_the_leave(), "Ravi", "", NOV12, NOV12, today=SOON, limit=400)
    assert "ECP-5 Task ECP-5" in soon  # overdue
    assert soon.startswith("Heads-up: you have 5 open Jira tickets to hand over before you go:")
    assert "due around" not in soon


def test_the_heads_up_drops_the_tickets_then_the_close_then_itself_rather_than_be_cut() -> None:
    lead = "Heads-up: you have 2 open Jira tickets to hand over before you go"
    work = _around_the_leave()
    assert own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR, limit=len(lead) + 22) == (
        f"{lead}. You can still apply."
    )
    assert own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR, limit=len(lead) + 1) == (
        f"{lead}."
    )
    assert own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR, limit=len(lead)) == ""


def test_no_heads_up_when_the_leave_touches_none_of_theirs() -> None:
    far_off = [ProjectWork("ECP", open_tickets=[_t("ECP-3", "Ravi", due=date(2026, 11, 18))])]
    assert own_heads_up(far_off, "Ravi", "", NOV12, NOV12, today=FAR) == ""
    assert own_heads_up(_around_the_leave("Bala"), "Ravi", "", NOV12, NOV12, today=FAR) == ""


def test_the_heads_up_lists_fewer_and_shorter_rather_than_be_cut() -> None:
    long = "Migrate the billing exports to the new warehouse and retire the old cron jobs"
    work = [ProjectWork("ECP", open_tickets=[
        Ticket(key=f"ECP-{i}", summary=long, status="To Do", category="new",
               assignee_name="Ravi", due=NOV12)
        for i in range(1, 6)
    ])]
    roomy = own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR, limit=400)
    assert "ECP-1 Migrate the billing exports to the new… and" not in roomy
    assert "ECP-3 Migrate the billing exports to the new… and 2 more." in roomy
    for limit in (220, 160, 120):
        said = own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR, limit=limit)
        assert len(said) <= limit and said.startswith("Heads-up: you have 5 open Jira tickets")
        assert said.endswith("You can still apply.")


# -- the whole check -------------------------------------------------------------------


@pytest.fixture()
def jira() -> FakeJiraWork:
    return FakeJiraWork()


def _jira_reads(hrms: FakeHrms) -> list[dict[str, Any]]:
    return [c["arguments"] for c in hrms.calls() if c["name"] == "get_jira_work"]


@pytest.fixture()
def checked(jira: FakeJiraWork):
    hrms = FakeHrms()
    hrms.results["get_user_session"] = {"authenticated": True, "name": "Ravi",
                                        "company_email": "ravi@acme.test"}
    hrms.results["get_jira_work"] = jira
    hrms.results["preview_leave"] = {
        "effective_days": 1, "created": False,
        "facts": {"team_leaves": {"team_size": 4, "overlap_count": 1, "on_leave_names": ["Anu"]}},
        "briefing": [
            "Team: 1 teammate(s) on leave that day: [Anu].",
            "Jira: Ticket ECP-2 is due on 2026-10-02.",
            "Balance: 10.0 Casual Leave day(s) available; this request uses 1.0.",
        ],
    }
    app = create_app(_settings(), hrms_transport=httpx.MockTransport(hrms.handler))
    with TestClient(app, base_url="http://hrms-mcp:8000") as client:
        yield client, hrms


def _preview(client: TestClient) -> str:
    return text(call(client, "preview_leave", {
        "leave_type": "Casual Leave", "date_from": "2026-10-02", "day_portion": "Full Day",
    }))


def test_the_check_warns_once_with_the_teammate_the_sprint_and_the_persons_tickets(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    said = _preview(client)
    warning = said.splitlines()[0]
    assert warning == (
        "Heads-up: Anu has already applied for leave on the same day. ECP's sprint "
        "“Sprint 14” ends on Mon 5 Oct with 6 of 10 tickets still to "
        "do or in progress, including Anu's ECP-1 Task ECP-1 (In Progress). You have 1 open "
        "Jira ticket to hand over before you go: ECP-2 Task ECP-2 (In Progress, no due date; "
        "sprint “Sprint 14” ends Mon 5 Oct, the day you're back). Do you still want to proceed?"
    )
    assert "Jira: Ticket" not in said, "said once, in the warning"
    assert "Balance: 10.0" in said and "Nothing is filed until you confirm." in said
    assert _jira_reads(hrms) == [{}], "the person's own work, asked of the HRMS"


def test_a_jira_that_does_not_answer_leaves_the_hrms_check_as_it_was(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    hrms.errors["get_jira_work"] = "Jira is not answering."
    said = _preview(client)
    assert said.splitlines()[0] == (
        "Heads-up: Anu has already applied for leave on the same day. "
        "Do you still want to proceed?"
    )
    assert "Jira: Ticket ECP-2 is due on 2026-10-02." in said, "the HRMS's own Jira line stays"


def test_an_hrms_that_reads_no_jira_for_the_person_leaves_the_check_as_it_was(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    jira.projects = False
    said = _preview(client)
    assert "Jira ticket" not in said.splitlines()[0]
    assert "Jira: Ticket ECP-2 is due on 2026-10-02." in said


def test_the_hrms_jira_lines_about_tickets_not_named_stay_for_the_applicant(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    hrms.results["preview_leave"]["briefing"] += [
        "Jira: Ticket PAY-7 is due on 2026-10-02.",
        "Jira: Ticket ECP-12 is due on 2026-10-02.",
        "Jira: 2 open tickets due this week.",
    ]
    said = _preview(client)
    assert "Jira: Ticket ECP-2 " not in said, "ECP-2 was named in the warning"
    assert "Jira: Ticket ECP-12 is due on 2026-10-02." in said, "not found as theirs here"
    assert "Jira: Ticket PAY-7 is due on 2026-10-02." in said, "their own, in another project"
    assert "Jira: 2 open tickets due this week." in said, "its projects are unknown"


def test_the_employee_is_shown_each_managers_pending_list_as_the_hrms_gives_it(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    pending = (
        "_Priya will see:_\n*Pending in Jira — Project A (1)* — open tickets assigned to Ravi:\n"
        "• *ECP-2* Task ECP-2 — In Progress\n\n"
        "_Arun will see:_\n*Pending in Jira:* no open tickets in Project B assigned to Ravi."
    )
    hrms.results["preview_leave"]["pending_jira"] = pending
    assert pending in _preview(client)


# -- a project manager asking about someone's leave -----------------------------------------


@pytest.fixture()
def managing(jira: FakeJiraWork):
    hrms = FakeHrms()
    hrms.results["get_user_session"] = {"authenticated": True, "name": "Priya"}
    hrms.results["get_jira_work"] = jira
    hrms.results["get_leave_routing"] = {
        "leave_id": "lv-9", "employee": "Anu",
        "briefing": [
            'Calendar: "Payments sync" (10:00) — you attend.',
            "Team: 1 teammate(s) already on leave that day: [Bala].",
            "Jira: Ticket ECP-1 is due on 2026-10-01.",
            "Balance: 9.0 Casual Leave day(s) available; this request uses 1.0.",
        ],
    }
    app = create_app(_settings(), hrms_transport=httpx.MockTransport(hrms.handler))
    with TestClient(app, base_url="http://hrms-mcp:8000") as client:
        yield client, hrms


def _impact(client: TestClient, request_id: str = "lv-9") -> dict:
    return call(client, "get_leave_request_impact", {"request_id": request_id})


def test_a_manager_asking_about_a_request_is_told_what_the_applicant_leaves_undone(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    assert text(_impact(client)).splitlines() == [
        "Anu — Casual leave, Thu 1 Oct 2026, full day, Pending. Reason: family function.",
        "Anu has 1 open Jira ticket in ECP, affected by this leave: ECP-1 Task ECP-1 (In "
        "Progress, no due date; sprint “Sprint 14” ends Mon 5 Oct, just after they're back).",
        "Only the Jira projects you decide this leave for were checked (ECP); any other "
        "projects Anu works on were not.",
        "ECP's sprint “Sprint 14” ends on Mon 5 Oct with 6 of 10 tickets still to do or in "
        "progress, including Anu's ECP-1 Task ECP-1 (In Progress).",
        'Calendar: "Payments sync" (10:00) — they attend.',
        "Team: 1 teammate(s) already on leave that day: [Bala].",
        "Balance: 9.0 Casual Leave day(s) available; this request uses 1.0.",
    ]
    assert _jira_reads(hrms) == [{"leave_id": "lv-9"}], "the applicant's, as the manager sees it"


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
    names = [c["name"] for c in hrms.calls()]
    assert "get_leave_routing" not in names and "get_jira_work" not in names


def test_someone_who_manages_nobody_cannot_ask_about_others_leave(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    hrms.errors["get_team_leaves"] = "access denied: only project managers and admins can view"
    assert "only project managers and HR" in text(_impact(client))


def test_the_hrms_jira_lines_about_other_projects_are_never_shown_to_the_manager(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    """Project A's manager is told of Project A's work only: a line about a
    ticket of a project they do not decide on goes, even if the HRMS sends it."""
    client, hrms = managing
    hrms.results["get_leave_routing"]["briefing"] += [
        "Jira: Ticket PAY-7 is due on 2026-10-02.",
        "Jira: Ticket ECP-12 is due on 2026-10-02.",
        "Jira: 2 open tickets due this week.",
    ]
    said = text(_impact(client))
    assert "Jira: Ticket ECP-1 " not in said, "ECP-1 was named above"
    assert "PAY-7" not in said, "another project's ticket"
    assert "Jira: Ticket ECP-12 is due on 2026-10-02." in said, "their project, not named above"
    assert "Jira: 2 open tickets due this week." in said, "names no ticket"


def test_without_jira_the_manager_is_told_it_was_not_checked_and_keeps_the_hrms_lines(
    managing: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = managing
    hrms.errors["get_jira_work"] = "Jira is not answering."
    said = text(_impact(client))
    assert "Jira was not checked, so their open tickets are unknown." in said
    assert "Jira: Ticket ECP-1 is due on 2026-10-01." in said


def test_a_managers_project_not_linked_or_not_read_is_unknown_never_none(
    managing: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = managing
    jira.projects = False
    jira.not_linked = ["Payroll"]
    jira.unreadable = [{"name": "Ops", "key": "OPS", "reason": "Jira didn't answer"}]
    lines = text(_impact(client)).splitlines()
    assert "Payroll is not linked to Jira, so Anu's tickets there could not be checked." in lines
    assert "Anu's tickets in Ops could not be checked: Jira didn't answer." in lines
    assert not any("has no open Jira tickets" in line for line in lines)


def test_hr_is_not_told_that_other_projects_were_left_out(
    managing: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = managing
    jira.view = "all"
    said = text(_impact(client))
    assert "Anu has 1 open Jira ticket in ECP" in said
    assert "Only the Jira projects" not in said, "HR sees every project of theirs"


def test_the_hrms_jira_lines_stay_when_no_project_of_the_managers_was_read(
    managing: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    """Nothing read here to narrow by: the HRMS's briefing, already narrowed to
    the manager's projects, is all there is."""
    client, _ = managing
    jira.projects = False
    jira.unreadable = [{"name": "EC Platform", "key": "ECP", "reason": "Jira didn't answer"}]
    said = text(_impact(client))
    assert "Anu's tickets in EC Platform could not be checked: Jira didn't answer." in said
    assert "Jira: Ticket ECP-1 is due on 2026-10-01." in said


def test_the_applicants_tickets_are_found_by_email_when_no_jira_account_is_given(
    managing: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    """Anu is "anu.r" in Jira: her email finds her tickets where her name does not."""
    client, hrms = managing
    hrms.results["resolve_employee"] = [{"name": "Anu", "company_email": "anu@acme.test"}]
    jira.theirs = [{**_ticket("ECP-1", "anu.r"), "assignee_email": "anu@acme.test"}]

    def without_account(arguments: dict[str, Any]) -> dict[str, Any]:
        answer = jira(arguments)
        for project in answer["projects"]:
            project.pop("account_id", None)
        return {**answer, "account_id": ""}

    hrms.results["get_jira_work"] = without_account
    assert "Anu has 1 open Jira ticket in ECP" in text(_impact(client))


def test_the_hrms_jira_lines_of_a_project_not_read_this_time_stay(
    managing: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    """Payroll's Jira did not answer: the HRMS's own line about PAY-7 is all
    that is said of its tickets, so it stays."""
    client, hrms = managing
    jira.unreadable = [{"name": "Payroll", "key": "PAY", "reason": "Jira didn't answer"}]
    hrms.results["get_leave_routing"]["briefing"].append("Jira: Ticket PAY-7 is due on 2026-10-02.")
    assert "Jira: Ticket PAY-7 is due on 2026-10-02." in text(_impact(client))


def test_the_employee_is_warned_of_tickets_jira_files_under_another_name(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    jira.mine = [_ticket("ECP-3", "ravi.k")]
    assert "You have 1 open Jira ticket to hand over before you go: ECP-3 Task" in _preview(client)


def test_open_tickets_the_leave_does_not_touch_are_a_plain_line_not_a_warning(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    jira.mine = [_ticket("ECP-20", "Ravi", "new")]
    said = _preview(client)
    warning, head, note = said.splitlines()[:3]
    assert "Jira ticket" not in warning
    assert head.startswith("Casual leave on Fri 2 Oct 2026")
    assert note == "Your 1 open Jira ticket is not due around your leave."


def _start(client: TestClient, **given: str) -> dict:
    return call(client, "start_leave_request", {"date_from": "2026-10-02", **given})[
        "_meta"]["ec/form"]


def test_the_employee_is_told_of_their_tickets_as_soon_as_they_name_the_date(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, _ = checked
    form = _start(client)
    assert form["prompt"] == (
        "Heads-up: you have an open Jira ticket to hand over before you go: ECP-2 Task ECP-2. "
        "You can still apply.\n\n"
        "Leave for Fri 2 Oct 2026 — choose the rest:"
    )
    assert len(form["prompt"]) <= PROMPT_LIMIT
    assert [q["name"] for q in form["questions"]] == ["leave_type", "day_portion", "reason"]


def test_a_request_with_nothing_to_choose_leaves_the_heads_up_to_the_check(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, _ = checked
    form = _start(client, leave_type="Casual Leave", day_portion="Full Day", reason="family")
    assert form["prompt"] == "Casual leave for Fri 2 Oct 2026, full day."


def test_a_jira_that_does_not_answer_still_starts_the_request(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    hrms.errors["get_jira_work"] = "Jira is not answering."
    assert _start(client)["prompt"] == "Leave for Fri 2 Oct 2026 — choose the rest:"


def test_an_hrms_that_answers_in_plain_text_still_starts_the_request(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    hrms.results["get_user_session"] = "Service temporarily unavailable"
    hrms.results["get_jira_work"] = "Service temporarily unavailable"
    assert _start(client)["prompt"] == "Leave for Fri 2 Oct 2026 — choose the rest:"


def test_a_slow_jira_does_not_hold_up_the_form(
    checked: tuple[TestClient, FakeHrms], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = checked

    async def never(*_: Any, **__: Any) -> str:
        await asyncio.sleep(3600)
        return "never said"

    monkeypatch.setattr(forms, "own_heads_up", never)
    monkeypatch.setattr(forms, "HEADS_UP_WAIT_SECONDS", 0.05)
    assert _start(client)["prompt"] == "Leave for Fri 2 Oct 2026 — choose the rest:"


def test_a_heads_up_asked_again_reuses_what_jira_said(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    assert _start(client)["prompt"].startswith("Heads-up:")
    assert _start(client)["prompt"].startswith("Heads-up:")
    assert len(_jira_reads(hrms)) == 1


def test_the_check_reads_jira_afresh_so_a_ticket_closed_since_is_not_warned_of(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    assert "ECP-2" in _start(client)["prompt"]
    jira.mine = []  # ECP-2 closed in Jira since
    assert "Jira ticket" not in _preview(client)


def test_a_request_with_nothing_to_choose_does_not_read_jira_for_a_heads_up(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    _start(client, leave_type="Casual Leave", day_portion="Full Day", reason="family")
    assert _jira_reads(hrms) == []


def test_several_managers_leave_nothing_to_choose_so_no_heads_up_is_read(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    """Every one of them approves, so who approves is never asked: with the
    rest named, the check says the tickets once, in full."""
    client, hrms = checked
    hrms.results["get_employee_project_managers"] = {
        "count": 2,
        "project_managers": [{"id": "pm-1", "name": "Priya"}, {"id": "pm-2", "name": "Kiran"}],
    }
    form = _start(client, leave_type="Casual Leave", day_portion="Full Day", reason="family")
    assert form["questions"] == []
    assert _jira_reads(hrms) == []


def test_a_slow_heads_up_is_read_to_the_end_for_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forms, "HEADS_UP_WAIT_SECONDS", 0.02)

    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "Heads-up: late."

    async def run() -> tuple[Any, asyncio.Task[str]]:
        reading = asyncio.create_task(slow())
        said = await forms._heads_up(reading)
        await asyncio.sleep(0.15)
        return said, reading

    said, reading = asyncio.run(run())
    assert said is None
    assert not reading.cancelled() and reading.result() == "Heads-up: late."


def test_a_heads_up_read_is_kept_going_when_the_form_is_called_off() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "Heads-up: late."

    async def run() -> asyncio.Task[str]:
        reading = asyncio.create_task(slow())
        waiting = asyncio.create_task(forms._heads_up(reading))
        await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert reading in forms._LATE
        await asyncio.sleep(0.15)
        return reading

    reading = asyncio.run(run())
    assert reading.result() == "Heads-up: late."
    assert reading not in forms._LATE


def test_an_answer_the_hrms_did_not_give_is_asked_again(
    checked: tuple[TestClient, FakeHrms],
) -> None:
    client, hrms = checked
    hrms.errors["get_jira_work"] = "Jira is not answering."
    assert not _start(client)["prompt"].startswith("Heads-up:")
    del hrms.errors["get_jira_work"]
    assert _start(client)["prompt"].startswith("Heads-up:"), "a failure is not kept"


def _long_tickets(due: str) -> list[dict[str, Any]]:
    return [
        _ticket(f"ECP-{i}", "Ravi", due=due,
                summary="Migrate the billing exports to the new warehouse " * 2)
        for i in range(20, 26)
    ]


def test_the_heads_up_is_fitted_to_the_prompt_it_sits_above(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    jira.mine = _long_tickets("2026-10-02")
    short = _start(client)
    full = _start(client, leave_type="Floating Leave", day_portion="Second Half")
    assert len(short["prompt"]) <= PROMPT_LIMIT and len(full["prompt"]) <= PROMPT_LIMIT
    # A shorter prompt leaves more room, and the heads-up takes it.
    assert len(short["prompt"].split("\n\n")[0]) > len(full["prompt"].split("\n\n")[0])


def test_a_failing_heads_up_is_said_as_none_not_raised(
    checked: tuple[TestClient, FakeHrms], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = checked

    async def broken(*_: Any, **__: Any) -> str:
        raise RuntimeError("odd answer")

    monkeypatch.setattr(forms, "own_heads_up", broken)
    assert _start(client)["prompt"] == "Leave for Fri 2 Oct 2026 — choose the rest:"


def test_the_heads_up_counts_at_least_when_a_project_was_read_in_part() -> None:
    work = [ProjectWork("ECP", open_tickets=[_t("ECP-1", "Ravi", due=NOV12)], complete=False)]
    said = own_heads_up(work, "Ravi", "", NOV12, NOV12, today=FAR)
    assert said.startswith("Heads-up: you have at least 1 open Jira ticket to hand over")


def test_the_heads_up_fits_beside_the_longest_prompt(
    checked: tuple[TestClient, FakeHrms], jira: FakeJiraWork
) -> None:
    client, _ = checked
    jira.mine = _long_tickets("2026-10-07")
    form = _start(client, date_to="2026-10-07", leave_type="Floating Leave",
                  day_portion="Second Half")
    assert form["prompt"].startswith("Heads-up: you have 6 open Jira tickets")
    assert len(form["prompt"]) <= PROMPT_LIMIT


# -- reading through the HRMS, in time --------------------------------------------------


class _SlowHrms:
    """Answers get_jira_work after ``delay`` seconds, counting the calls."""

    def __init__(self, delay: float = 0.0) -> None:
        self.settings = _settings(jira_wait_seconds=0.05)
        self.delay = delay
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], identity: dict[str, Any]) -> Any:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return FakeJiraWork()(arguments)


def _ctx(identity: dict[str, Any]) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(params={"_identity": identity}))


def test_a_slow_jira_is_skipped_rather_than_hold_up_the_check() -> None:
    reader = JiraReader(_SlowHrms(delay=1.0))  # type: ignore[arg-type]
    assert asyncio.run(reader.read(_ctx({"user_id": "emp-1"}))) is None


def test_only_the_askers_own_work_is_kept_and_only_for_someone_named() -> None:
    hrms = _SlowHrms()
    reader = JiraReader(hrms)  # type: ignore[arg-type]

    async def run() -> None:
        named, nameless = _ctx({"user_id": "emp-1"}), _ctx({"signature": "c0ffee"})
        await reader.read(named, reuse=True)
        await reader.read(named, reuse=True)
        assert hrms.calls == 1, "the asker's own work is reused"
        await reader.read(named, leave_id="lv-9", reuse=True)
        await reader.read(named, leave_id="lv-9", reuse=True)
        assert hrms.calls == 3, "a leave of someone else's is read afresh"
        await reader.read(nameless, reuse=True)
        await reader.read(nameless, reuse=True)
        assert hrms.calls == 5, "no one named, nothing kept to give to another"

    asyncio.run(run())


class _BrokenHrms(_SlowHrms):
    async def call(self, tool: str, arguments: dict[str, Any], identity: dict[str, Any]) -> Any:
        raise AttributeError("'list' object has no attribute 'get'")


def test_an_hrms_answer_not_of_the_shape_asked_for_skips_jira_rather_than_fail() -> None:
    reader = JiraReader(_BrokenHrms())  # type: ignore[arg-type]
    assert asyncio.run(reader.read(_ctx({"user_id": "emp-1"}))) is None
