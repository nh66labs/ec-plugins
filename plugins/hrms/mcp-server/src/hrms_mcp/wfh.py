"""Working from home: the request, its check and its filing, as the person asking.

Work from home is not leave. The HRMS keeps it apart — its own requests, its own
weekly limit, no balance — and takes the same three things the HRMS's own WFH
form does: the dates, the WFH mode (full day, first half, second half) and a
reason. Never a leave type.

``start_wfh_request`` answers with a **form** for whatever of the mode and the
reason is missing, as ``start_leave_request`` does for leave (EC-D184), naming
``apply_wfh`` as the write and ``preview_wfh`` as the check before Confirm. No
Jira heads-up comes with the dates: the person is still working.

``apply_wfh`` is a write, confirmed by the person first, and **safe to repeat**:
the HRMS files a new request every time it is called, so it looks for the same
request among the person's own first. Like leave, it goes to every one of the
person's project managers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp.compoff import _today_of
from hrms_mcp.forms import PORTIONS, _portion_named
from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import (
    _approvers,
    _ask,
    dates_of,
    day_of,
    portion_of,
    warning_of,
    when_of,
)
from hrms_mcp.writes import FILES, UNDOES, _waiting_for, one_to_cancel

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

#: What the HRMS accepts as a WFH mode, by its own names.
WfhMode = Literal["Full Day", "First Half", "Second Half"]


def wfh_line(date_from: str, date_to: str, wfh_mode: str = "") -> str:
    """"Work from home on Mon 26 Oct 2026, full day" — the mode left out when
    none is known yet."""
    when = when_of(date_from, date_to)
    line = f"Work from home {'from' if ' to ' in when else 'on'} {when}"
    return f"{line}, {portion_of(wfh_mode)}" if wfh_mode else line


def over_limit(result: Any, date_from: str) -> str:
    """The Heads-up sentence when the HRMS says the request is over its weekly
    WFH limit; else nothing. The HRMS's own verdict, ``within_policy``, decides,
    and no total is named: it counts approved requests, not days, and only the
    week the request starts in — so neither is its limit, which mixes the two."""
    if not isinstance(result, dict):
        return ""
    facts = result.get("facts") if isinstance(result.get("facts"), dict) else {}
    within = result.get("within_policy", facts.get("within_policy"))
    if within is not False:
        return ""
    return (
        f"This goes over the weekly WFH limit for the week of {day_of(date_from)}; "
        "your managers see it as an exception."
    )


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=READ_ONLY)
    async def start_wfh_request(
        date_from: str,
        ctx: Context,
        date_to: str = "",
        wfh_mode: str = "",
        reason: str = "",
    ) -> CallToolResult:
        """Start a work-from-home (WFH) request for the person asking, from what
        they said. Working from home is not leave: never start_leave_request for it.

        Pass the dates (YYYY-MM-DD, taken from get_leave_context), and the WFH
        mode (full day, first half or second half) and the reason if they said
        them — nothing you did not hear. The person is shown what is missing as
        choices, then the request to confirm: do not ask them about the mode or
        the reason yourself, and do not call preview_wfh or apply_wfh. Dates are
        the one thing to ask for yourself, if they gave none.

        A day that is a weekend or a public holiday needs no WFH: tell the person
        so, naming the day or holiday, and call this only if they still want to.
        """
        date_from, date_to = dates_of(date_from, date_to)
        known: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
        questions: list[dict[str, Any]] = []

        mode = _portion_named(wfh_mode)
        if mode:
            known["wfh_mode"] = mode
        else:
            questions.append(
                {
                    "name": "wfh_mode",
                    "label": "WFH mode",
                    "kind": "choice",
                    "options": [{"value": value, "label": label} for value, label in PORTIONS],
                }
            )
        if reason.strip():
            known["reason"] = reason.strip()
        else:
            questions.append({"name": "reason", "label": "Reason", "kind": "text"})

        prompt = wfh_line(date_from, date_to, mode)
        prompt += " — choose the rest:" if questions else "."
        form = {
            "prompt": prompt,
            "questions": questions,
            "action": {"tool": "apply_wfh", "arguments": known},
            "check": {"tool": "preview_wfh"},
        }
        missing = ", ".join(q["label"].lower() for q in questions) or "nothing"
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"The person is shown the missing details as choices ({missing}), "
                        "then the request to confirm. Do not ask them about these yourself, "
                        "and do not call apply_wfh for this request."
                    ),
                )
            ],
            _meta={"ec/form": form},
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def preview_wfh(
        date_from: str,
        wfh_mode: WfhMode,
        ctx: Context,
        date_to: str = "",
        reason: str = "",
    ) -> str:
        """Check a work-from-home request before filing it, creating nothing:
        a heads-up when it takes the person past the weekly WFH limit, and
        who must approve it. Dates are
        YYYY-MM-DD; date_to defaults to date_from. Call this before apply_wfh
        with the same details. The answer is written to the person asking."""
        date_from, until = dates_of(date_from, date_to)
        # Independent of each other: side by side, the check waits for the slower only.
        result, approvers = await asyncio.gather(
            _ask(hrms, ctx, "preview_wfh", {"date_from": date_from, "date_to": until}),
            _approvers(hrms, ctx),
        )
        if not isinstance(result, dict):
            return str(result)
        head = wfh_line(date_from, until, wfh_mode)
        head += f" — reason: {reason.strip()}." if reason.strip() else "."
        sections = [
            warning_of([over_limit(result, date_from)]),
            head,
            f"{approvers}\nNothing is filed until you confirm.",
        ]
        return "\n\n".join(s for s in sections if s)

    @server.tool(annotations=FILES, structured_output=False)
    async def apply_wfh(
        date_from: str,
        wfh_mode: WfhMode,
        reason: str,
        ctx: Context,
        date_to: str = "",
    ) -> str:
        """File a work-from-home request for the person asking. They confirm it first.

        Call preview_wfh with the same details before this. Every argument comes
        from what the person said or from get_leave_context — never guess a WFH
        mode or a reason. Dates are YYYY-MM-DD; date_to defaults to date_from.
        It goes to every one of their project managers.
        """
        if not reason.strip():
            raise ToolError("A reason is needed. Ask the person why they need to work from home.")
        date_from, date_to = dates_of(date_from, date_to)
        what = f"work from home, {when_of(date_from, date_to)}, {portion_of(wfh_mode)}"
        for item in await _ask(hrms, ctx, "get_wfh_history", {}) or []:
            if (
                isinstance(item, dict)
                and str(item.get("status")) in ("Pending", "Approved")
                and str(item.get("date_from", ""))[:10] == date_from
                and str(item.get("date_to", ""))[:10] == date_to
                and portion_of(item.get("mode", "")) == portion_of(wfh_mode)
            ):
                return f"Already filed: {what} — {item.get('status')}. Nothing new was filed."

        # No project manager is sent: the HRMS sends it to every one of theirs.
        filed = await _ask(
            hrms,
            ctx,
            "apply_wfh",
            {
                "date_from": date_from,
                "date_to": date_to,
                "leave_mode": wfh_mode,
                "reason": reason.strip(),
            },
        )
        status = filed.get("status", "Pending") if isinstance(filed, dict) else "Pending"
        waiting = await _waiting_for(hrms, ctx) if status == "Pending" else ""
        return (
            f"Filed: {what[:1].upper()}{what[1:]} — reason: {reason.strip()}. "
            f"Status: {status}.{waiting}"
        )

    @server.tool(annotations=UNDOES, structured_output=False)
    async def cancel_my_wfh(date_from: str, ctx: Context) -> str:
        """Withdraw one of the person's own work-from-home requests, pending or
        approved and not yet over. date_from is the first day of the request
        (YYYY-MM-DD)."""
        date_from, _ = dates_of(date_from, "")
        history, today = await asyncio.gather(
            _ask(hrms, ctx, "get_wfh_history", {}), _today_of(hrms, ctx)
        )
        held = [
            item
            for item in history or []
            if isinstance(item, dict) and str(item.get("date_from", ""))[:10] == date_from
        ]

        def described(item: dict[str, Any]) -> str:
            line = wfh_line(
                date_from, str(item.get("date_to", ""))[:10] or date_from, str(item.get("mode", ""))
            )
            return f"{line[:1].lower()}{line[1:]}"

        item = one_to_cancel(
            held,
            none=(
                f"The person has no work-from-home request starting {day_of(date_from)} "
                "that can be cancelled."
            ),
            many=(
                f"The person has more than one work-from-home request starting "
                f"{day_of(date_from)}. Cancel it in the HRMS instead."
            ),
            already=described,
            ref="wfh_id",
        )
        if isinstance(item, str):
            return item
        ends = str(item.get("date_to", ""))[:10] or date_from
        if today and item.get("status") == "Approved" and ends < today.isoformat():
            raise ToolError(
                f"That work from home ({when_of(date_from, ends)}) is already over, so it "
                "is not cancelled here. Tell the person to ask HR."
            )
        await _ask(hrms, ctx, "cancel_wfh", {"wfh_id": str(item["wfh_id"])})
        return f"Cancelled: {described(item)[:1].upper()}{described(item)[1:]}."
