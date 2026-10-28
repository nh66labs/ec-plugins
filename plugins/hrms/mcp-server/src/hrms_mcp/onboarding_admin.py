"""HR manages everyone's onboarding through the assistant: who is being onboarded,
one person's status, retrying a failed step, excusing a step or requiring it
again, and starting an onboarding for an existing employee.

These tools carry no wording. Each passes HR's request to the HRMS and hands the
HRMS's data back as it is; how the assistant lays that out, what it asks and
when it proposes a change is said in the descriptions below, because that is
where the platform lets it be changed without a release of the code. The HRMS
decides who may do any of it (HRMS admins only), resolves the person, and
matches HR's words for a step against the steps themselves, so no list of step
names lives here.

None completes a step, and none is offered: a step is completed only when what
it stands for is verified.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.onboarding import NOT_HR_ADMIN, NotAdmin, as_json, ask_admin
from hrms_mcp.tools import READ_ONLY
from hrms_mcp.writes import FILES

#: Said at the end of every change tool's description.
NEVER_COMPLETE = (
    "Never offer to mark a step completed, and never say one is: a step is completed "
    "only when it is verified (a sign-in, an upload, a join, a check) — if HR asks for "
    "that, say so and offer what can be done."
)


# --- The guides: what each tool is for and how the assistant uses it ---------------

LIST_ONBOARDINGS = """\
The new employees being onboarded now: each one's progress, the step
they are on, and anything waiting on HR.

HRMS admins only: anyone else is told so — tell them, citing it, and
stop. Call it when HR asks who is being onboarded, who is stuck, or what
needs them, and answer from it, citing it. One short line per person:
their name, the progress, the step they are on; after it, what needs HR,
when there is something. An empty list means nobody is being onboarded —
say so. Show an employee code only to tell two people apart.
"""

GET_STATUS = """\
One new employee's onboarding in full: every step with its status, any
note or error on it, the latest reminders sent, and the HR orientation.

HRMS admins only: anyone else is told so — tell them, citing it, and
stop. Call it when HR asks how someone's onboarding is going or what is
left, and to find which step failed before retrying it. employee is the
name HR used, or their email or code; when it fits several people the
tool says so — ask HR which. Lay the answer out in this order: a first
line with the name, the progress and when it started; then the steps in
order, each with its status in plain words (done, in progress, failed,
not required, not started) and its note or error after it; then, only if
there is one, the orientation and the latest reminders. Call a step by
its `step` name; never show a `key` or an id.
"""

RETRY_STEP = """\
Retry a failed onboarding step for a new employee. HR confirms it
first. If HR-Assist does the step itself (the setup email, the Slack
link, the Jira check) it is done again straight away.

HRMS admins only. 1. employee and step are HR's words ("Ethan", "setup
email") — pass them as said; the tool matches them to a person and a
step. 2. When HR names no step, call get_onboarding_status and propose
the one that failed; if several failed, ask which. 3. When the tool says
HR's words fit several steps or people, ask which. 4. Only a failed step
can be retried: if the tool says it is not, tell HR what state it is in.
5. Its card is HR's confirmation — never ask "shall I go ahead?" yourself.
6. Afterwards, say what the tool returned the step as now. Safe to repeat.
""" + NEVER_COMPLETE

MARK_NOT_REQUIRED = """\
Mark an onboarding step not required for a new employee, so it stops
counting towards their onboarding. HR confirms it first.

HRMS admins only. 1. employee and step are HR's words — pass them as said.
2. reason is HR's own, if they gave one ("they use another tracker");
never ask for one. 3. When the tool says the words fit several steps or
people, ask which. 4. A completed step cannot be changed — if the tool
says so, tell HR. 5. Its card is HR's confirmation — never ask "shall I go
ahead?". 6. Afterwards, say the progress the tool returned. Safe to
repeat; require_onboarding_step undoes it.
""" + NEVER_COMPLETE

REQUIRE_STEP = """\
Undo "not required": put an onboarding step back for a new employee,
so it counts again. HR confirms it first. If HR-Assist does the step
itself it is done again straight away.

HRMS admins only. employee and step are HR's words — pass them as said;
when they fit several, ask which. Its card is HR's confirmation — never
ask "shall I go ahead?". Afterwards, say the progress the tool returned.
Safe to repeat.
""" + NEVER_COMPLETE

START_ONBOARDING = """\
Start the onboarding of an employee who already exists, and send their
setup email. HR confirms it first.

HRMS admins only. Someone created through the assistant already has an
onboarding — use this only for an employee who was added another way.
employee is HR's words (a name, email or code); when they fit several
people, ask which. Its card is HR's confirmation — never ask "shall I go
ahead?". Safe to repeat: the tool says whether it started or had begun
already — say which.
"""


def register(server: MCPServer, hrms: Hrms) -> None:
    async def read(ctx: Context, tool: str, arguments: dict[str, Any]) -> str:
        """A read: anyone but an HRMS admin is told so, as an answer the reply can cite."""
        try:
            return as_json(await ask_admin(hrms, ctx, tool, arguments))
        except NotAdmin:
            return NOT_HR_ADMIN

    async def change(ctx: Context, tool: str, arguments: dict[str, Any]) -> str:
        """A change: anyone but an HRMS admin is refused."""
        try:
            return as_json(await ask_admin(hrms, ctx, tool, arguments))
        except NotAdmin:
            raise ToolError(NOT_HR_ADMIN) from None

    @server.tool(description=LIST_ONBOARDINGS, annotations=READ_ONLY, structured_output=False)
    async def list_onboardings(ctx: Context) -> str:
        return await read(ctx, "list_onboardings", {})

    @server.tool(description=GET_STATUS, annotations=READ_ONLY, structured_output=False)
    async def get_onboarding_status(employee: str, ctx: Context) -> str:
        return await read(ctx, "get_onboarding_status", {"employee": employee.strip()})

    @server.tool(description=RETRY_STEP, annotations=FILES, structured_output=False)
    async def retry_onboarding_step(employee: str, step: str, ctx: Context) -> str:
        return await change(
            ctx, "retry_onboarding_step", {"employee": employee.strip(), "step": step.strip()}
        )

    @server.tool(description=MARK_NOT_REQUIRED, annotations=FILES, structured_output=False)
    async def mark_onboarding_step_not_required(
        employee: str, step: str, ctx: Context, reason: str = ""
    ) -> str:
        return await change(
            ctx,
            "mark_onboarding_step_not_required",
            {"employee": employee.strip(), "step": step.strip(), "reason": reason.strip()},
        )

    @server.tool(description=REQUIRE_STEP, annotations=FILES, structured_output=False)
    async def require_onboarding_step(employee: str, step: str, ctx: Context) -> str:
        return await change(
            ctx, "require_onboarding_step", {"employee": employee.strip(), "step": step.strip()}
        )

    @server.tool(description=START_ONBOARDING, annotations=FILES, structured_output=False)
    async def start_onboarding(employee: str, ctx: Context) -> str:
        return await change(ctx, "start_onboarding", {"employee": employee.strip()})
