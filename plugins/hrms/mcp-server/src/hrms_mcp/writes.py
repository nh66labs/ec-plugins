"""The leave tools that change something in the HRMS, as the person asking.

Every tool here is annotated as a write, so a platform that confirms writes —
Enterprise Claw does, always — shows the person exactly what will be sent and
runs it only once they accept. What a tool here receives is therefore what the
person saw and agreed to.

Each one is **safe to repeat**. A confirmed action can be delivered twice (a
retry after a lost answer), and the HRMS files a new request every time
``apply_leave`` is called. So before acting each tool looks at the HRMS's own
view of the request and, when the thing it would do is already done, reports
that rather than doing it again. Approving or rejecting something already
decided is reported the same way the HRMS reports it: as done.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from hrms_mcp.hrms import Hrms, HrmsError
from hrms_mcp.tools import (
    DayPortion,
    LeaveType,
    _ask,
    _identity,
    kind_of,
    portion_of,
    same_kind,
    when_of,
)

FILES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
UNDOES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


async def _mine(hrms: Hrms, ctx: Context) -> list[dict[str, Any]]:
    return list(await _ask(hrms, ctx, "get_leaves", {}) or [])


async def _checked(
    hrms: Hrms, ctx: Context, leave_id: str, employee: str
) -> tuple[dict[str, Any], str]:
    """The request, and whose it is — refused unless ``employee`` names that person.

    The card a person confirms shows the employee's name beside the request's
    reference; checking the two agree here means a card can never read as one
    person's leave while acting on another's.
    """
    item = await _to_decide(hrms, ctx, leave_id)
    who = await _whose(hrms, ctx, leave_id)
    if not who:
        raise ToolError(
            "The HRMS did not say whose request that is, so it cannot be decided here. "
            "Decide it in the HRMS instead."
        )
    if _name(employee) != _name(who):
        raise ToolError(
            f"That request is {who}'s, not {employee}'s. Look it up again with "
            "list_leave_requests_to_decide."
        )
    return item, who


def _name(name: str) -> str:
    """A name as compared: whitespace collapsed, case ignored."""
    return " ".join(name.split()).casefold()


async def _whose(hrms: Hrms, ctx: Context, leave_id: str) -> str:
    """The name of the employee whose request it is, or "" when the HRMS gives none."""
    try:
        routed = await hrms.call("get_leave_routing", {"leave_id": leave_id}, _identity(ctx))
    except HrmsError as error:
        raise ToolError(str(error)) from None
    if not isinstance(routed, dict):
        return ""
    return " ".join(str(routed.get("employee") or "").split())


def _decided(item: dict[str, Any], who: str) -> str:
    """"Navaneeth K's casual leave for Thu 1 Oct 2026, full day"."""
    when = when_of(str(item.get("date_from", "")), str(item.get("date_to", "")))
    return (
        f"{who}'s {kind_of(item.get('type', '')).lower()} for {when}, "
        f"{portion_of(item.get('mode', ''))}"
    )


async def _to_decide(hrms: Hrms, ctx: Context, leave_id: str) -> dict[str, Any]:
    """The request, if it is one the asker may decide — found in their own list,
    which the HRMS scopes to their team, or to everyone for HR."""
    for status in ("Pending", "Approved", "Rejected"):
        try:
            leaves = await hrms.call("get_team_leaves", {"status": status}, _identity(ctx))
        except HrmsError as error:
            raise ToolError(str(error)) from None
        for item in leaves or []:
            if str(item.get("leave_id")) == leave_id:
                return item
    raise ToolError(
        "That is not a leave request waiting on the person asking. Look it up again "
        "with list_leave_requests_to_decide."
    )


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=FILES, structured_output=False)
    async def apply_leave(
        leave_type: LeaveType,
        date_from: str,
        day_portion: DayPortion,
        reason: str,
        ctx: Context,
        date_to: str = "",
        approver: str = "",
    ) -> str:
        """File a leave request for the person asking. They confirm it first.

        Call preview_leave with the same details before this. Every argument
        comes from what the person said or from get_leave_context — never guess
        a leave type, a day portion or a reason. Dates are YYYY-MM-DD; date_to
        defaults to date_from. approver is the name of the project manager who
        should approve, needed only when they have more than one.
        """
        if not reason.strip():
            raise ToolError("A reason is needed. Ask the person why they need the leave.")
        date_to = date_to or date_from
        for item in await _mine(hrms, ctx):
            if (
                str(item.get("status")) in ("Pending", "Approved")
                and str(item.get("leave_date_from", ""))[:10] == date_from
                and str(item.get("leave_date_to", ""))[:10] == date_to
                and same_kind(item.get("leave_type", ""), leave_type)
                and portion_of(item.get("leave_mode", "")) == portion_of(day_portion)
            ):
                return (
                    f"Already filed: {kind_of(leave_type)}, {when_of(date_from, date_to)}, "
                    f"{portion_of(day_portion)} — {item.get('status')}. Nothing new was filed."
                )

        arguments: dict[str, Any] = {
            "leave_type": leave_type,
            "leave_mode": day_portion,
            "date_from": date_from,
            "date_to": date_to,
            "reason": reason.strip(),
        }
        managers = (await _ask(hrms, ctx, "get_employee_project_managers", {}) or {}).get(
            "project_managers", []
        )
        chosen = None
        if approver.strip():
            wanted = approver.strip().lower()
            chosen = next((m for m in managers if m.get("name", "").lower() == wanted), None)
            if chosen is None:
                chosen = next((m for m in managers if wanted in m.get("name", "").lower()), None)
            if chosen is None:
                names = ", ".join(m.get("name", "") for m in managers) or "none"
                raise ToolError(f"{approver} is not one of their project managers ({names}).")
            arguments["project_manager_id"] = chosen["id"]
        elif len(managers) == 1:
            chosen = managers[0]

        filed = await _ask(hrms, ctx, "apply_leave", arguments)
        status = filed.get("status", "Pending") if isinstance(filed, dict) else "Pending"
        waiting = (
            f" It is waiting for {chosen['name']} to approve."
            if chosen and status == "Pending"
            else ""
        )
        return (
            f"Filed: {kind_of(leave_type)}, {when_of(date_from, date_to)}, "
            f"{portion_of(day_portion)} — reason: {reason.strip()}. Status: {status}.{waiting}"
        )

    @server.tool(annotations=UNDOES, structured_output=False)
    async def cancel_my_leave(request_id: str, ctx: Context) -> str:
        """Withdraw one of the person's own leave requests, pending or approved.
        request_id comes from list_my_leaves."""
        mine = {str(item.get("id")): item for item in await _mine(hrms, ctx)}
        item = mine.get(request_id)
        if item is None:
            raise ToolError("That is not one of the person's leave requests.")
        what = (
            f"{kind_of(item.get('leave_type', ''))}, "
            f"{when_of(str(item.get('leave_date_from', '')), str(item.get('leave_date_to', '')))}"
        )
        if item.get("status") == "Cancelled":
            return f"Already cancelled: {what}. Nothing changed."
        await _ask(hrms, ctx, "cancel_leave", {"leave_id": request_id})
        return f"Cancelled: {what}."

    @server.tool(annotations=FILES, structured_output=False)
    async def approve_leave(
        request_id: str, employee: str, ctx: Context, note: str = ""
    ) -> str:
        """Approve a leave request waiting on the person asking. request_id and
        employee (the name of the person whose leave it is) both come from
        list_leave_requests_to_decide. note is optional, for the employee."""
        item, who = await _checked(hrms, ctx, request_id, employee)
        if item.get("status") == "Approved":
            return f"Already approved: {_decided(item, who)}. Nothing changed."
        arguments: dict[str, Any] = {"leave_id": request_id}
        if note.strip():
            arguments["notes"] = note.strip()
        await _ask(hrms, ctx, "approve_leave", arguments)
        return f"Approved {_decided(item, who)}. The HRMS tells {who}."

    @server.tool(annotations=UNDOES, structured_output=False)
    async def reject_leave(request_id: str, employee: str, reason: str, ctx: Context) -> str:
        """Reject a leave request waiting on the person asking, with the reason the
        employee will be told. request_id and employee (the name of the person
        whose leave it is) both come from list_leave_requests_to_decide.

        reason is required. If the person already said why ("because of the
        pending deployment"), use their words and do not ask again; only when
        they gave none, ask for it.
        """
        said = reason.strip().rstrip(".")
        if not said:
            raise ToolError("A reason is needed to reject. Ask the person why.")
        item, who = await _checked(hrms, ctx, request_id, employee)
        if item.get("status") == "Rejected":
            return f"Already rejected: {_decided(item, who)}. Nothing changed."
        await _ask(hrms, ctx, "reject_leave", {"leave_id": request_id, "reason": said})
        return f"Rejected {_decided(item, who)}. Reason given: {said}. The HRMS tells {who}."
