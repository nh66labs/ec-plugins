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
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp import trend, workload
from hrms_mcp.hrms import Hrms, HrmsError
from hrms_mcp.jira_work import JiraReader, JiraWork

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
1. Call get_leave_context first, and take dates from it — never work them out
   yourself:
   "today" is today's date, "tomorrow" the day after, "this Thursday" is this
   week's Thursday and "next Tuesday" next week's Tuesday.
2. Call start_leave_request with the dates and whatever else the person already
   said — the leave type, full or half day, the reason — and nothing they did
   not say. They are shown what is missing as choices, then the request to
   confirm: do not ask them about any of it yourself, and do not call
   preview_leave or apply_leave for it. The only thing to ask yourself is the
   date, if they gave none. Never ask about projects, managers or approvers:
   every project manager of theirs must approve, and one rejection rejects it.

When a check has "Pending in Jira" lists, filter them as preview_leave says:
each is what the manager named above it will see.

When a check starts with "Heads-up" — teammates already off, open Jira tickets,
a tight sprint — tell the person that first, with the ticket keys, and leave the
choice to them: it is a warning, not a refusal.

Deciding requests (project managers and HR): call list_leave_requests_to_decide
to find the request, then approve_leave or reject_leave. A rejection needs a
reason: use theirs if given and do not ask again; else ask. When approve_leave
says others must still approve, tell the manager who.

Asked about someone's leave as their project manager or HR — anything pending,
work affected while they are away — find the request with
list_leave_requests_to_decide (status Approved if already approved), then call
get_leave_request_impact with it. Answer from the
tickets it lists: name the ones that bear on what was asked, by key and summary,
and say plainly when none do — in the projects it checked, which you name; say
that any other projects of theirs were not checked. When it says Jira was not
checked, say you could not check their tickets — never that they have none.
When it says it is not certain, say which tickets may have been missed and why —
never that they have none.

Showing a manager a pending request, also call list_leave_requests_to_decide with
status Approved. If someone else's approved leave overlaps it, say so first — who,
already approved, the dates — then call get_leave_request_impact on that request
and list their pending Jira tickets by key and summary. Warn only: the manager
decides.

Every answer drawn from these tools cites the result it came from by its number,
like [1], in the sentence that uses it — a list of holidays or balances too, and
an answer that there is nothing (no requests to decide, no leave taken). An
answer that cites nothing is not shown to the person.

Never show a request's id; describe it by who, what and when.
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


def same_kind(a: str, b: str) -> bool:
    return kind_of(a).lower() == kind_of(b).lower()


def portion_of(value: str) -> str:
    """``FULL DAY`` or ``Full Day`` → "full day"."""
    return " ".join(str(value or "").replace("_", " ").split()).lower()


def _names(names: list[str]) -> str:
    """``[A]`` → "A"; ``[A, B]`` → "A and B"; three or more → "3 teammates (A, B and C)"."""
    listed = workload.joined(names)
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
    days = "day" if date_from == date_to else "days"
    return f"{_names(names)} {have} already applied for leave on the same {days}."


def warning_of(sentences: list[str]) -> str:
    """Every reason to think twice, as one paragraph that asks once. It warns and
    never blocks — whether to go ahead is the person's call, made on the Confirm
    that follows."""
    said = [s for s in sentences if s]
    return f"Heads-up: {' '.join(said)} Do you still want to proceed?" if said else ""


