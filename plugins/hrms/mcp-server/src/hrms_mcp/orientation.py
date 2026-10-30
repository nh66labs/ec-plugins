"""A new employee's HR orientation, from HR's side, through the assistant.

The employee proposes their times with the date and time pickers the HR bot
sends them (not through a tool); every HR admin is told in a direct message and
answers in the same chat. These tools are that answer: see what is waiting,
confirm one time with a place (which schedules it, and the employee is sent the
date, time and place), and, once the orientation has been held, mark it
completed — the last step: an onboarding is complete only after that.

HRMS admins only: anyone else is told so as an answer, which the reply can cite.
The place is never guessed. These tools' descriptions carry how the conversation
runs, because the server's instructions are at the platform's limit.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp import say
from hrms_mcp.hrms import Hrms
from hrms_mcp.onboarding import NOT_HR_ADMIN, NotAdmin, ask_admin
from hrms_mcp.say import RELAY
from hrms_mcp.tools import READ_ONLY
from hrms_mcp.writes import FILES

NO_REQUESTS = "No new employee is waiting for their HR orientation to be confirmed."

CONFIRM_ORIENTATION = """Confirm a new employee's HR orientation for a date, time and place.
HR confirms it first. It schedules the orientation; it does not complete
the onboarding.

HRMS admins only. When HR says, in the same chat as the orientation
message, "Confirm Ethan's orientation for Tuesday 11am in Room 2":
1. Call list_orientation_requests first. If it says only an HRMS
   administrator can, tell them, citing it, and stop.
2. employee is the name HR used, or their email or code.
3. Take date (YYYY-MM-DD) and time (24-hour HH:MM) from the option HR
   named in that list. For a day in words with no option, call
   get_leave_context and take the date from it — never work it out
   yourself.
4. HR did not say where: ask, in one short question. Never guess a place.
   A room or a meeting link will do.
5. Propose this once; its card is HR's confirmation — never ask "shall I
   go ahead?" yourself. A refusal: say what it said and do not retry.
6. Afterwards, relay its answer: it says who it is for, when and where,
   whether the time was changed (a reschedule) or the place, and whether
   the employee was told — when they were not reached it says to tell
   them yourself, so do. The onboarding finishes only after the
   orientation is held, when HR marks it completed.
Safe to repeat; it also moves an orientation already confirmed.
""" + RELAY

COMPLETE_ORIENTATION = """Mark a new employee's HR orientation as held: the last step of their
onboarding, which is complete only after it. HR confirms it first.

HRMS admins only. Use it when HR says the orientation HAS happened —
"Mark Ethan's orientation as completed", "Ethan's orientation is done".
1. employee is the name HR used, or their email or code. When the tool
   says the words fit several people, ask which.
2. Never offer it, and never call it, before HR says the orientation has
   been held: confirming a time only schedules it. If HR asks right after
   confirming a time, say it is for after the orientation.
3. Its card is HR's confirmation — never ask "shall I go ahead?"
   yourself. A refusal (for example, no time has been confirmed yet): say
   what it said and do not retry.
4. Afterwards, relay its answer: it says whose orientation it is and
   whether their onboarding is now complete or what is still open.
Safe to repeat.
""" + RELAY


def request_lines(requests: list[dict[str, Any]]) -> str:
    """Who is waiting, and each time they are free — with the date and time as
    confirm_orientation takes them."""
    if not requests:
        return NO_REQUESTS
    lines = []
    for r in requests:
        who = f"{r.get('employee')} ({r.get('employee_code')})"
        lines.append(f"{who} is free at:")
        for i, o in enumerate(r.get("options") or [], start=1):
            lines.append(f"  {i}. {o.get('label')} — date {o.get('date')}, time {o.get('time')}")
    lines.append(
        "To confirm one, HR says when and where, for example: "
        "Confirm Ethan's orientation for Tuesday 11am in Room 2."
    )
    return "\n".join(lines)


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_orientation_requests(ctx: Context) -> str:
        """The HR orientation times new employees have proposed and HR has yet to
        answer: who, and each time they are free, with its date and time.

        HRMS admins only: anyone else is told so — tell them, citing it, and
        stop. Call it when HR asks which orientations are waiting, or before
        confirming one, and answer from it, citing it.
        """
        try:
            found = await ask_admin(hrms, ctx, "list_orientation_requests", {})
        except NotAdmin:
            return NOT_HR_ADMIN
        if not isinstance(found, dict):
            raise ToolError("The HRMS did not say which orientations are waiting.")
        return request_lines(list(found.get("requests") or []))

    @server.tool(description=CONFIRM_ORIENTATION, annotations=FILES, structured_output=False)
    async def confirm_orientation(
        employee: str, date: str, time: str, place: str, ctx: Context
    ) -> str:
        if not place.strip():
            raise ToolError("Where is it? Ask HR for a room or a meeting link.")
        try:
            result = await ask_admin(
                hrms,
                ctx,
                "confirm_orientation",
                {
                    "employee": employee.strip(),
                    "date": date.strip(),
                    "time": time.strip(),
                    "place": place.strip(),
                },
            )
        except NotAdmin:
            raise ToolError(NOT_HR_ADMIN) from None
        return say.orientation_confirmed(result)

    @server.tool(description=COMPLETE_ORIENTATION, annotations=FILES, structured_output=False)
    async def complete_orientation(employee: str, ctx: Context) -> str:
        try:
            result = await ask_admin(
                hrms, ctx, "complete_orientation", {"employee": employee.strip()}
            )
        except NotAdmin:
            raise ToolError(NOT_HR_ADMIN) from None
        return say.orientation_completed(result)
