"""A month's leave and holidays for a list of people: the client timesheet's read.

Enterprise Claw's client timesheet (its docs/design/client-timesheet.md, step 3)
asks for one month: whose leave fell on which day, full or half, and the
company's holidays, for the employees on its roster, named by work email. It
reads the answer as data (``structuredContent``), never as words a model
restates.

**Only an HRMS administrator sees anyone's leave.** Anyone else is told, for
every person, that their leave is **not visible**, never that they have none: a
day off read as a missing day would be the worse mistake.

A project manager is not given their team's. The HRMS answers "who are this
employee's project managers?" about whoever *asks*, overwriting the employee id
with the verified identity, so it can't say whom a manager manages; and its team
leave lists only people who took leave, so someone with none would look the same
as someone outside the team (measured against the live HRMS, 2026-10-28).

Leave is the HRMS's **approved or pending** team leave (``get_team_leaves``, which
answers HR with everyone's), counted into days by the same rules as the leave
trend (``trend.days_of``), so the two never disagree about what a day of leave
is. The client timesheet uses it to check that leave its employees asked for in
the client's own leave channel is also on the HRMS, where a request still
waiting for a decision counts as applied (0.26.0). Holidays say whether they
are public or optional.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp import trend
from hrms_mcp.hrms import Hrms, HrmsError
from hrms_mcp.tools import _ask, _identity, kind_of

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

#: The statuses that count as applied: a decision is not waited for.
COUNTED = ("Approved", "Pending")

#: A roster is tens of people; a list far longer than this is not one.
MOST_PEOPLE = 200


def month_of(value: str) -> tuple[date, date]:
    """``2026-10`` → its first and last day; anything else refused with why."""
    try:
        year, number = (int(part) for part in value.strip().split("-")[:2])
        first = date(year, number, 1)
    except ValueError:
        raise ToolError(f"month = {value}: give it as 2026-10") from None
    return first, first.replace(day=calendar.monthrange(year, number)[1])


def _leave(record: dict[str, Any]) -> trend.Leave | None:
    """A team-leave record as a leave, or None when its dates can't be read."""
    try:
        start = date.fromisoformat(str(record.get("date_from", ""))[:10])
        end_text = str(record.get("date_to") or "")[:10]
        end = date.fromisoformat(end_text) if end_text else start
    except ValueError:
        return None
    mode = " ".join(str(record.get("mode") or "").replace("_", " ").split()).lower()
    return trend.Leave(
        kind=kind_of(str(record.get("type") or "")),
        start=start,
        end=max(start, end),
        half=mode in ("first half", "second half"),
        status=str(record.get("status") or ""),
    )


async def _person(hrms: Hrms, ctx: Context, email: str) -> dict[str, Any] | None:
    """The one employee whose work email this is, or None."""
    try:
        found = await hrms.call("resolve_employee", {"query": email}, _identity(ctx))
    except HrmsError:
        return None
    wanted = email.strip().casefold()
    exact = [
        person for person in found or []
        if isinstance(person, dict) and person.get("id")
        and str(person.get("company_email") or "").casefold() == wanted
    ]
    return exact[0] if len(exact) == 1 else None


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=READ_ONLY)
    async def get_leave_calendar(month: str, emails: list[str], ctx: Context) -> CallToolResult:
        """One month's approved or pending leave, day by day, and the company's
        holidays, for the employees named by work email. For the platform's client
        timesheet: it reads the structured result, not the words. month is 2026-10. Only an HRMS
        administrator sees anyone's leave; for anyone else every person is "not
        visible", never "no leave"."""
        first, last = month_of(month)
        wanted = list(dict.fromkeys(e.strip().casefold() for e in emails if e.strip()))
        if len(wanted) > MOST_PEOPLE:
            raise ToolError(f"at most {MOST_PEOPLE} people at once")
        session = await _ask(hrms, ctx, "get_user_session", {})
        if not isinstance(session, dict) or not session.get("employee_id"):
            raise ToolError("The HRMS could not say who is asking, so no leave is given.")
        admin = session.get("role") == "admin"

        holidays_found = await _ask(
            hrms, ctx, "get_holidays", {"year": str(first.year), "month": str(first.month)}
        )
        holidays = [
            {
                "date": str(item.get("date", ""))[:10],
                "name": str(item.get("name", "")),
                "public": str(item.get("type", "public")).casefold() != "optional",
            }
            for item in holidays_found or [] if isinstance(item, dict)
        ]
        off_days = trend.holidays_of(holidays)

        people = dict(zip(wanted, await asyncio.gather(
            *(_person(hrms, ctx, email) for email in wanted)
        ), strict=True))
        found = {email: str(p["id"]) for email, p in people.items() if p}
        visible = set(found) if admin else set()

        by_employee: dict[str, dict[date, tuple[str, float]]] = {}
        if visible:
            try:
                # Approved and pending, one read each: a request still waiting
                # for a decision is applied. One leave read twice is counted
                # once, by its id.
                records = []
                for status in COUNTED:
                    records += await hrms.call(
                        "get_team_leaves", {"status": status}, _identity(ctx)
                    ) or []
            except HrmsError as error:
                if "access denied" not in str(error).lower():
                    raise ToolError(str(error)) from None
                records, visible = [], set()
            counted: set[str] = set()
            for record in records:
                if not isinstance(record, dict) or str(record.get("status")) not in COUNTED:
                    continue
                key = str(record.get("leave_id") or id(record))
                if key in counted:
                    continue
                counted.add(key)
                leave = _leave(record)
                if leave is None or not trend.overlaps(leave, first, last):
                    continue
                days = by_employee.setdefault(str(record.get("employee_id") or ""), {})
                for day, portion in trend.days_of(leave, off_days).items():
                    if first <= day <= last:
                        _, before = days.get(day, ("", 0.0))
                        days[day] = (leave.kind, min(1.0, before + portion))

        result: dict[str, Any] = {}
        for email in wanted:
            employee = found.get(email)
            seen = email in visible
            days = by_employee.get(employee or "", {}) if seen else {}
            result[email] = {
                "found": employee is not None,
                "visible": seen,
                "days": {
                    day.isoformat(): {"type": kind, "portion": portion}
                    for day, (kind, portion) in sorted(days.items())
                },
            }
        public = sum(1 for h in holidays if h["public"])
        said = (
            f"Leave for {first:%B %Y}: {len(found)} of {len(wanted)} people found in the HRMS, "
            f"{len(visible)} visible to you; {public} public holiday{'s' * (public != 1)}."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=said)],
            structuredContent={
                "month": f"{first:%Y-%m}", "holidays": holidays, "people": result,
            },
        )
