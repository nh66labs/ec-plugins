"""get_leave_calendar: a month's leave and holidays for the client timesheet.

The HRMS decides who may see whose leave: an administrator everyone, a project
manager the people they manage, anyone else no one. Someone not visible is never
reported as having no leave. Days are counted as the leave trend counts them.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

ANU = {"id": "emp-7", "name": "Anu Thomas", "employee_id": "H032",
       "company_email": "anu@acme.test"}
RAVI = {"id": "emp-9", "name": "Ravi K", "employee_id": "H040",
        "company_email": "ravi.k@acme.test"}
PEOPLE = {"anu@acme.test": ANU, "ravi.k@acme.test": RAVI}


def _leave(employee: str, day_from: str, day_to: str = "", kind: str = "Casual Leave",
           mode: str = "Full Day", status: str = "Approved") -> dict[str, Any]:
    return {"leave_id": f"lv-{employee}-{day_from}", "employee_id": employee, "type": kind,
            "mode": mode, "date_from": day_from, "date_to": day_to or day_from,
            "status": status, "reason": "private"}


def _hrms_for(fake: FakeHrms, role: str, manages: tuple[str, ...] = ()) -> None:
    fake.results["get_user_session"] = {
        **fake.results["get_user_session"], "role": role, "employee_id": "emp-0042",
    }
    fake.results["resolve_employee"] = lambda args: (
        [PEOPLE[args["query"]]] if args["query"] in PEOPLE else []
    )
    fake.results["get_employee_project_managers"] = lambda args: {
        "project_managers": (
            [{"id": "emp-0042", "name": "Ravi"}] if args["employee_id"] in manages else
            [{"id": "pm-1", "name": "Priya"}]
        )
    }
    fake.results["get_holidays"] = [
        {"date": "2026-10-02", "name": "Gandhi Jayanti", "type": "Public"},
        {"date": "2026-10-20", "name": "Dussehra", "type": "Optional"},
    ]
    fake.results["get_team_leaves"] = [
        _leave("emp-7", "2026-10-05", "2026-10-07"),
        _leave("emp-7", "2026-10-14", kind="Sick Leave", mode="Second Half"),
        # Across the start of the month, and over the public holiday on the 2nd.
        _leave("emp-9", "2026-09-30", "2026-10-02"),
        _leave("emp-9", "2026-10-21", status="Pending"),
    ]


def _calendar(server: TestClient, emails: list[str], month: str = "2026-10") -> dict[str, Any]:
    result = call(server, "get_leave_calendar", {"month": month, "emails": emails})
    assert not result.get("isError"), text(result)
    return result


def test_an_hrms_admin_sees_everyones_leave_day_by_day(client, hrms) -> None:  # noqa: F811
    _hrms_for(hrms, "admin")
    result = _calendar(client, ["ANU@acme.test", "ravi.k@acme.test"])
    data = result["structuredContent"]
    assert data["month"] == "2026-10"
    assert data["holidays"] == [
        {"date": "2026-10-02", "name": "Gandhi Jayanti", "public": True},
        {"date": "2026-10-20", "name": "Dussehra", "public": False},
    ]
    anu = data["people"]["anu@acme.test"]
    assert (anu["found"], anu["visible"]) == (True, True)
    assert anu["days"] == {
        "2026-10-05": {"type": "Casual leave", "portion": 1.0},
        "2026-10-06": {"type": "Casual leave", "portion": 1.0},
        "2026-10-07": {"type": "Casual leave", "portion": 1.0},
        "2026-10-14": {"type": "Sick leave", "portion": 0.5},
    }
    # 30 Sep is September's; 2 Oct is a holiday, not leave; a pending request is
    # applied, so it counts (0.25.0).
    assert data["people"]["ravi.k@acme.test"]["days"] == {
        "2026-10-01": {"type": "Casual leave", "portion": 1.0},
        "2026-10-21": {"type": "Casual leave", "portion": 1.0},
    }
    assert text(result) == (
        "Leave for October 2026: 2 of 2 people found in the HRMS, 2 visible to you; "
        "1 public holiday."
    )


def test_anyone_but_an_admin_sees_no_one_and_reads_no_leave(client, hrms) -> None:  # noqa: F811
    """A project manager included: the HRMS can't say whom they manage (it answers
    about whoever asks), so their team's leave isn't given, and nobody's day is
    reported as leave-free."""
    _hrms_for(hrms, "employee", manages=("emp-7",))
    people = _calendar(client, ["anu@acme.test", "ravi.k@acme.test"])["structuredContent"]["people"]
    assert people["anu@acme.test"] == {"found": True, "visible": False, "days": {}}
    assert people["ravi.k@acme.test"] == {"found": True, "visible": False, "days": {}}
    names = {c["name"] for c in hrms.calls()}
    assert "get_team_leaves" not in names and "get_employee_project_managers" not in names


def test_an_email_the_hrms_doesnt_know_is_not_found(client, hrms) -> None:  # noqa: F811
    _hrms_for(hrms, "admin")
    people = _calendar(client, ["nobody@acme.test"])["structuredContent"]["people"]
    assert people["nobody@acme.test"] == {"found": False, "visible": False, "days": {}}


def test_a_month_that_isnt_one_is_refused_before_the_hrms(client, hrms) -> None:  # noqa: F811
    _hrms_for(hrms, "admin")
    result = call(client, "get_leave_calendar", {"month": "October", "emails": ["anu@acme.test"]})
    assert result.get("isError") and "give it as 2026-10" in text(result)
    assert hrms.calls() == []


def test_the_calendar_is_read_only(client) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    tool = tools["get_leave_calendar"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert set(tool["inputSchema"]["properties"]) == {"month", "emails"}
