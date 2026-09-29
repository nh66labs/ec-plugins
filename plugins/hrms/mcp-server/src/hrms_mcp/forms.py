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

Everything here is the HRMS's knowledge; the platform knows nothing about leave.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import _ask, kind_of, portion_of, when_of

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

PORTIONS = (("Full Day", "Full day"), ("First Half", "First half"), ("Second Half", "Second half"))

#: The HRMS's leave types, by its own names, in the order a person expects them.
LEAVE_TYPES = ("Casual Leave", "Sick Leave", "Floating Leave")


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def register(server: MCPServer, hrms: Hrms) -> None:
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
        known: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
        questions: list[dict[str, Any]] = []

        balances = {
            str(b.get("type", "")): b for b in await _ask(hrms, ctx, "get_leave_balance", {}) or []
        }
        chosen_type = next((t for t in LEAVE_TYPES if t.lower() == leave_type.strip().lower()), "")
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

        chosen_portion = next(
            (value for value, _ in PORTIONS if portion_of(value) == portion_of(day_portion)), ""
        )
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
            wanted = approver.strip().lower()
            named = next((m for m in managers if wanted and wanted in m["name"].lower()), None)
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
        missing = ", ".join(q["label"].lower() for q in questions) or "nothing"
        form = {
            "prompt": prompt,
            "questions": questions,
            "action": {"tool": "apply_leave", "arguments": known},
            "check": {"tool": "preview_leave"},
        }
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
