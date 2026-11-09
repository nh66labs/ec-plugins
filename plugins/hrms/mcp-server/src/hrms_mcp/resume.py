"""Getting an employee's resume through the assistant.

HR can ask for anyone's resume; everyone else gets their own. The HRMS builds the
resume and sends the file to the *asker's own* Slack DM — it is never sent to the
person it is about, and the file never passes through this chat. This tool only
works out whose resume is wanted and hands the request to the HRMS, which checks
again who may have it.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import READ_ONLY, _ask

GET_RESUME = """Gets an employee's resume (CV). Call this whenever anyone asks to see, show,
view, download or get a resume or CV, theirs or someone else's. Do NOT search the
company's knowledge first and never answer "no resume found": a resume is not a stored
document, the HRMS builds it fresh from the employee's profile, projects and timesheets.

It sends the resume as a Word file to the asking person's own Slack messages, and with
mode "view" also returns a short summary to show here. employee is the name HR used, or
an email or employee code — this is the one tool where HR names another person, so pass
the name as given; leave it empty for the asker's own resume. Everyone can get their own;
only HR admins can get someone else's — if anyone else asks for another person's, tell
them so and stop.

Run it straight away — never ask for confirmation first. mode is "view" for "show me ..."
(reply with the summary, then one short line that the file is in their Slack messages, and
nothing else) and "download" for "download / send me ..." (just that one line). The file
goes to the asker, never to the person the resume is about. When the name fits several
people the tool lists them — ask which one. Never invent resume content: say only what
the tool returned.
"""

NOT_YOURS = "You can only get your own resume. HR can get anyone's."


def _people(found: Any) -> list[dict[str, Any]]:
    if not isinstance(found, list):
        return []
    return [p for p in found if isinstance(p, dict) and p.get("id")]


def pick(found: Any, wanted: str) -> tuple[dict[str, Any] | None, list[str]]:
    """The one person meant, or None and the names it could be."""
    people = _people(found)
    exact = [p for p in people if str(p.get("name") or "").casefold() == wanted.casefold()]
    chosen = exact or people
    if len(chosen) == 1:
        return chosen[0], []
    return None, [f"{p.get('name')} ({p.get('employee_code')})" for p in chosen]


def sent_answer(name: str, mode: str, summary: str, failed: str = "") -> str:
    if failed:
        # Never claim a file was sent when it was not; the summary is still worth showing.
        said = f"I couldn't send {name}'s resume file to your Slack messages ({failed})."
        return f"{summary}\n\n{said}" if summary else said
    sent = f"I've sent {name}'s resume to your Slack messages as a Word file."
    return f"{sent}\n\n{summary}" if mode == "view" and summary else sent


def register(server: MCPServer, hrms: Hrms) -> None:
    # Read-only on purpose: it changes nothing in the HRMS, and the file goes only to the
    # asker's own Slack messages — so the platform runs it without a confirmation card.
    @server.tool(annotations=READ_ONLY, structured_output=False, description=GET_RESUME)
    async def get_resume(ctx: Context, employee: str = "", mode: str = "download") -> str:
        mode = "view" if mode.strip().lower() == "view" else "download"
        wanted = employee.strip()
        arguments: dict[str, Any] = {"mode": mode}
        if wanted:
            session = await _ask(hrms, ctx, "get_user_session", {})
            session = session if isinstance(session, dict) else {}
            found = await _ask(hrms, ctx, "resolve_employee", {"query": wanted})
            person, names = pick(found, wanted)
            if person is None:
                if not names:
                    raise ToolError(f"No employee found matching {wanted}.")
                return "That fits several people — ask which one: " + "; ".join(names) + "."
            own = str(person["id"]) == str(session.get("employee_id") or "")
            if not own and session.get("role") != "admin":
                return NOT_YOURS
            arguments["employee_id"] = str(person["id"])
        result = await _ask(hrms, ctx, "get_employee_resume", arguments)
        if not isinstance(result, dict):
            raise ToolError("The HRMS gave no answer about the resume. Try again shortly.")
        return sent_answer(
            str(result.get("employee") or "the"),
            mode,
            str(result.get("summary") or ""),
            str(result.get("delivery_failed") or ""),
        )
