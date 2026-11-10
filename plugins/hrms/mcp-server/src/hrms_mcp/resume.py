"""Getting an employee's resume through the assistant, in professional wording.

HR can ask for anyone's resume; everyone else gets their own. The HRMS builds the
resume and sends the file to the *asker's own* Slack DM — it is never sent to the
person it is about, and the file never passes through this chat. This tool only
works out whose resume is wanted and hands the request to the HRMS, which checks
again who may have it.

The resume is built from Jira ticket titles, timesheet notes and profile text, which
read raw. The HRMS does no AI of its own: for the asker's OWN resume, get_resume holds
the file and hands the assistant the points still in raw wording; the assistant's model
(Enterprise Claw's) rewrites them, and save_resume_wording stores the result and sends
the one file. save_resume_wording runs without a confirmation card (EC-D359, the
manifest's confirm_free), which is why it only ever touches the asker's own resume —
HR asking for someone else's gets it as it is. From then on every copy of that resume —
the HR-Connect download, the review page, this chat — has the wording.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

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

Getting your own resume includes putting it in professional wording. When the answer
says the file is held and ends with "Points still in raw wording", the file has NOT been
sent yet: in this same turn, without asking, rewrite every listed point by the
save_resume_wording rules and call save_resume_wording with them and the same mode — it
saves the wording and sends the one resume, already reworded. That is part of what the
person asked for, not something extra. Then reply as save_resume_wording's answer says
(with its summary for "view"). Never show those points or their refs in chat.
"""

NOT_YOURS = "You can only get your own resume. HR can get anyone's."

#: How the assistant words resume points — the same rules wherever it is asked to.
WORDING_RULES = """\
- Use ONLY facts in the original point: never add technologies, metrics, numbers,
  percentages, team sizes, outcomes or responsibilities it does not state.
- Project highlight: one sentence starting with a strong past-tense action verb
  (Developed, Implemented, Designed, Resolved, Automated, Integrated…), at most 30
  words, no ticket keys (ABC-123), no "I", no trailing period. Shape it as what was
  built + for whom or why, when the original states or plainly implies the purpose
  (e.g. "so managers see only their own project's pending tasks"); never invent one.
- Vary the opening verb: no two highlights of the same project start with the same
  verb, and do not repeat a phrase such as "end to end" across them.
- A point that is only testing or verification: word it as the quality work it was,
  naming what was validated ("Validated the WFH cancellation flow through end-to-end
  testing"), never a bare "Tested the implementation".
- Write for a reader outside the company: spell out internal acronyms and project
  codenames when the original makes their meaning clear (e.g. "the HR management
  system" for HRMS), otherwise keep them; keep product names such as Jira or Slack.
- Project description: 1-2 sentences, present tense, saying what the project is, who
  it is for and what it lets them do, as far as the original says — a product
  summary, not a task ("Internal HR system for employee and leave management").
- No filler: never use empty intensifiers such as thorough, comprehensive, robust,
  seamless, complete, extensive or various; say what was done, not how impressive it
  was ("verified it through testing", not "through thorough testing").
- Bio: a 1-4 sentence professional summary, no "I" and no name. Lead with the role,
  company and start date, then the projects and skills the original names. Leave out
  logged hours and counts of projects or tickets ("Junior Engineer at Hamon since
  September 2025", not "...logging 24 hours of project work").
- Achievement: one clear sentence.
- Fix spelling and grammar; keep any web link exactly as given; plain text only —
  no markdown, bullets or emojis. A point too vague to improve is lightly cleaned up,
  never dropped or merged with another."""

SAVE_RESUME_WORDING = f"""Saves professional wording for points of the asker's OWN resume — the
raw points get_resume handed you — and sends the resume, in that wording, to their Slack
messages (get_resume holds the file back until this runs). It runs at once, with no
confirmation, and never on anyone else's resume. Every copy of the resume (HR-Connect
download included) uses the wording from then on; only the wording changes, never the
sections or layout.

wording is one line per point: "[ref] new wording", the ref exactly as get_resume gave
it, one line for every point it gave. Word each point by these rules:
{WORDING_RULES}

mode is the mode you passed to get_resume ("view" also answers with a summary to show).
Call it straight after rewriting — never ask the person first, and never show the points
or refs in chat."""


def wording_request(result: dict[str, Any]) -> str:
    """The points get_resume's answer lists as still in raw wording — data only:
    what to do with them is in GET_RESUME, where the platform trusts it (a tool's
    answer is read as quoted material, never as instructions)."""
    items = [i for i in result.get("to_refine") or [] if isinstance(i, dict) and i.get("ref")]
    if not items:
        return ""
    total = int(result.get("to_refine_total") or len(items))
    lines = []
    for item in items:
        where = str(item.get("section") or "point")
        if item.get("project"):
            where += f" — {item['project']}"
        lines.append(f"[{item['ref']}] ({where}) {item.get('text', '')}")
    more = f" (the first {len(items)} of {total})" if total > len(items) else ""
    return f"\n\nPoints still in raw wording{more}:\n" + "\n".join(lines) + "\n"


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