def _number(value: Any) -> bool:
    """An int or float from the HRMS — never a bool, which Python counts as one."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _days(n: float) -> str:
    """``1`` → "1 day"; ``0.5`` → "half a day"; ``2.5`` → "2.5 days"."""
    return "half a day" if n == 0.5 else f"{n:g} day{'' if n == 1 else 's'}"


def request_line(
    leave_type: str, date_from: str, date_to: str, day_portion: str, days: Any
) -> str:
    """The request as one sentence: "Casual leave on Fri 9 Oct 2026, full day — 1 day."."""
    when = when_of(date_from, date_to)
    on = "from" if " to " in when else "on"
    counts = f" — {_days(days)}" if _number(days) else ""
    return f"{kind_of(leave_type)} {on} {when}, {portion_of(day_portion)}{counts}."


def balance_sentence(balance: Any) -> tuple[str, bool]:
    """What the request leaves of the balance, from the HRMS's ``facts.balance``,
    and whether it needs more than is left — a reason to think twice, said in
    the Heads-up. Empty when it gave none, and the HRMS's own line is shown instead."""
    if not isinstance(balance, dict):
        return "", False
    left, needs = balance.get("available"), balance.get("requested")
    if not _number(left) or not _number(needs):
        return "", False
    kind = kind_of(str(balance.get("leave_type") or "")).lower()
    after = left - needs
    if left <= 0:
        return f"You have no {kind} left, and this needs {_days(needs)}.", True
    have = f"You have {_days(left)} of {kind} left"
    if after < 0:
        return f"{have}, and this needs {_days(needs)} — {_days(-after)} more than you have.", True
    return f"{have} — {_days(after)} after this one.", False


# What the HRMS's balance line may say that facts.balance already does; a line
# saying anything else — that the excess is unpaid, say — is kept.
_BALANCE_CLAUSES = (
    re.compile(r"[\d.]+ [^;]*\bavailable", re.IGNORECASE),
    re.compile(r"this request uses [\d.]+(?: days?| day\(s\))?", re.IGNORECASE),
)


def _plain_balance(line: str) -> bool:
    """Whether an HRMS "Balance:" line says only what is available and what the
    request uses, however it words the leave type or the days."""
    if not line.startswith("Balance:"):
        return False
    clauses = line.removeprefix("Balance:").strip().rstrip(".").split(";")
    return all(
        any(c.fullmatch(clause.strip()) for c in _BALANCE_CLAUSES) for clause in clauses
    )


def _briefing_lines(briefing: Any, *, said: str = "") -> list[str]:
    """The HRMS's own briefing — a list of lines, or text — without its team line,
    which the warning above already says, nor a Jira line whose every ticket this
    server already named (``said``). One naming a ticket not named — in a project
    not read here, or not due around the leave — stays."""
    lines = briefing if isinstance(briefing, list) else str(briefing or "").splitlines()
    return [
        line
        for line in (str(item).strip() for item in lines)
        if line and not line.startswith("Team:") and not _said_already(line, said)
    ]


async def _session_and_work(
    hrms: Hrms, jira: JiraReader, ctx: Context, *, leave_id: str = "", reuse: bool = False
) -> tuple[dict[str, Any], JiraWork | None]:
    """The asker's session — {} when the HRMS will not give it — and the Jira
    work the HRMS reads for them (``JiraReader.read``), asked side by side."""

    async def session() -> dict[str, Any]:
        try:
            answer = await _ask(hrms, ctx, "get_user_session", {})
        except ToolError:
            return {}
        # An HRMS that answers in plain text gives a str, not the shape asked for.
        return answer if isinstance(answer, dict) else {}

    return await asyncio.gather(session(), jira.read(ctx, leave_id=leave_id, reuse=reuse))


async def _own_jira(
    hrms: Hrms, jira: JiraReader, ctx: Context, date_from: str, date_to: str, *, reuse: bool
) -> tuple[list[workload.ProjectWork], dict[str, Any], str, str, date, date] | None:
    """The asker's projects' open work in Jira, as the HRMS read it, with what
    the rules need about them — their session, email and Jira account — and the
    leave's dates; None when the HRMS does not say who they are or what Jira
    holds of theirs. ``reuse`` takes a recent answer (``JiraReader.read``).
    Never raises: a warning left out is better than a leave that cannot be
    started or checked."""
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        return None
    session, work = await _session_and_work(hrms, jira, ctx, reuse=reuse)
    if work is None or not work.projects:
        return None
    email = str(session.get("company_email") or "")
    return work.projects, session, email, work.account, start, end


#: Says the heads-up in at most the characters given ("" when it does not fit).
HeadsUp = Callable[[int], str]


