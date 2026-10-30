"""An employee's own onboarding, through the assistant: where it stands, what is
still needed from them, and confirming their profile.

Both tools act on the person asking and nobody else — the HRMS takes them from
the signed identity. What the employee still has to fill in (PAN, Aadhaar, bank
details) is named, never asked for: those go in on their profile, never in a
chat, and the answer links them there.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import READ_ONLY, _ask
from hrms_mcp.writes import FILES

#: The codes HR-Assist puts at the start of a refusal, so none of this matches wording.
CODE_NO_ONBOARDING = "[no_onboarding]"
CODE_PROFILE_INCOMPLETE = "[profile_incomplete]"


def _label(step: dict[str, Any]) -> str:
    """A step as the employee reads it; its key if the HRMS sent no label."""
    return str(step.get("label") or step.get("key") or "a step")


#: What the asker is told when they have no onboarding — an answer, not a
#: failure, so it can be cited.
NO_ONBOARDING = "You have no onboarding in progress, so there is nothing left to do."

#: The steps the employee does themselves; the rest are HR's, or are checked
#: by HR-Assist (an invite sent, Jira access granted).
THEIRS = {"first_login", "slack_joined", "documents_uploaded", "profile_confirmed"}

#: The steps the employee does before they are asked for their orientation times.
BEFORE_ORIENTATION = ("first_login", "documents_uploaded", "profile_confirmed")


def _profile_links(dashboard_url: str, employee_id: str) -> tuple[str, str]:
    """Links to their profile and to editing it — ("", "") when the dashboard's
    address or their id is not known, and the page is then named, not linked."""
    base = dashboard_url.strip().rstrip("/")
    if base and employee_id:
        return f"{base}/employees/{employee_id}", f"{base}/employees/{employee_id}/edit"
    return "", ""


def _at(page: str, link: str) -> str:
    """"on My Profile → Documents: <link>", or "… in HR-Assist" with no link."""
    return f"on {page}: {link}" if link else f"on {page} in HR-Assist"


def _joined(items: list[str]) -> str:
    return ", ".join(items)


def _orientation_lines(ob: dict[str, Any], steps: list[dict[str, Any]], profile: str) -> list[str]:
    """Where the HR orientation stands, for the employee."""
    orient = ob.get("orientation") or {}
    status = {s.get("key"): s.get("status") for s in steps}
    if orient.get("status") == "confirmed":
        return [f"Your HR orientation: {orient.get('confirmed_label')} — {orient.get('place')}."]
    if orient.get("status") == "requested":
        times = "; ".join(orient.get("slot_labels") or [])
        return [f"You've proposed times for your HR orientation ({times}); HR will confirm one."]
    if status.get("orientation_scheduled") == "pending" and all(
        status.get(key) in ("completed", "not_required") for key in BEFORE_ORIENTATION
    ):
        where = f"the date and time pickers in my message, or {_at('My Profile', profile)}"
        return [f"Choose two or more times for your HR orientation with {where}."]
    return []


def onboarding_lines(ob: dict[str, Any], dashboard_url: str) -> str:
    """The asker's onboarding as a few lines: progress, what is done, what is
    left (and whose it is), and what their profile and documents still need."""
    progress = ob.get("progress") or {}
    done, total = progress.get("done", 0), progress.get("total", 0)
    profile, edit = _profile_links(dashboard_url, str(ob.get("employee_id") or ""))
    steps = ob.get("steps") or []
    if ob.get("status") == "completed":
        return "Your onboarding is complete — every step is done."

    lines = [f"Your onboarding: {done} of {total} steps done."]
    finished = [_label(s) for s in steps if s.get("status") == "completed"]
    if finished:
        lines.append("Done: " + _joined(finished) + ".")
    left = [s for s in steps if s.get("status") not in ("completed", "not_required")]
    # Orientation is theirs only until they have proposed times; then it is HR's.
    def theirs(step: dict[str, Any]) -> bool:
        return step.get("key") in THEIRS or (
            step.get("key") == "orientation_scheduled" and step.get("status") == "pending"
        )

    yours = [_label(s) for s in left if theirs(s)]
    others = [_label(s) for s in left if not theirs(s)]
    if yours:
        lines.append("Still to do, by you: " + _joined(yours) + ".")
    if others:
        lines.append("Still to do, by HR: " + _joined(others) + ".")

    lines.extend(_orientation_lines(ob, steps, profile))
    gaps = ob.get("profile_gaps") or []
    if gaps:
        lines.append(
            f"Before you can confirm your profile, fill in your {_joined(gaps)} "
            f"{_at('My Profile → Edit Profile', edit)}, or ask me to send you the private "
            "profile form in Slack"
        )
    elif any(s.get("key") == "profile_confirmed" and s.get("status") != "completed" for s in steps):
        lines.append("Your profile is complete: you can confirm it now.")
    missing = ob.get("missing_documents") or []
    if missing:
        lines.append(f"Documents still needed: {_joined(missing)} — upload them "
                     f"{_at('My Profile → Documents', profile)}")
    return "\n".join(lines)


def register(server: MCPServer, hrms: Hrms, dashboard_url: str = "") -> None:
    async def _my_id(ctx: Context) -> str:
        """The asker's employee id, for a link to their own profile — "" when
        the HRMS will not say, and the link then names the page instead."""
        try:
            session = await _ask(hrms, ctx, "get_user_session", {})
        except ToolError:
            return ""
        return str(session.get("employee_id") or "") if isinstance(session, dict) else ""

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_my_onboarding(ctx: Context) -> str:
        """The asking employee's own onboarding: what is done, what is left and
        whose it is, and what their profile and documents still need.

        Call it when someone asks about their onboarding, what is left, or
        whether they can confirm their profile, and answer from it, citing
        it. It names missing details (PAN, Aadhaar, bank details) — never ask
        anyone to type those into the chat; offer to send them the private
        profile form (start_profile_update), or point them to the link it gives.
        """
        try:
            ob = await _ask(hrms, ctx, "get_my_onboarding", {})
        except ToolError as error:
            if CODE_NO_ONBOARDING in str(error):
                return NO_ONBOARDING
            raise
        if not isinstance(ob, dict):
            raise ToolError("The HRMS did not say where your onboarding stands.")
        return onboarding_lines(ob, dashboard_url)

    @server.tool(annotations=FILES, structured_output=False)
    async def confirm_my_profile(ctx: Context) -> str:
        """The asking employee confirms their own profile is complete and
        correct, completing that onboarding step. They confirm it first.

        Call get_my_onboarding before this, and propose it only when that
        says the profile is complete; otherwise tell them what to fill in and
        where. Safe to repeat: a profile already confirmed stays confirmed.
        """
        try:
            ob = await _ask(hrms, ctx, "confirm_my_profile", {})
        except ToolError as error:
            said = str(error)
            if CODE_NO_ONBOARDING in said:
                raise ToolError(NO_ONBOARDING) from None
            if CODE_PROFILE_INCOMPLETE in said:
                # What is missing follows the code, as data; the sentence is ours.
                gaps = said.split(CODE_PROFILE_INCOMPLETE, 1)[1].strip()
                _, edit = _profile_links(dashboard_url, await _my_id(ctx))
                raise ToolError(
                    f"Fill in your {gaps} {_at('My Profile → Edit Profile', edit)}, "
                    "then confirm. Nothing was confirmed."
                ) from None
            raise
        progress = (ob or {}).get("progress") or {} if isinstance(ob, dict) else {}
        return ("Your profile is confirmed. "
                f"Onboarding: {progress.get('done', 0)} of {progress.get('total', 0)} steps done.")
