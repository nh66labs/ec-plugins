"""An employee adding or changing their own profile details, through the
assistant — without the details ever passing through it.

The one tool sends the person asking a private Slack message with a button; the
form it opens sends what is typed straight to the HRMS, so a phone number, PAN,
Aadhaar or bank detail is never in this chat, the model's context or a log. The
HRMS takes the asker from the signed identity, so the form can only be sent to
the person who asked.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.my_onboarding import _at, _profile_links
from hrms_mcp.tools import _ask
from hrms_mcp.writes import FILES

log = logging.getLogger("hrms_mcp")

#: How the HRMS says the form went.
FORM_SENT = "sent"
FORM_NOT_IN_SLACK = "not_in_slack"
FORM_SLACK_OFF = "slack_off"

START_PROFILE_UPDATE = """Sends the asking employee a private message in Slack with an
"Update my profile" button. The button opens a form where they add or change their
phone, address, date of birth, PAN, Aadhaar and bank details.

Call it whenever someone wants to add, fill in, change or correct their profile
details, or asks how. Then tell them what the answer says — that the form is in
their Slack messages, or where else to go. NEVER ask for, accept or repeat those
details in the chat, and never put them in a tool call: if someone types a PAN,
Aadhaar, bank or other personal detail, do not use it, and tell them to enter it
in the form instead. A change to the bank account on file is confirmed by a link
emailed to them before it takes effect — say so if they ask.
"""


def sent_answer() -> str:
    return (
        "I've sent you a private message here in Slack with an \"Update my profile\" "
        "button. Press it to open the form — what you type there goes straight to "
        "HR-Assist, not through this chat. A change to the bank account already on "
        "file is confirmed by a link emailed to your company address before it takes effect."
    )


def elsewhere_answer(why: str, dashboard_url: str, employee_id: str) -> str:
    """Where to go when the form could not be sent in Slack."""
    _, edit = _profile_links(dashboard_url, employee_id)
    return (
        f"I couldn't send the form in Slack ({why}). Update your details "
        f"{_at('My Profile → Edit Profile', edit)}. Please don't type them into this chat."
    )


def register(server: MCPServer, hrms: Hrms, dashboard_url: str = "") -> None:
    @server.tool(annotations=FILES, structured_output=False, description=START_PROFILE_UPDATE)
    async def start_profile_update(ctx: Context) -> str:
        try:
            sent = await _ask(hrms, ctx, "start_profile_update", {})
        except ToolError as e:
            log.warning("start_profile_update failed: %s", e)
            sent = {}
        outcome = str(sent.get("form") or "") if isinstance(sent, dict) else ""
        if outcome == FORM_SENT:
            return sent_answer()
        try:
            session = await _ask(hrms, ctx, "get_user_session", {})
        except ToolError:
            session = {}
        employee_id = str(session.get("employee_id") or "") if isinstance(session, dict) else ""
        why = {
            FORM_NOT_IN_SLACK: "you aren't in the company Slack yet",
            FORM_SLACK_OFF: "Slack messages from the HR assistant are switched off",
        }.get(outcome, "something went wrong sending it — you can try asking again in a minute")
        return elsewhere_answer(why, dashboard_url, employee_id)
