"""Comp-off: the request, its check and its filing, as the person asking.

A compensatory off is claimed for a day the person worked that was a company
holiday or a weekend — the HRMS's own rule, by the date alone. It is not leave,
and takes the same three things the HRMS's own comp-off form does: the day
worked, the day to take off in return (optional: it can be chosen later in the
HRMS) and a reason. Never a leave type.

``start_comp_off_request`` answers with a **form** (EC-D184). When no day worked
was named, it offers the HRMS's own list of holidays and weekends the person
logged work on and has not claimed; the day off is never asked, since a form
has no date to choose from. ``preview_comp_off`` is the check before Confirm:
the HRMS has none for a request not yet filed, so it works out from the
holidays whether each day is one.

``apply_comp_off`` is a write, confirmed by the person first, and **safe to
repeat**: the HRMS files a new request every time it is called, so it looks for
one already filed for the same day worked first.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import _ask, _today, approvers_sentence, date_in, day_of, warning_of
from hrms_mcp.writes import FILES, UNDOES, _waiting_for, one_to_cancel

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

#: A "leave type" that is a comp-off, which the HRMS keeps apart from leave.
COMP_OFF = re.compile(r"\bcomp[\s-]*offs?\b|\bcompensat", re.IGNORECASE)

#: Most days worked a form offers to choose from, the latest first.
CHOICES = 10


def _worked(work_date: str) -> date:
    worked = date_in(work_date)
    if worked is None:
        raise ToolError(
            "The day worked (YYYY-MM-DD) is needed. Ask the person which holiday or "
            "weekend they worked, then call this again."
        )
    return worked


def _off(off_date: str) -> date | None:
    """The day off, or None when none was chosen; refused when it is not a date."""
    if not off_date.strip():
        return None
    off = date_in(off_date)
    if off is None:
        raise ToolError(f"The day off {off_date!r} is not a date (YYYY-MM-DD).")
    return off


def comp_off_line(work_date: str, off_date: str = "", worked_as: str = "") -> str:
    """"Comp-off for working on Fri 2 Oct 2026 (Gandhi Jayanti), off on Mon 2 Nov 2026"."""
    named = f" ({worked_as})" if worked_as else ""
    line = f"Comp-off for working on {day_of(work_date)}{named}"
    return f"{line}, off on {day_of(off_date)}" if off_date else line


async def _holidays(hrms: Hrms, ctx: Context, *days: date) -> dict[str, dict[str, Any]]:
    """The company's holidays, by date, for every year the days fall in."""
    years = sorted({d.year for d in days})
    calendars = await asyncio.gather(
        *(_ask(hrms, ctx, "get_holidays", {"year": str(year)}) for year in years)
    )
    found: dict[str, dict[str, Any]] = {}
    for calendar in calendars:
        for item in calendar if isinstance(calendar, list) else []:
            if not isinstance(item, dict):
                continue
            day = str(item.get("date", ""))[:10]
            # Two on one date: a public holiday is kept over an optional one,
            # since the day is then off whatever the optional one says.
            if day not in found or found[day].get("is_optional"):
                found[day] = item
    return found


def claimable_as(day: date, holidays: dict[str, dict[str, Any]]) -> str:
    """What makes a day worked claimable, as the HRMS judges it — a company
    holiday by its name, or "Weekend" — or "" when it is neither."""
    holiday = holidays.get(day.isoformat())
    if holiday is not None:
        # Optional ones too: the HRMS counts any holiday on its calendar, which
        # is why a day off that is an optional holiday (_already_off) is still
        # a working day while a day worked on one is claimable.
        return str(holiday.get("name") or "Holiday")
    return "Weekend" if day.weekday() >= 5 else ""


def _already_off(day: date, holidays: dict[str, dict[str, Any]]) -> str:
    """Why the day is already off — a public holiday or a weekend — or "". An
    optional holiday is a working day."""
    holiday = holidays.get(day.isoformat())
    if holiday is not None and not holiday.get("is_optional"):
        return str(holiday.get("name") or "a holiday")
    return "a weekend" if day.weekday() >= 5 else ""


async def _who_approves(hrms: Hrms, ctx: Context) -> str:
    """Who must approve, said before it is filed — or "" when the HRMS cannot
    say, in whatever shape: the check goes on without it."""
    try:
        result = await _ask(hrms, ctx, "get_employee_project_managers", {})
    except ToolError:
        return ""
    managers = result.get("project_managers") if isinstance(result, dict) else None
    if not isinstance(managers, list):
        return ""
    return approvers_sentence([m for m in managers if isinstance(m, dict)], filed=False)


async def _today_of(hrms: Hrms, ctx: Context) -> date | None:
    """Today in the person's timezone, as the HRMS says it; None when it will not
    say, and the check goes on without it."""
    try:
        session = await _ask(hrms, ctx, "get_user_session", {})
    except ToolError:
        return None
    return _today(session) if isinstance(session, dict) and session.get("current_date") else None


async def _or_none(read: Any) -> Any:
    """What a read the check can do without came to, or None when it failed."""
    try:
        return await read
    except ToolError:
        return None


