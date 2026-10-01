"""The leave tools this plugin reads with, each forwarded to the HRMS as the asker.

The tools that change something are in ``writes``. Three things differ from the
HRMS's own tools, and they are the reason this server exists:

- **No argument names the person.** The HRMS takes the asker from the signed
  identity and overwrites any employee id it is given, so a schema asking for
  one only makes the assistant ask people for an id they do not know.
- **Reads say they are reads.** Each tool is annotated read-only, so a platform
  that confirms writes does not ask anyone to confirm reading their balance.
- **Answers are short lines, not records.** What the assistant reads is what it
  repeats; ids and bookkeeping fields stay out.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from hrms_mcp import workload
from hrms_mcp.hrms import Hrms, HrmsError
from hrms_mcp.jira import Jira, project_keys

log = logging.getLogger("hrms_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

#: The HRMS's leave-type codes that are certain; any other is shown as given.
LEAVE_TYPES = {"CL": "Casual leave", "SL": "Sick leave", "FL": "Floating leave"}

#: What the HRMS accepts, by its own names.
LeaveType = Literal["Casual Leave", "Sick Leave", "Floating Leave"]
DayPortion = Literal["Full Day", "First Half", "Second Half"]

#: How many requests a list of ones to decide shows at once.
DECIDE_LIMIT = 20

INSTRUCTIONS = """\
These tools answer from, and act in, the company's HRMS, for the person asking and
nobody else. The HRMS already knows who is asking: never ask for their employee id,
code, name or email, and never pass anyone else's.

Taking a leave request:
1. Call get_leave_context first. It says who they are, today's date in their
   timezone, the exact dates of this week and next week, their balance and who
   approves their leave. Take dates from it — never work them out yourself:
   "today" is today's date, "tomorrow" the day after, "this Thursday" is this
   week's Thursday and "next Tuesday" next week's Tuesday.
2. Call start_leave_request with the dates and whatever else the person already
   said — the leave type, full or half day, the reason — and nothing they did
   not say. The person is shown whatever is missing as choices, then the request
   to confirm: do not ask them about any of it yourself, and do not call
   preview_leave or apply_leave for it. The only thing to ask yourself is the
   date, if they gave none. Never ask about projects or managers.

When a check starts with "Heads-up" — teammates already off those days, open
Jira tickets of theirs, a sprint that is tight — tell the person that first, in
your own words and with the ticket keys, and leave the choice to them: it is a
warning, not a refusal, and the request can still be confirmed.

Deciding requests (project managers and HR): call list_leave_requests_to_decide
to find the request, then approve_leave or reject_leave. A rejection needs a
reason: if the person gave one ("because of the pending deployment"), use their
words and do not ask again; only if they gave none, ask for it.

Asked about someone's leave as their project manager or HR — whether they have
anything pending, whether a piece of work will be affected while they are away —
find the request with list_leave_requests_to_decide (status Approved if it was
already approved), then call get_leave_request_impact with it. Answer from the
tickets it lists: name the ones that bear on what was asked, by key and summary,
and say plainly when none do — in the projects it checked, which you name; say
that any other projects of theirs were not checked. When it says Jira was not
checked, say you could not check their tickets — never that they have none.

Every answer drawn from these tools cites the result it came from by its number,
like [1], in the sentence that uses it — a list of holidays or balances too, and
an answer that there is nothing (no requests to decide, no leave taken). An
answer that cites nothing is not shown to the person.