async def own_heads_up(
    hrms: Hrms, jira: JiraReader | None, ctx: Context, date_from: str, date_to: str
) -> HeadsUp | None:
    """The asker's own tickets these dates touch, as one short sentence for the
    moment they name them (``workload.own_heads_up``), sized once the room for
    it is known; None when Jira was not read. A recent answer from Jira will
    do: the check before Confirm reads it again."""
    if jira is None:
        return None
    read = await _own_jira(hrms, jira, ctx, date_from, date_to, reuse=True)
    if read is None:
        return None
    work, session, email, account, start, end = read
    settings = jira.settings
    def say(limit: int) -> str:
        return workload.own_heads_up(
            work, str(session.get("name") or ""), email, start, end, account=account,
            today=_today(session),
            after_days=settings.jira_after_leave_days,
            soon_days=settings.jira_leave_soon_days,
            limit=limit,
        )

    return say


async def jira_sentences(
    hrms: Hrms, jira: JiraReader | None, ctx: Context, off: list[str], date_from: str, date_to: str
) -> tuple[list[str], str] | None:
    """What Jira says about this leave, or None when Jira was not read: the
    sentences that warn, and a note said as a plain line when none of the
    person's open tickets is due around the leave.

    The person's own open tickets the leave touches, and — when a teammate is already off — any of
    their projects' sprints that is tight (``workload``). Never fails the check:
    a Jira or an HRMS that does not answer here leaves the warning out. Jira is
    read afresh, so a ticket closed or moved since the heads-up is not warned of.
    """
    if jira is None:
        return None
    read = await _own_jira(hrms, jira, ctx, date_from, date_to, reuse=False)
    if read is None:
        return None
    work, session, email, account, start, end = read
    settings = jira.settings
    warning, note = workload.own_work(
        work, str(session.get("name") or ""), email, start, end, account=account,
        today=_today(session),
        after_days=settings.jira_after_leave_days,
        soon_days=settings.jira_leave_soon_days,
    )
    return [
        *workload.tight_sprints(
            work, off, start, end,
            tight_days=settings.sprint_tight_days,
            tight_share=settings.sprint_tight_open_share,
        ),
        warning,
    ], note


def _today(session: Any) -> date:
    """Today in the asker's timezone, as the HRMS says it; the server's own when
    it does not."""
    try:
        return date.fromisoformat(str((session or {}).get("current_date") or "")[:10])
    except (ValueError, AttributeError):
        return date.today()


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


