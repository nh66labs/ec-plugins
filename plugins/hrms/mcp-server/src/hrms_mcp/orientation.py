"""A new employee's HR orientation, from HR's side, through the assistant.

The employee proposes their times with the date and time pickers the HR bot
sends them (not through a tool); every HR admin is told in a direct message and
answers in the same chat. These two tools are that answer: see what is waiting,
then confirm one time with a place. Confirming completes the onboarding step and
the employee is sent the date, time and place.

HRMS admins only: anyone else is told so as an answer, which the reply can cite.
The place is never guessed. These tools' descriptions carry how the conversation
runs, because the server's instructions are at the platform's limit.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.onboarding import NOT_HR_ADMIN, NotAdmin, as_json, ask_admin
from hrms_mcp.tools import READ_ONLY
from hrms_mcp.writes import FILES

NO_REQUESTS = "No new employee is waiting for their HR orientation to be confirmed."


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

    @server.tool(annotations=FILES, structured_output=False)
    async def confirm_orientation(
        employee: str, date: str, time: str, place: str, ctx: Context
    ) -> str:
        """Confirm a new employee's HR orientation for a date, time and place.
        HR confirms it first. It completes that onboarding step.

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
        6. Afterwards, say who it is for, when and where, and that the "HR
           orientation scheduled" step is complete. Then say whether the employee
           was told, from employee_told: "sent" — they have the date, time and
           place; anything else ("failed", "skipped", "not_sent") — they were NOT
           reached, so HR should let them know. Never say they were told unless
           it says "sent".
        Safe to repeat; it also moves an orientation already confirmed.
        """
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
        if not isinstance(result, dict) or not result.get("when"):
            raise ToolError("The HRMS did not confirm the orientation. Check the Onboarding card.")
        return as_json(result)