async def _target(hrms: Hrms, ctx: Context, wanted: str) -> tuple[dict[str, Any], str]:
    """The HRMS arguments naming whose resume, or ({}, why not) — an answer to
    give instead. Empty ``wanted`` is the asker's own."""
    if not wanted:
        return {}, ""
    session = await _ask(hrms, ctx, "get_user_session", {})
    session = session if isinstance(session, dict) else {}
    found = await _ask(hrms, ctx, "resolve_employee", {"query": wanted})
    person, names = pick(found, wanted)
    if person is None:
        if not names:
            raise ToolError(f"No employee found matching {wanted}.")
        return {}, "That fits several people — ask which one: " + "; ".join(names) + "."
    own = str(person["id"]) == str(session.get("employee_id") or "")
    if not own and session.get("role") != "admin":
        return {}, NOT_YOURS
    return {"employee_id": str(person["id"])}, ""


#: Saves wording on the resume, replacing any saved before — a write, safe to repeat.
REWORDS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def saved_answer(result: dict[str, Any]) -> str:
    """What save_resume_wording says: what was saved, where the file went, what was not,
    and the summary for "view"."""
    saved = int(result.get("saved") or 0)
    rejected = [str(r) for r in result.get("rejected") or []]
    whose = "your"
    parts = []
    if saved:
        points = f"{saved} point{'s' if saved != 1 else ''}"
        parts.append(f"Put {points} of {whose} resume in professional wording.")
    if result.get("delivery_failed"):
        failed = result["delivery_failed"]
        parts.append(f"I couldn't send the resume file to your Slack messages ({failed}).")
    elif result.get("sent_to"):
        parts.append(
            f"I've sent {whose} resume to your Slack messages as a Word file"
            + (", and HR-Connect downloads use the new wording too." if saved else ".")
        )
    else:
        # Never let a held file pass as delivered.
        parts.append("The resume file was not sent to your Slack messages.")
    if not saved:
        parts.append("No new wording was saved.")
    if rejected:
        parts.append("Kept the original for: " + "; ".join(rejected) + ".")
    said = " ".join(parts)
    summary = str(result.get("summary") or "").strip()
    return f"{summary}\n\n{said}" if summary else said


async def _save(hrms: Hrms, ctx: Context, wording: str, mode: str) -> str:
    """Saves wording on the asker's own resume (the HRMS binds it to them) and sends
    the file; empty wording only sends it."""
    mode = "view" if mode.strip().lower() == "view" else "download"
    result = await _ask(hrms, ctx, "save_resume_wording", {"wording": wording, "mode": mode})
    if not isinstance(result, dict):
        raise ToolError("The HRMS gave no answer about the wording. Try again shortly.")
    return saved_answer(result)


def register(server: MCPServer, hrms: Hrms) -> None:
    # Read-only on purpose: it changes nothing in the HRMS, and the file goes only to the
    # asker's own Slack messages — so the platform runs it without a confirmation card.
    @server.tool(annotations=READ_ONLY, structured_output=False, description=GET_RESUME)
    async def get_resume(ctx: Context, employee: str = "", mode: str = "download") -> str:
        mode = "view" if mode.strip().lower() == "view" else "download"
        target, refused = await _target(hrms, ctx, employee.strip())
        if refused:
            return refused
        result = await _ask(hrms, ctx, "get_employee_resume", {"mode": mode, **target})
        if not isinstance(result, dict):
            raise ToolError("The HRMS gave no answer about the resume. Try again shortly.")
        name = str(result.get("employee") or "the")
        if result.get("file_after_wording"):
            if result.get("own") is not True:
                # Wording is saved on the asker's own resume only, so a held file for
                # anyone else must never be handed over for rewording.
                return (
                    f"The HRMS held {name}'s resume file instead of sending it, so "
                    "nothing went to your Slack messages. Try again shortly."
                )
            points = wording_request(result)
            if not points:
                # Held, yet nothing listed to reword: release the file rather
                # than leave the person without it.
                return await _save(hrms, ctx, "", mode)
            held = f"{name}'s resume file is held until its wording is saved (not sent yet)."
            return held + points
        return sent_answer(
            name,
            mode,
            str(result.get("summary") or ""),
            str(result.get("delivery_failed") or ""),
        )

    @server.tool(annotations=REWORDS, structured_output=False, description=SAVE_RESUME_WORDING)
    async def save_resume_wording(ctx: Context, wording: str, mode: str = "download") -> str:
        return await _save(hrms, ctx, wording, mode)