def register(server: MCPServer, hrms: Hrms, jira: JiraReader | None = None) -> None:
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
                f"{kind}, {when}, {mode} — {item.get('status', 'unknown')}: {reason}{why}"
                f"{progress_of(item)}{ref}"
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
        # Accepted and ignored, so a card saved before every project manager
        # approved still validates: the HRMS sends the request to them all.
        approver: str = "",
    ) -> str:
        """Check a leave request before filing it, creating nothing: how many days
        it counts as, clashes with meetings or teammates' leave, the balance
        left, and who must approve it. Dates are YYYY-MM-DD; date_to defaults to
        date_from. Call this before apply_leave with the same details. The
        answer is written to the person asking.

        Of the Jira tickets it lists, in its heads-up and its "Pending in Jira"
        lists alike, name only those in the person's current sprint and due on
        or after the leave's first day. Each "Pending in Jira" list comes under
        a project manager's name and holds only the projects that manager
        manages: keep each list under its own manager. Leave out one due before
        the leave starts, even if still open: a ticket due Oct 15 or Oct 20 is
        not named for a leave from Nov 7. Leave out one with no due date, or in
        no current sprint. When none is left, say none of their current
        sprint's tickets fall due on or after the leave; do not list or count
        the ones left out."""
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
        until = date_to or date_from
        head = request_line(
            leave_type, date_from, until, day_portion, result.get("effective_days")
        )
        balance, short = balance_sentence((result.get("facts") or {}).get("balance"))
        off = teammates_off(result)
        # Independent of each other: side by side, the check waits for the slower only.
        # The Jira read is cancelled if the managers' lookup fails, not left running.
        reading = asyncio.ensure_future(
            jira_sentences(hrms, jira, ctx, off, date_from, until)
        )
        try:
            approvers = await _approvers(hrms, ctx)
        except BaseException:
            reading.cancel()
            raise
        jira_warnings, jira_note = await reading or ([], "")
        warning = warning_of(
            [teammates_sentence(off, date_from, until), *jira_warnings, balance if short else ""]
        )
        parts = [p for p in (warning, head) if p]
        if balance and not short:
            parts.append(balance)
        # The HRMS's own lists of their open tickets, as each project manager's
        # card will show them: said as they are, so all read the same.
        if pending := str(result.get("pending_jira") or "").strip():
            parts.append(pending)
        if jira_note:
            parts.append(jira_note)
        parts.extend(
            line
            for line in _briefing_lines(result.get("briefing"), said="\n".join(parts))
            # Said above in the person's words; the HRMS's own line only without it,
            # or when it says more — that the excess is unpaid, say.
            if not (balance and _plain_balance(line))
        )
        parts.append(approvers)
        parts.append("Nothing is filed until you confirm.")
        return "\n".join(parts)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_leave_requests_to_decide(
        ctx: Context,
        status: Literal["Pending", "Approved", "Rejected", "Cancelled"] = "Pending",
    ) -> str:
        """Leave requests waiting on the person asking: their team's, for a
        project manager, and everyone's, for HR. Each line names who, the leave,
        the dates and the reason, where an every-manager approval stands, then
        the request id to pass to approve_leave or reject_leave — never show
        that id to the person. One that says "you approved" waits on the others:
        do not approve it again."""
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
        me = await _asker_name(hrms, ctx)
        # Side by side: one after another, 20 lookups could outlast the call.
        lines = list(
            await asyncio.gather(
                *(describe_request(hrms, ctx, item, me) for item in leaves[:DECIDE_LIMIT])
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
        by key and summary — in the projects the asker manages only, every one
        for HR — a sprint of theirs that is tight, and the HRMS's own briefing —
        meetings that day, teammates also off, balance. request_id
        comes from list_leave_requests_to_decide; never show it to the person.

        Of the tickets it lists, name only those in the applicant's current
        sprint and due on or after the leave's first day — the same tickets the
        applicant was shown. Leave out one due before the leave starts, even if
        still open: a ticket due Oct 15 or Oct 20 is not named for a leave from
        Nov 7. Leave out one with no due date, or in no current sprint. When
        none is left, say none of their current sprint's tickets fall due on or
        after the leave; do not list or count the ones left out."""
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
        from_jira = await their_jira(hrms, jira, ctx, who, request_id, date_from, date_to)
        said, checked = from_jira or (
            ["Jira was not checked, so their open tickets are unknown."], None
        )
        lines.extend(said)
        lines.extend(
            _briefing_for_manager(routed.get("briefing"), said=" ".join(said), checked=checked)
        )
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY)
    async def get_leave_trend(
        employee: str,
        ctx: Context,
        period: trend.Period = trend.DEFAULT_PERIOD,
        date_from: str = "",
        date_to: str = "",
    ) -> CallToolResult:
        """One employee's leave over a period, counted from the HRMS's records:
        requests by status, approved days taken, by type, month, quarter and
        weekday, long breaks around weekends and holidays, repeats, the same
        length of time before, upcoming leave and the balance now — then the
        lines that are notable.

        Call it for any question about someone's leave trend, pattern, frequency
        or history — "what is the trend of Anu taking leave?" — not
        list_leave_requests_to_decide, which is for deciding. employee is the
        name, work email or employee code as the person said it, or "me" for
        their own. Only an HRMS administrator may ask about someone else.

        period: pass the one the person named. With none, leave the default and
        say the period analysed was the last six months, with its dates.
        "between dates" takes date_from and date_to as YYYY-MM-DD.

        Answer in this order: the period and its dates; how much leave and how
        many requests; by type; patterns; against the time before; then the
        Notable lines — only those as highlights; upcoming leave and the balance
        are context, said last and never as notable. Say only what the result
        says: never suggest a reason, a health matter, a motive, misuse or a
        judgement of the person, and never compare them with other employees — a
        pattern is counts and dates. The HRMS records no gender: call the person
        by name or "they", never "he" or "she". When it says too few days to show a pattern, say
        exactly that. When several people match, ask which one by the names and
        codes given, then call again with the code. Cite the result, like [1], in
        each sentence that uses it. Leave reasons are not given; do not look for
        them elsewhere.

        When the result is a trend (not a refusal or a question of who), lay it
        out with present, copying every figure exactly as the result writes it —
        never count, round or reword one. First a metrics card titled "Leave trend
        · " and the person as the first line names them, subtitled the period's
        dates, " · ", then the Pattern line; its fields in this order: "Leave
        taken" (the days; note the Change line), "Requests" (the number, format
        number; note the counts by status as written), "Monday or Friday"
        (value like "6/11.5"; note "days · about 4.6 if spread evenly"), "Long
        breaks" (the number, format number; note "3+ days off around a weekend
        or holiday"), "Leave left" (value like "10.5/25"; note "days · " then the
        types as written). Leave out a field whose line the result does not
        have. Second, when "By month and type" names two months or more, a table
        titled "By month": columns Month, one a leave type taken as named
        there, then Total (format number); a row a month as named there, "–"
        for none, the month's total from "By month"; then a Total row of each
        type's days from "By type" and the days from "Leave taken" — never add
        a column up yourself. Then write one sentence naming
        the person and the period, then the Notable lines as short sentences,
        then upcoming leave in one line — never the cards' figures again."""
        session = await _ask(hrms, ctx, "get_user_session", {})
        if not isinstance(session, dict) or not session.get("employee_id"):
            raise ToolError("The HRMS could not say who is asking, so no trend is given.")
        today = _today(session)
        try:
            span = trend.span_of(period, today, date_from, date_to)
        except ValueError as error:
            raise ToolError(str(error)) from None

        def said(text: str) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text=text)])

        if _is_self(session, employee):
            person: dict[str, Any] | str = {
                "id": session["employee_id"],
                "name": session.get("name"),
                "employee_id": session.get("employee_code"),
            }
        elif session.get("role") == "admin":
            person = await _person_meant(hrms, ctx, employee)
        else:
            # The HRMS would answer a non-admin's question about someone else
            # with the asker's own leave, so it is never put to it.
            return said(NOT_AN_ADMIN)
        if isinstance(person, str):
            return said(person)
        target = str(person["id"])

        first = span.before_start - timedelta(days=7)
        last = max(span.end, today) + timedelta(days=7)
        records, balances, *calendars = await asyncio.gather(
            _ask(hrms, ctx, "get_leaves", {"employee_id": target}),
            _balances(hrms, ctx, target),
            *(_ask(hrms, ctx, "get_holidays", {"year": str(year)})
              for year in range(first.year, last.year + 1)),
        )
        records = [r for r in records or [] if isinstance(r, dict)]
        if any(str(r.get("employee_id") or target) != target for r in records):
            # Only ever the person asked about: leave of anyone else, under
            # their name, would be worse than no answer. A record that names no
            # one is the answer to asking for this person's leave.
            raise ToolError(
                "The HRMS returned leave that is not this person's, so no trend is given. "
                "Tell the person it could not be checked right now."
            )
        holidays = trend.holidays_of(
            item for calendar in calendars for item in calendar or [] if isinstance(item, dict)
        )
        found = trend.summarise(trend.leaves_of(records, kind_of), holidays, span, today)
        who = _person_label(person)
        return said("\n".join(trend.lines(found, who, balance=_balance_line(balances))))


