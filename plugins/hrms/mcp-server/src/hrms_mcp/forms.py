"""Starting a leave request: the choices that are missing, as a form (EC-D184).

The assistant passes whatever it understood from the person — "half-day leave
tomorrow for a doctor's appointment" gives a date, a day portion and a reason.
This tool works out what is still missing and answers with a **form**: the
missing things as choices the person clicks, with options from the HRMS itself
(their leave types with what is left of each, their project managers), naming
``apply_leave`` as the write the answers complete and ``preview_leave`` as the
check to run first.

The platform renders the form, collects the answers without a model, runs the
check, and shows the request to confirm. So a request takes one assistant turn,
not one per question — and the options can only be ones the HRMS offers.

**A heads-up comes with the dates.** When the dates fall around open Jira tickets
of the person's, the form's prompt says so before anything else is chosen — one
short sentence with the tickets' keys. The check before Confirm says it again in
full, with each ticket's status and deadline.

Everything here is the HRMS's knowledge; the platform knows nothing about leave.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp.hrms import Hrms
from hrms_mcp.jira import Jira
from hrms_mcp.tools import (
    HeadsUp,
    _ask,
    kind_of,
    manager_named,
    own_heads_up,
    portion_of,
    when_of,
)

log = logging.getLogger("hrms_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

PORTIONS = (("Full Day", "Full day"), ("First Half", "First half"), ("Second Half", "Second half"))

#: The HRMS's leave types, by its own names, in the order a person expects them.
LEAVE_TYPES = ("Casual Leave", "Sick Leave", "Floating Leave")

#: Most of a form's prompt the platform shows (``actions.form_of`` in Enterprise
#: Claw — keep the two the same); past it, the prompt is cut where it stands. The
#: heads-up is fitted to what the prompt leaves of it.
PROMPT_LIMIT = 300

#: Longest the form waits for the heads-up once the HRMS has given the form;
#: past it, the form goes without one.
HEADS_UP_WAIT_SECONDS = 2.0


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _type_named(leave_type: str) -> str:
    """The HRMS's leave type the person named, or "" when they named none."""
    return next((t for t in LEAVE_TYPES if t.lower() == leave_type.strip().lower()), "")


def _portion_named(day_portion: str) -> str:
    """The HRMS's day portion the person named, or "" when they named none."""
    asked = portion_of(day_portion)
    return next((value for value, _ in PORTIONS if portion_of(value) == asked), "")


async def form_for(
    hrms: Hrms,
    ctx: Context,
    date_from: str,
    date_to: str,
    leave_type: str,
    day_portion: str,
    reason: str,
    approver: str,
) -> dict[str, Any]:
    """The form for what is still missing, with options from the HRMS."""
    known: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
    questions: list[dict[str, Any]] = []

    balances = {
        str(b.get("type", "")): b for b in await _ask(hrms, ctx, "get_leave_balance", {}) or []
    }
    chosen_type = _type_named(leave_type)
    if chosen_type:
        known["leave_type"] = chosen_type
    else:
        questions.append(
            {
                "name": "leave_type",
                "label": "Leave type",
                "kind": "choice",
                "options": [
                    {
                        "value": name,
                        "label": (
                            f"{name} · {balances[name].get('available', 0):g} left"
                            if name in balances
                            else name
                        ),
                    }
                    for name in LEAVE_TYPES
                ],
            }
        )

    chosen_portion = _portion_named(day_portion)
    if chosen_portion:
        known["day_portion"] = chosen_portion
    else:
        questions.append(
            {
                "name": "day_portion",
                "label": "Full or half day",
                "kind": "choice",
                "options": [{"value": value, "label": label} for value, label in PORTIONS],
            }
        )

    if reason.strip():
        known["reason"] = reason.strip()
    else:
        questions.append({"name": "reason", "label": "Reason", "kind": "text"})

    managers = (await _ask(hrms, ctx, "get_employee_project_managers", {}) or {}).get(
        "project_managers", []
    )
    if len(managers) > 1:
        named = manager_named(managers, approver)
        if named is not None:
            known["approver"] = named["name"]
        else:
            questions.append(
                {
                    "name": "approver",
                    "label": "Who should approve it",
                    "kind": "choice",
                    "options": [{"value": m["name"], "label": m["name"]} for m in managers],
                }
            )

    what = kind_of(chosen_type) if chosen_type else "Leave"
    prompt = f"{what} for {when_of(date_from, date_to)}"
    if chosen_portion:
        prompt += f", {portion_of(chosen_portion)}"
    prompt += " — choose the rest:" if questions else "."
    return {
        "prompt": prompt,
        "questions": questions,
        "action": {"tool": "apply_leave", "arguments": known},
        "check": {"tool": "preview_leave"},
    }