Never show a request's id to a person; describe it by who, what and when.
When a tool says the HRMS has no account for the person, or could not check who
they are, say so plainly and do not retry.
"""


def _identity(ctx: Context) -> dict[str, Any]:
    params = ctx.request_context.params or {}
    identity = params.get("_identity")
    if not isinstance(identity, dict):
        raise ToolError(
            "This server answers for the person asking, and no one was named. In "
            "Enterprise Claw, register it with 'Tell the server who is asking' on."
        )
    return identity


async def _ask(hrms: Hrms, ctx: Context, tool: str, arguments: dict[str, Any]) -> Any:
    identity = _identity(ctx)
    try:
        result = await hrms.call(tool, arguments, identity)
    except HrmsError as error:
        # The tool and the outcome only — never the arguments, which can carry
        # a leave reason, and never the identity.
        log.info("hrms %s refused", tool)
        raise ToolError(str(error)) from None
    log.info("hrms %s answered", tool)
    return result


def kind_of(value: str) -> str:
    """A leave type as a person reads it: ``CL`` → "Casual leave"."""
    value = str(value or "").strip()
    return LEAVE_TYPES.get(value, value[:1].upper() + value[1:].lower() if value else "Leave")


def manager_named(managers: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The one project manager a name means: an exact match, else the only one
    whose name contains it. Two that contain it, or none, is None — ask instead."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    exact = [m for m in managers if str(m.get("name", "")).lower() == wanted]
    if exact:
        return exact[0] if len(exact) == 1 else None
    partial = [m for m in managers if wanted in str(m.get("name", "")).lower()]
    return partial[0] if len(partial) == 1 else None


def same_kind(a: str, b: str) -> bool:
    return kind_of(a).lower() == kind_of(b).lower()


def portion_of(value: str) -> str:
    """``FULL DAY`` or ``Full Day`` → "full day"."""
    return " ".join(str(value or "").replace("_", " ").split()).lower()


def _names(names: list[str]) -> str:
    """``[A]`` → "A"; ``[A, B]`` → "A and B"; three or more → "3 teammates (A, B and C)"."""
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return listed if len(names) < 3 else f"{len(names)} teammates ({listed})"


def teammates_off(result: dict[str, Any]) -> list[str]:
    """Who on the asker's projects is already off then: the HRMS's own count
    (``facts.team_leaves``) — anyone on the same project whose leave overlaps and
    is not rejected or cancelled, so a pending one counts."""
    team = (result.get("facts") or {}).get("team_leaves") or {}
    return [str(n).strip() for n in team.get("on_leave_names") or [] if str(n).strip()]


def teammates_sentence(names: list[str], date_from: str, date_to: str) -> str:
    if not names:
        return ""
    have = "has" if len(names) == 1 else "have"
    return (
        f"{_names(names)} {have} already applied for leave on "
        f"{when_of(date_from, date_to)}, so it may be difficult to approve."
    )


def warning_of(sentences: list[str]) -> str:
    """Every reason to think twice, as one paragraph that asks once. It warns and
    never blocks — whether to go ahead is the person's call, made on the Confirm
    that follows."""
    said = [s for s in sentences if s]
    return f"Heads-up: {' '.join(said)} Do you still want to apply?" if said else ""


def _briefing_lines(briefing: Any, *, jira_checked: bool = False) -> list[str]:
    """The HRMS's own briefing — a list of lines, or text — without its team line,
    which the warning above already says, nor its Jira lines when this server
    read Jira itself and said what they say."""
    lines = briefing if isinstance(briefing, list) else str(briefing or "").splitlines()
    return [
        line
        for line in (str(item).strip() for item in lines)
        if line
        and not line.startswith("Team:")
        and not (jira_checked and line.startswith("Jira:"))
    ]


async def jira_sentences(
    hrms: Hrms, jira: Jira | None, ctx: Context, off: list[str], date_from: str, date_to: str
) -> list[str] | None:
    """What Jira says about this leave, or None when Jira was not read.

    The person's own open tickets, and — when a teammate is already off — any of
    their projects' sprints that is tight (``workload``). Never fails the check:
    a Jira or an HRMS that does not answer here leaves the warning out.
    """
    if jira is None or not jira.configured:
        return None
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        session = await _ask(hrms, ctx, "get_user_session", {}) or {}
        projects = await _ask(hrms, ctx, "get_my_projects", {}) or []
    except (ToolError, ValueError):
        return None
    names = [str(p.get("name") or "") for p in projects if isinstance(p, dict)]
    email = str(session.get("company_email") or "")
    work, account = await asyncio.gather(
        jira.work(project_keys(jira.settings, names)), jira.account_of(email)
    )
    if not work:
        return None
    settings = jira.settings
    return [
        *workload.tight_sprints(
            work, off, start, end,
            tight_days=settings.sprint_tight_days,
            tight_share=settings.sprint_tight_open_share,
        ),
        workload.own_work(
            work, str(session.get("name") or ""), email, start, end, account=account
        ),
    ]


def when_of(date_from: str, date_to: str) -> str:
    start, end = day_of(date_from), day_of(date_to)
    return start if not date_to or start == end else f"{start} to {end}"


def day_of(value: str) -> str:
    return _day(value)


def _day(value: str) -> str:
    """``2026-10-02`` as ``Fri 2 Oct 2026``; anything unparseable as given."""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return f"{parsed:%a} {parsed.day} {parsed:%b %Y}"


def register(server: MCPServer, hrms: Hrms, jira: Jira | None = None) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_holidays(year: int, ctx: Context, month: int = 0) -> str:
        """The company's holidays for a year, or for one month of it.

        month is 1-12, or 0 for the whole year. Each line gives the date, its
        weekday, the holiday's name and whether it is public or optional.
        """
        if not 0 <= month <= 12:
            raise ToolError("month must be 1-12, or 0 for the whole year")
        arguments: dict[str, Any] = {"year": str(year)}
        if month:
            arguments["month"] = str(month)
        holidays = await _ask(hrms, ctx, "get_holidays", arguments)
        if not holidays:
            return "No holidays are recorded for that period."
        lines = []
        for item in holidays:
            kind = "optional" if item.get("is_optional") else (item.get("type") or "").lower()
            suffix = f" ({kind})" if kind else ""
            lines.append(f"{_day(str(item.get('date', '')))} — {item.get('name', '')}{suffix}")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_my_leave_balance(ctx: Context, year: int = 0) -> str:
        """The asker's own leave balance: for each leave type, the days
        available, used, and the year's total. year defaults to this year."""
        arguments: dict[str, Any] = {"year": year} if year else {}
        balances = await _ask(hrms, ctx, "get_leave_balance", arguments)
        if not balances:
            return "The HRMS has no leave quota recorded for you."
        return "\n".join(
            f"{item.get('type', 'Leave')}: {item.get('available', 0):g} of "
            f"{item.get('total_quota', 0):g} days available ({item.get('used', 0):g} used)"
            for item in balances
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_my_leaves(
        ctx: Context,
        status: Literal["", "Pending", "Approved", "Rejected", "Cancelled"] = "",
    ) -> str:
        """The asker's own leave requests, newest first, optionally only those
        with one status. Each line gives the type, the dates, full or half day,
        the status and the reason given."""
        arguments: dict[str, Any] = {"status": status} if status else {}
        leaves = await _ask(hrms, ctx, "get_leaves", arguments)
        if not leaves:
            which = f" that are {status.lower()}" if status else ""
            return f"You have no leave requests{which}."
        ordered = sorted(
            leaves, key=lambda item: str(item.get("leave_date_from", "")), reverse=True
        )
        lines = []
        for item in ordered:
            start = _day(str(item.get("leave_date_from", "")))
            end = _day(str(item.get("leave_date_to", "")))
            when = start if start == end or not end else f"{start} to {end}"
            kind = kind_of(item.get("leave_type", ""))
            mode = portion_of(item.get("leave_mode", ""))
            reason = item.get("reason") or "no reason given"
            ref = f" (request {item['id']})" if item.get("id") else ""
            # Why it was turned down, in the approver's words: what the employee
            # most wants to know about a rejected request.
            why = (
                f" — rejected because: {item['notes']}"
                if item.get("status") == "Rejected" and item.get("notes")
                else ""
            )
            lines.append(
                f"{kind}, {when}, {mode} — {item.get('status', 'unknown')}: {reason}{why}{ref}"
            )
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_leave_context(ctx: Context) -> str:
        """Who the person asking is, today's date in their timezone, the dates of
        this week and next week, holidays in the next month, their leave balance,
        and who approves their leave. Call this first whenever leave comes up."""
        me = await _ask(hrms, ctx, "get_user_session", {})
        if not isinstance(me, dict) or not me.get("authenticated", True):
            raise ToolError("The HRMS could not say who is asking.")
        today = str(me.get("current_date", ""))
        lines = [
            f"You are {me.get('name', 'unknown')} (role in the HRMS: {me.get('role', 'unknown')}).",
            f"Today is {me.get('current_day', '')} {today}, in {me.get('timezone', 'unknown')}.",
        ]
        for label, key in (("This week", "this_week"), ("Next week", "next_week")):
            week = me.get(key) or {}
            days = ", ".join(f"{name.capitalize()} {week[name]}" for name in _WEEK if name in week)
            lines.append(f"{label}: {days}.")
        lines.append(await _holidays_ahead(hrms, ctx, today))
        balances = await _ask(hrms, ctx, "get_leave_balance", {})
        if balances:
            lines.append(
                "Leave balance: "
                + "; ".join(
                    f"{b.get('type', 'Leave')} {b.get('available', 0):g} of "
                    f"{b.get('total_quota', 0):g} days left"
                    for b in balances
                )
                + "."
            )
        lines.append(await _approvers(hrms, ctx))
        lines.append(
            "Leave types: Casual Leave, Sick Leave, Floating Leave. "
            "Day portions: Full Day, First Half, Second Half."
        )
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def preview_leave(
        leave_type: LeaveType,
        date_from: str,
        day_portion: DayPortion,
        ctx: Context,
        date_to: str = "",
    ) -> str:
        """Check a leave request before filing it, creating nothing: how many days
        it counts as, clashes with meetings or teammates' leave, and the balance
        left. Dates are YYYY-MM-DD; date_to defaults to date_from. Call this
        before apply_leave with the same details."""
        result = await _ask(
            hrms,
            ctx,
            "preview_leave",
            {
                "leave_type": leave_type,
                "leave_mode": day_portion,
                "date_from": date_from,
                "date_to": date_to or date_from,
            },
        )
        if not isinstance(result, dict):
            return str(result)
        days = result.get("effective_days")
        counts = (
            f": counts as {days:g} day{'s' if days != 1 else ''}."
            if isinstance(days, int | float)
            else "."
        )
        head = (
            f"{kind_of(leave_type)}, {when_of(date_from, date_to or date_from)}, "
            f"{portion_of(day_portion)}{counts}"
        )
        until = date_to or date_from
        off = teammates_off(result)
        from_jira = await jira_sentences(hrms, jira, ctx, off, date_from, until)
        warning = warning_of([teammates_sentence(off, date_from, until), *(from_jira or [])])
        parts = [warning, head] if warning else [head]
        parts.extend(_briefing_lines(result.get("briefing"), jira_checked=from_jira is not None))
        parts.append(await _approvers(hrms, ctx))
        parts.append("Nothing has been filed.")
        return "\n".join(parts)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_leave_requests_to_decide(
        ctx: Context,
        status: Literal["Pending", "Approved", "Rejected", "Cancelled"] = "Pending",
    ) -> str:
        """Leave requests waiting on the person asking: their team's, for a
        project manager, and everyone's, for HR. Each line names who, the leave,
        the dates and the reason, then the request id to pass to approve_leave or
        reject_leave — never show that id to the person."""
        try:
            leaves = await hrms.call("get_team_leaves", {"status": status}, _identity(ctx))
        except HrmsError as error:
            if "access denied" in str(error).lower():
                return (
                    "The person asking has no team whose leave they decide — only project "
                    "managers and HR can approve or reject leave."
                )
            raise ToolError(str(error)) from None
        if not leaves:
            return f"No leave requests are {status.lower()} for you to decide."
        # Side by side: one after another, 20 lookups could outlast the call.
        lines = list(
            await asyncio.gather(
                *(describe_request(hrms, ctx, item) for item in leaves[:DECIDE_LIMIT])
            )
        )
        more = len(leaves) - DECIDE_LIMIT
        if more > 0:
            lines.append(f"…and {more} more not shown.")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_leave_request_impact(request_id: str, ctx: Context) -> str:
        """What one leave request on the asker's team would leave undone, for the
        project manager or HR asking about it: the applicant's open Jira tickets
        by key and summary, a sprint of theirs that is tight, and the HRMS's own
        briefing — meetings that day, teammates also off, balance. request_id
        comes from list_leave_requests_to_decide; never show it to the person."""
        identity = _identity(ctx)
        item = None
        for status in IMPACT_STATUSES:
            try:
                leaves = await hrms.call("get_team_leaves", {"status": status}, identity)
            except HrmsError as error:
                if "access denied" in str(error).lower():
                    return (
                        "The person asking has no team whose leave they decide — only "
                        "project managers and HR can ask about someone else's leave."
                    )
                raise ToolError(str(error)) from None
            item = next(
                (i for i in leaves or [] if str(i.get("leave_id") or i.get("id")) == request_id),
                None,
            )
            if item is not None:
                break
        if item is None:
            # Only a request the HRMS lists as the asker's team's is described:
            # the HRMS decides whose leave a manager may see, not the id passed.
            raise ToolError(
                "That is not a pending or approved leave request on your team. "
                "Find it with list_leave_requests_to_decide."
            )
        routed = await _routing(hrms, ctx, request_id)
        who = str(routed.get("employee") or "Someone")
        date_from = str(item.get("date_from", ""))
        date_to = str(item.get("date_to", "")) or date_from
        lines = [
            f"{who} — {kind_of(item.get('type', ''))}, {when_of(date_from, date_to)}, "
            f"{portion_of(item.get('mode', ''))}, {item.get('status', '')}. "
            f"Reason: {item.get('reason') or 'none given'}."
        ]
        from_jira = await their_jira(hrms, jira, ctx, who, date_from, date_to)
        lines.extend(from_jira or ["Jira was not checked, so their open tickets are unknown."])
        lines.extend(_briefing_for_manager(routed.get("briefing"), jira_checked=bool(from_jira)))
        return "\n".join(lines)


#: The requests a manager may ask about: one still to decide, or one already
#: approved whose days are still ahead of the team.
IMPACT_STATUSES = ("Pending", "Approved")


async def _routing(hrms: Hrms, ctx: Context, leave_id: str) -> dict[str, Any]:
    """The HRMS's routing of a request — who applied, and its briefing — or
    nothing when the HRMS will not say."""
    try:
        routed = await hrms.call("get_leave_routing", {"leave_id": leave_id}, _identity(ctx))
    except HrmsError:
        return {}
    return routed if isinstance(routed, dict) else {}


def _briefing_for_manager(briefing: Any, *, jira_checked: bool) -> list[str]:
    """The HRMS's briefing, which is worded to the applicant, said about them.

    Its team line stays — nothing above says it to the manager — and its Jira
    lines go when this server read Jira itself and said what they say."""
    lines = briefing if isinstance(briefing, list) else str(briefing or "").splitlines()
    said = []
    for line in (str(item).strip() for item in lines):
        if not line or (jira_checked and line.startswith("Jira:")):
            continue
        said.append(line.replace(" — you attend.", " — they attend.").replace(
            " — you organize.", " — they organize."
        ))
    return said


async def _email_of(hrms: Hrms, ctx: Context, name: str) -> str:
    """The work email of the one employee the HRMS calls exactly this, or "".

    The HRMS names an applicant and nothing more; their email is what finds them
    in Jira, where their name may be spelt differently."""
    try:
        found = await _ask(hrms, ctx, "resolve_employee", {"query": name})
    except ToolError:
        return ""
    same = [
        p for p in found or []
        if isinstance(p, dict) and str(p.get("name") or "").casefold() == name.casefold()
    ]
    return str(same[0].get("company_email") or "") if len(same) == 1 else ""


async def their_jira(
    hrms: Hrms, jira: Jira | None, ctx: Context, who: str, date_from: str, date_to: str
) -> list[str] | None:
    """What Jira says about someone else's leave, or None when Jira was not read.

    Their open tickets and a sprint of theirs that is tight, in the projects of
    the manager asking — which are the ones the applicant's leave is decided on.
    The HRMS does not list the applicant's own projects, so it says that any
    others were not checked."""
    if jira is None or not jira.configured or who == "Someone":
        return None
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        projects = await _ask(hrms, ctx, "get_my_projects", {}) or []
    except (ToolError, ValueError):
        return None
    names = [str(p.get("name") or "") for p in projects if isinstance(p, dict)]
    email = await _email_of(hrms, ctx, who)
    work, account = await asyncio.gather(
        jira.work(project_keys(jira.settings, names)), jira.account_of(email)
    )
    if not work:
        return None
    settings = jira.settings
    checked = ", ".join(p.key for p in work)
    return [
        workload.their_work(work, who, start, end, email=email, account=account),
        f"Only the asker's own Jira projects were checked ({checked}); any other "
        f"projects {who} works on were not.",
        *workload.tight_sprints(
            work, [who], start, end,
            tight_days=settings.sprint_tight_days,
            tight_share=settings.sprint_tight_open_share,
            email=email,
            account=account,
        ),
    ]


_WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


async def _holidays_ahead(hrms: Hrms, ctx: Context, today: str, days: int = 31) -> str:
    """Holidays from today for about a month, across a year boundary if needed."""
    try:
        start = date.fromisoformat(today)
    except ValueError:
        return "Holidays: unknown."
    end = start.toordinal() + days
    found = []
    for year in sorted({start.year, date.fromordinal(end).year}):
        for item in await _ask(hrms, ctx, "get_holidays", {"year": str(year)}) or []:
            try:
                when = date.fromisoformat(str(item.get("date", ""))[:10])
            except ValueError:
                continue
            if start.toordinal() <= when.toordinal() <= end:
                found.append(f"{_day(when.isoformat())} {item.get('name', '')}")
    return "Holidays in the next month: " + ("; ".join(found) if found else "none") + "."


async def _approvers(hrms: Hrms, ctx: Context) -> str:
    """Who approves the asker's leave, said so the assistant asks only when it must."""
    result = await _ask(hrms, ctx, "get_employee_project_managers", {})
    managers = [m.get("name", "") for m in (result or {}).get("project_managers", [])]
    if not managers:
        return "No project manager is recorded for them; the HRMS routes the request itself."
    if len(managers) == 1:
        return f"Their leave is approved by {managers[0]}."
    return (
        f"They have {len(managers)} project managers: {', '.join(managers)}. Ask which one "
        "should approve, and pass that name as approver when applying."
    )


async def describe_request(hrms: Hrms, ctx: Context, item: dict[str, Any]) -> str:
    """One team request as a line: who, what, when, why, then its id."""
    leave_id = str(item.get("leave_id") or item.get("id") or "")
    who = str((await _routing(hrms, ctx, leave_id)).get("employee") or "Someone")
    when = when_of(str(item.get("date_from", "")), str(item.get("date_to", "")))
    return (
        f"{who} — {kind_of(item.get('type', ''))}, {when}, {portion_of(item.get('mode', ''))}, "
        f"{item.get('status', '')}. Reason: {item.get('reason') or 'none given'}. "
        f"(request {leave_id})"
    )