NOT_AN_ADMIN = "Only an HRMS administrator can look at another employee's leave pattern."

#: What a person says to mean themselves.
SELF_WORDS = {"me", "myself", "my", "i", "mine"}


def _is_self(session: dict[str, Any], employee: str) -> bool:
    said = employee.strip().casefold()
    own = {
        str(session.get(field) or "").casefold()
        for field in ("name", "employee_code", "company_email")
    } - {""}
    return said in SELF_WORDS or said in own


def _person_label(person: dict[str, Any]) -> str:
    name = str(person.get("name") or "This employee")
    code = str(person.get("employee_id") or "")
    return f"{name} ({code})" if code else name


async def _person_meant(hrms: Hrms, ctx: Context, employee: str) -> dict[str, Any] | str:
    """The one employee a name, email or code means — or what to say instead.

    The HRMS matches a name as any part of it, so "Anu" finds Anusree and Manu
    too: an exact name, code or email wins; otherwise a single match; otherwise
    the assistant is told to ask which one."""
    wanted = employee.strip()
    if not wanted:
        return "Say whose leave to look at — a name, work email or employee code."
    try:
        found = await hrms.call("resolve_employee", {"query": wanted}, _identity(ctx))
    except HrmsError as error:
        if "no employee found" in str(error).lower():
            return f'The HRMS has no employee matching "{wanted}".'
        raise ToolError(str(error)) from None
    people = [p for p in found or [] if isinstance(p, dict) and p.get("id")]
    if not people:
        return f'The HRMS has no employee matching "{wanted}".'
    key = wanted.casefold()
    exact = [
        p for p in people
        if key in {str(p.get(f) or "").casefold() for f in ("name", "employee_id", "company_email")}
    ]
    if len(exact) == 1:
        return exact[0]
    if len(people) == 1:
        return people[0]
    choices = exact or people
    named = ", ".join(_person_label(p) for p in choices[:CHOICES_LIMIT])
    more = len(choices) - CHOICES_LIMIT
    if more > 0:
        named += f" and {more} more"
    return (
        f'Several people match "{wanted}": {named}. Ask which one they mean, then call '
        "again with their employee code."
    )