#: Heads-ups still being read after the form went without them. Held so they
#: are not collected half-read: what was read, once read, is kept for the next.
_LATE: set[asyncio.Task[HeadsUp | None]] = set()


async def _read_heads_up(
    hrms: Hrms, jira: Jira | None, ctx: Context, date_from: str, date_to: str
) -> HeadsUp | None:
    """``own_heads_up``, or None when anything goes wrong. A slow Jira, or an
    HRMS that answers oddly, never fails the form: the check before Confirm
    still says it in full."""
    try:
        return await own_heads_up(hrms, jira, ctx, date_from, date_to)
    except Exception:
        log.info("leave heads-up skipped")
        return None


async def _heads_up(reading: asyncio.Task[HeadsUp | None]) -> HeadsUp | None:
    """The heads-up, if it comes within ``HEADS_UP_WAIT_SECONDS`` of the form;
    else none. Past that — or when the form itself is called off — the read goes
    on alone rather than being thrown away."""
    try:
        return await asyncio.wait_for(asyncio.shield(reading), HEADS_UP_WAIT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as stopped:
        _LATE.add(reading)
        reading.add_done_callback(_LATE.discard)
        if isinstance(stopped, asyncio.CancelledError):
            raise
        log.info("leave heads-up skipped: slow")
        return None


def register(server: MCPServer, hrms: Hrms, jira: Jira | None = None) -> None:
    @server.tool(annotations=READ_ONLY)
    async def start_leave_request(
        date_from: str,
        ctx: Context,
        date_to: str = "",
        leave_type: str = "",
        day_portion: str = "",
        reason: str = "",
        approver: str = "",
    ) -> CallToolResult:
        """Start a leave request for the person asking, from what they said.

        Pass every detail you understood — the dates (YYYY-MM-DD, taken from
        get_leave_context), and the leave type, full or half day, reason and
        approver if they said them — and nothing you did not hear. Leave out
        what they did not say: the person is shown the missing things as
        choices, and nothing is filed until they confirm. Call this instead of
        asking them about the leave type, full or half day, the reason or the
        approver. Dates are the one thing to ask for yourself, if they gave none.
        """
        if not _valid_date(date_from):
            raise ToolError(
                "A start date (YYYY-MM-DD) is needed. Ask the person which day, "
                "then call this again."
            )
        date_to = date_to if _valid_date(date_to) else date_from

        def read() -> asyncio.Task[HeadsUp | None]:
            return asyncio.create_task(_read_heads_up(hrms, jira, ctx, date_from, date_to))

        # Read beside the HRMS, so the form waits only for the slower, unless
        # nothing can be left to choose: with every detail named, the check runs
        # at once and says it in full — said here as well, it would be read twice
        # in a row. An approver not named may still be asked (several managers);
        # if it is not, the read is called off.
        asks = not (
            _type_named(leave_type) and _portion_named(day_portion) and reason.strip()
            and approver.strip()
        )
        reading = read() if asks else None
        try:
            form = await form_for(
                hrms, ctx, date_from, date_to, leave_type, day_portion, reason, approver
            )
        except BaseException:
            if reading is not None:
                reading.cancel()
            raise
        if not form["questions"]:
            if reading is not None:
                reading.cancel()
        elif say := await _heads_up(reading or read()):
            # Fitted to the prompt as it is, beside what the platform shows.
            if heads_up := say(PROMPT_LIMIT - len(form["prompt"]) - len("\n\n")):
                form["prompt"] = f"{heads_up}\n\n{form['prompt']}"
        missing = ", ".join(q["label"].lower() for q in form["questions"]) or "nothing"
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"The person is shown the missing details as choices ({missing}), "
                        "then the request to confirm. Do not ask them about these yourself, "
                        "and do not call apply_leave for this request."
                    ),
                )
            ],
            _meta={"ec/form": form},
        )