def _filed(history: Any, work_date: str) -> dict[str, Any] | None:
    """The person's comp-off already pending or approved for that day worked."""
    for item in history or []:
        if (
            isinstance(item, dict)
            and str(item.get("status")) in ("Pending", "Approved")
            and str(item.get("work_date", ""))[:10] == work_date
        ):
            return item
    return None


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=READ_ONLY)
    async def start_comp_off_request(
        ctx: Context,
        work_date: str = "",
        off_date: str = "",
        reason: str = "",
    ) -> CallToolResult:
        """Start a comp-off (compensatory off) request for the person asking: a
        day off in return for a holiday or weekend they worked. It is not leave:
        never start_leave_request for it.

        Pass work_date — the day worked, YYYY-MM-DD — and off_date and the
        reason only if they said them. Find a holiday they named ("I worked on
        Gandhi Jayanti") in get_holidays for its year: the day worked is past,
        and get_leave_context lists only the weeks ahead. Take the day off from
        get_leave_context. With no day worked named, leave it out: the person is
        offered the holidays and weekends they logged work on. The day worked
        being a holiday or a weekend is the point: never say it is already off.
        Do not ask about the reason or the day off yourself, and do not call
        preview_comp_off or apply_comp_off.
        """
        known: dict[str, Any] = {}
        questions: list[dict[str, Any]] = []
        older = 0
        if work_date.strip():
            known["work_date"] = _worked(work_date).isoformat()
        else:
            found = await _ask(hrms, ctx, "get_comp_off_eligibility", {})
            found = found if isinstance(found, dict) else {}
            days = [str(d)[:10] for d in found.get("eligible_work_dates") or []]
            names = [str(n) for n in found.get("holidays") or []]
            # The HRMS builds the two lists from the same rows, so they pair by
            # position; lists that do not line up are shown with no names rather
            # than with the wrong ones.
            if len(names) != len(days):
                names = [""] * len(days)
            # One entry a day, the latest first, whatever order the HRMS gives them in.
            named = sorted(
                {day: name for day, name in zip(days, names, strict=True)
                 if date_in(day) is not None}.items(),
                reverse=True,
            )
            options = [
                {"value": day, "label": f"{day_of(day)} · {name}" if name else day_of(day)}
                for day, name in named
            ]
            older = max(len(options) - CHOICES, 0)
            if not options:
                raise ToolError(
                    "The HRMS lists no holiday or weekend the person logged work on and "
                    "has not claimed. Ask them which day they worked, then call this "
                    "again with work_date."
                )
            questions.append(
                {
                    "name": "work_date",
                    "label": "Day worked",
                    "kind": "choice",
                    "options": options[:CHOICES],
                }
            )
        if off := _off(off_date):
            known["off_date"] = off.isoformat()
        if reason.strip():
            known["reason"] = reason.strip()
        else:
            questions.append({"name": "reason", "label": "Reason", "kind": "text"})

        if "work_date" in known:
            prompt = comp_off_line(known["work_date"], known.get("off_date", ""))
        elif "off_date" in known:
            prompt = f"Comp-off, off on {day_of(known['off_date'])}"
        else:
            prompt = "Comp-off"
        prompt += " — choose the rest:" if questions else "."
        if older:
            # The oldest are the ones left out: said, so one is never lost unseen.
            prompt += (
                f" The latest {CHOICES} days worked are shown; for one of the {older} "
                "older ones, say its date."
            )
        form = {
            "prompt": prompt,
            "questions": questions,
            "action": {"tool": "apply_comp_off", "arguments": known},
            "check": {"tool": "preview_comp_off"},
        }
        missing = ", ".join(q["label"].lower() for q in questions) or "nothing"
        left_out = (
            f" {older} older days worked are not among the choices: if they name one, "
            "call this again with it as work_date."
            if older
            else ""
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"The person is shown the missing details as choices ({missing}), "
                        "then the request to confirm. Do not ask them about these yourself, "
                        f"and do not call apply_comp_off for this request.{left_out}"
                    ),
                )
            ],
            _meta={"ec/form": form},
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def preview_comp_off(
        work_date: str,
        ctx: Context,
        off_date: str = "",
        reason: str = "",
    ) -> str:
        """Check a comp-off request before filing it, creating nothing: whether
        the day worked is a holiday or a weekend, whether the day off is already
        one, a comp-off already filed for that day, and who must approve it.
        Call this before apply_comp_off with the same details. The answer is
        written to the person asking."""
        worked, off = _worked(work_date), _off(off_date)
        # Independent of each other: side by side, the check waits for the slowest
        # only. Each only adds a warning or a name, so one that fails is left out
        # rather than stopping the request: apply_comp_off still looks for one
        # already filed before it files.
        holidays, approvers, history, today = await asyncio.gather(
            _or_none(_holidays(hrms, ctx, *(d for d in (worked, off) if d))),
            _who_approves(hrms, ctx),
            _or_none(_ask(hrms, ctx, "get_comp_off_history", {})),
            _today_of(hrms, ctx),
        )
        # Without the calendar, a holiday cannot be told from a working day: said
        # so, rather than warned of as one.
        known = holidays if holidays is not None else {}
        worked_as = claimable_as(worked, known)
        warnings = []
        if filed := _filed(history, worked.isoformat()):
            warnings.append(
                f"You already have a comp-off for {day_of(worked.isoformat())} — "
                f"{filed.get('status')}."
            )
        if today and worked > today:
            warnings.append(f"{day_of(worked.isoformat())} is still to come.")
        if holidays is None:
            warnings.append(
                "The holiday calendar could not be read, so whether these days are "
                "holidays was not checked."
            )
        elif not worked_as:
            warnings.append(
                f"{day_of(worked.isoformat())} is not a holiday or a weekend; your managers "
                "see it as an exception."
            )
        if off and off == worked:
            warnings.append(f"The day off, {day_of(off.isoformat())}, is the day worked.")
        elif off and off < worked:
            warnings.append(f"The day off, {day_of(off.isoformat())}, is before the day worked.")
        elif off and today and off < today:
            warnings.append(f"The day off, {day_of(off.isoformat())}, has already passed.")
        if off and holidays is not None and (why := _already_off(off, holidays)):
            warnings.append(f"The day off, {day_of(off.isoformat())}, is already off: {why}.")

        head = comp_off_line(worked.isoformat(), off.isoformat() if off else "", worked_as)
        head += f" — reason: {reason.strip()}." if reason.strip() else "."
        if not off:
            head += "\nThe day off is not chosen yet: it can be set in the HRMS later."
        sections = [
            warning_of(warnings),
            head,
            f"{approvers or 'Your project managers will approve it.'}\n"
            "Nothing is filed until you confirm.",
        ]
        return "\n\n".join(s for s in sections if s)

    @server.tool(annotations=FILES, structured_output=False)
    async def apply_comp_off(
        work_date: str,
        reason: str,
        ctx: Context,
        off_date: str = "",
    ) -> str:
        """File a comp-off request for the person asking. They confirm it first.

        Call preview_comp_off with the same details before this. Every argument
        comes from what the person said, the form, or get_leave_context — never
        guess a day or a reason. Dates are YYYY-MM-DD; off_date may be left out.
        It goes to their project managers.
        """
        if not reason.strip():
            raise ToolError("A reason is needed. Ask the person what they worked on that day.")
        worked, off = _worked(work_date).isoformat(), _off(off_date)
        what = comp_off_line(worked, off.isoformat() if off else "")
        if filed := _filed(await _ask(hrms, ctx, "get_comp_off_history", {}), worked):
            # Said as it is on file: its day off may not be the one asked for now.
            held = comp_off_line(worked, str(filed.get("off_date") or "")[:10])
            return (
                f"Already filed: {held[:1].lower()}{held[1:]} — {filed.get('status')}. "
                "Nothing new was filed."
            )

        arguments: dict[str, Any] = {"work_date": worked, "reason": reason.strip()}
        if off:
            arguments["off_date"] = off.isoformat()
        filed = await _ask(hrms, ctx, "apply_comp_off", arguments)
        status = str((filed.get("status") if isinstance(filed, dict) else "") or "Pending")
        waiting = await _waiting_for(hrms, ctx) if status == "Pending" else ""
        return f"Filed: {what} — reason: {reason.strip()}. Status: {status}.{waiting}"

    @server.tool(annotations=UNDOES, structured_output=False)
    async def cancel_my_comp_off(work_date: str, ctx: Context) -> str:
        """Withdraw one of the person's own comp-off requests, pending or
        approved and with its day off not yet taken. work_date is the day worked
        (YYYY-MM-DD)."""
        worked = _worked(work_date).isoformat()
        # The HRMS does not check whose a comp-off is, so only the person's own
        # history is looked through: an id not in it is never sent.
        history, today = await asyncio.gather(
            _ask(hrms, ctx, "get_comp_off_history", {}), _today_of(hrms, ctx)
        )
        held = [
            item
            for item in history or []
            if isinstance(item, dict) and str(item.get("work_date", ""))[:10] == worked
        ]

        def described(item: dict[str, Any]) -> str:
            line = comp_off_line(worked, str(item.get("off_date") or "")[:10])
            return f"{line[:1].lower()}{line[1:]}"

        item = one_to_cancel(
            held,
            none=f"The person has no comp-off for {day_of(worked)} that can be cancelled.",
            many=(
                f"The person has more than one comp-off for {day_of(worked)}. "
                "Cancel it in the HRMS instead."
            ),
            already=described,
            ref="id",
        )
        if isinstance(item, str):
            return item
        off = str(item.get("off_date") or "")[:10]
        if today and item.get("status") == "Approved" and off and off < today.isoformat():
            raise ToolError(
                f"The day off for that comp-off ({day_of(off)}) has already passed, so it "
                "is not cancelled here. Tell the person to ask HR."
            )
        await _ask(hrms, ctx, "cancel_comp_off", {"comp_off_id": str(item["id"])})
        return f"Cancelled: {described(item)[:1].upper()}{described(item)[1:]}."