#: How many people a name matching several lists.
CHOICES_LIMIT = 8


async def _balances(hrms: Hrms, ctx: Context, employee_id: str) -> list[Balance]:
    """Their balance now, a leave type at a time — or none: a trend is still
    worth giving when the balance cannot be read."""
    try:
        found = await _ask(hrms, ctx, "get_leave_balance", {"employee_id": employee_id})
    except ToolError:
        return []
    return [
        (kind_of(str(item.get("type", ""))), float(item["available"]), float(item["total_quota"]))
        for item in found or []
        if isinstance(item, dict) and _number(item.get("available"))
        and _number(item.get("total_quota"))
    ]


#: A balance as the HRMS gives it: the leave type, days available, the quota.
Balance = tuple[str, float, float]


def _balance_line(balances: list[Balance]) -> str:
    """The days left of every type's quota, together and by type, as the card
    shows them: "Leave left now: 10.5/25 days — Casual 3/12 · Sick 7.5/12"."""
    if not balances:
        return ""
    left = sum(b[1] for b in balances)
    quota = sum(b[2] for b in balances)
    parts = [f"{trend.short_kind(kind)} {have:g}/{total:g}" for kind, have, total in balances]
    return f"Leave left now: {left:g}/{quota:g} days — " + " · ".join(parts) + "."



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


_TICKET = re.compile(r"\b[A-Z][A-Z0-9_]*-\d+\b")


def _said_already(line: str, said: str) -> bool:
    """A Jira line of the HRMS's whose every ticket this server already named
    (``said``). One naming no ticket, or a ticket not named — not due around
    the leave, or one Jira did not show as the applicant's — stays."""
    keys = set(_TICKET.findall(line))
    return line.startswith("Jira:") and bool(keys) and keys <= set(_TICKET.findall(said))


def _other_project(line: str, checked: set[str] | None) -> bool:
    """A Jira line of the HRMS's naming a ticket outside the Jira projects read
    for the manager (``checked``): another project's work, which is never
    theirs to be shown. With nothing read, the HRMS's own scoping stands."""
    if checked is None or not line.startswith("Jira:"):
        return False
    return any(key.rsplit("-", 1)[0] not in checked for key in _TICKET.findall(line))


def _briefing_for_manager(
    briefing: Any, *, said: str, checked: set[str] | None = None
) -> list[str]:
    """The HRMS's briefing, which is worded to the applicant, said about them.

    Its team line stays — nothing above says it to the manager — and its Jira
    lines go when this server already named every ticket they name (``said``),
    or when they name a ticket of a project the manager does not decide on."""
    lines = briefing if isinstance(briefing, list) else str(briefing or "").splitlines()
    kept = []
    for line in (str(item).strip() for item in lines):
        if not line or _said_already(line, said) or _other_project(line, checked):
            continue
        kept.append(line.replace(" — you attend.", " — they attend.").replace(
            " — you organize.", " — they organize."
        ))
    return kept


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
    hrms: Hrms, jira: JiraReader | None, ctx: Context, who: str, request_id: str,
    date_from: str, date_to: str,
) -> tuple[list[str], set[str] | None] | None:
    """What Jira says about someone else's leave, and the Jira projects it was
    read in (None when none was); None when Jira was not read.

    Their open tickets — those the leave touches first — and a sprint of theirs
    that is tight, as the HRMS read them for the asker: in the projects the asker
    manages only, which are the ones the leave is decided on here (every one of
    theirs, for HR). A project of those that is not linked to Jira, or whose
    Jira did not answer, is said as such — its tickets are unknown, not none."""
    if jira is None or who == "Someone":
        return None
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        return None
    (session, work), email = await asyncio.gather(
        _session_and_work(hrms, jira, ctx, leave_id=request_id), _email_of(hrms, ctx, who)
    )
    if work is None:
        return None
    settings = jira.settings
    said = []
    if work.projects:
        said.append(workload.their_work(
            work.projects, who, start, end, email=email, account=work.account,
            today=_today(session),
            after_days=settings.jira_after_leave_days,
            soon_days=settings.jira_leave_soon_days,
        ))
        if work.view == "managed":
            # HR sees every project of theirs: only a manager's view leaves some out.
            said.append(
                "Only the Jira projects you decide this leave for were checked "
                f"({', '.join(p.key for p in work.projects)}); any other projects {who} "
                "works on were not."
            )
    if work.not_linked:
        are = "is" if len(work.not_linked) == 1 else "are"
        said.append(
            f"{workload.joined(work.not_linked)} {are} not linked to Jira, so {who}'s "
            "tickets there could not be checked."
        )
    said += [
        f"{who}'s tickets in {name} could not be checked: {why}." for name, why in work.unreadable
    ]
    said += workload.tight_sprints(
        work.projects, [who], start, end,
        tight_days=settings.sprint_tight_days,
        tight_share=settings.sprint_tight_open_share,
        email=email,
        account=work.account,
    )
    # With no project read, the HRMS's own briefing — already narrowed to the
    # asker's projects — is all there is: nothing to narrow it further by. A
    # project whose Jira did not answer is still the asker's: its lines stay.
    if not work.projects:
        return said, None
    return said, {p.key for p in work.projects} | set(work.unreadable_keys)


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
    """Who approves the asker's leave, said to the asker before it is filed."""
    result = await _ask(hrms, ctx, "get_employee_project_managers", {})
    return approvers_sentence((result or {}).get("project_managers", []), filed=False)


def approvers_sentence(managers: list[dict[str, Any]], *, filed: bool) -> str:
    """Who decides the asker's leave, said to the asker: the one manager, or all
    of them — every one must approve. Never a choice: the HRMS sends it to them
    all. ``filed`` says it of a request just filed ("It is waiting for…")
    rather than of one about to be."""
    names = _manager_names(managers)
    if not names:
        return "You have no project manager on record, so HR will decide it."
    if len(names) == 1:
        if filed:
            return f"It is waiting for {names[0]} to approve."
        return f"{_name_of(managers[0], 'Your project manager')} will approve it."
    if filed:
        return (
            f"It is waiting for {workload.joined(names)} — it is approved once all of them "
            "approve, and rejected if any of them rejects."
        )
    return (
        f"It goes to {workload.joined(names)}; all of them must approve it, "
        "and one rejection rejects it."
    )


def _name_of(manager: dict[str, Any], blank: str) -> str:
    """A manager's name, or ``blank`` when the HRMS left it empty."""
    return str(manager.get("name") or "").strip() or blank


def _manager_names(found: list[dict[str, Any]]) -> list[str]:
    """Their names, as listed to the asker — one the HRMS left blank still counts."""
    return [_name_of(m, "one with no name") for m in found]


def votes_of(item: dict[str, Any], decision: str) -> list[str]:
    """The names of a request's approvers whose vote is ``decision`` ("Approved",
    "Pending"), as the HRMS lists them on it; none when it lists no approvals."""
    return [
        str(a.get("approver_name") or "").strip() or "one with no name"
        for a in item.get("approvals") or []
        if isinstance(a, dict) and a.get("decision") == decision
    ]


def _every_one(item: dict[str, Any]) -> bool:
    """Whether a request still waits for every project manager to approve it.
    One filed under the older rule — the first decision is final — never does."""
    return item.get("status") == "Pending" and item.get("approval_rule") == "all"


def _votes(item: dict[str, Any], me: str = "") -> tuple[list[str], list[str]]:
    """Who has approved a request, and who is still to decide — the asker, whose
    name is ``me``, said as "you"."""
    def said(names: list[str]) -> list[str]:
        return ["you" if me and name.casefold() == me.casefold() else name for name in names]
    return said(votes_of(item, "Approved")), said(votes_of(item, "Pending"))


def progress_of(item: dict[str, Any]) -> str:
    """Where a request waiting on every project manager stands, said to its
    employee: " — approved by A; waiting for B and C". Empty for any other."""
    if not _every_one(item):
        return ""
    approved, waiting = _votes(item)
    said = [
        *([f"approved by {workload.joined(approved)}"] if approved else []),
        *([f"waiting for {workload.joined(waiting)}"] if waiting else []),
    ]
    return f" — {'; '.join(said)}" if said else ""


def _standing(item: dict[str, Any], me: str = "") -> str:
    """The same, said to an approver as a sentence: " You approved; waiting for
    B and C." when they already have — so they are not asked to approve twice —
    and "waiting for you and C" while they are still to decide. ``me`` is the
    asker's name."""
    if not _every_one(item):
        return ""
    approved, waiting = _votes(item, me)
    still = f"waiting for {workload.joined(waiting)}" if waiting else ""
    if item.get("my_decision") == "Approved":
        return f" You approved; {still}." if still else " You approved."
    said = [
        *([f"Approved by {workload.joined(approved)}"] if approved else []),
        *([still] if still else []),
    ]
    return f" {'; '.join(said)}." if said else ""


async def _asker_name(hrms: Hrms, ctx: Context) -> str:
    """The asker's name as the HRMS has it, or "" when it will not say — only
    used to say "you" in place of their own name."""
    try:
        me = await _ask(hrms, ctx, "get_user_session", {})
    except ToolError:
        return ""
    return str(me.get("name") or "").strip() if isinstance(me, dict) else ""


async def describe_request(
    hrms: Hrms, ctx: Context, item: dict[str, Any], me: str = ""
) -> str:
    """One team request as a line: who, what, when, why, where its approval
    stands (said to ``me``, the asker), then its id."""
    leave_id = str(item.get("leave_id") or item.get("id") or "")
    who = str((await _routing(hrms, ctx, leave_id)).get("employee") or "Someone")
    when = when_of(str(item.get("date_from", "")), str(item.get("date_to", "")))
    return (
        f"{who} — {kind_of(item.get('type', ''))}, {when}, {portion_of(item.get('mode', ''))}, "
        f"{item.get('status', '')}. Reason: {item.get('reason') or 'none given'}."
        f"{_standing(item, me)} (request {leave_id})"
    )
