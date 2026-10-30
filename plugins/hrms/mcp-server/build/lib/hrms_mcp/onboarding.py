"""Onboarding a new employee: HR tells the assistant, and the HRMS gets the record.

The record is the one the HRMS's own Employees > Create employee makes, with the
same checks — the HRMS runs them, and only for an HRMS admin. These tools only
carry HR's words there and bring a sentence back:

- **Names, not ids.** Designation, company and status go by the names HR uses;
  the HRMS finds them. No id is shown, and no argument names a person: the
  HRMS takes the asker from the signed identity.
- **Only what HR knows on the day.** PAN, Aadhaar and bank details are not taken
  here: the employee adds them on their own profile, so they never pass through
  a chat.
- **Safe to repeat.** A confirmed create can be delivered twice. Before acting,
  ``create_employee`` looks for anyone already holding the details, reports the
  person it would create as already created, and refuses a clash with anyone
  else, naming who has what.

How the assistant runs the conversation with HR is said in these tools' own
descriptions, not in the server's instructions: those are cut at the
platform's 4,000 characters and the leave and MBO guidance already fills them.
The model reads a tool's whole description in every turn it is offered.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import READ_ONLY, _ask, day_of
from hrms_mcp.writes import FILES

#: What anyone but an HRMS admin is told. A read answers with it rather than
#: failing: an answer drawn only from failed calls cites nothing, and the
#: platform then shows the person a generic "couldn't find anything" instead.
NOT_HR_ADMIN = (
    "Only an HRMS administrator can onboard a new employee, so nothing was looked "
    "up or created."
)


class NotAdmin(Exception):
    """The HRMS refused the asker as not an admin."""


async def ask_admin(hrms: Hrms, ctx: Context, tool: str, arguments: dict[str, Any]) -> Any:
    """Ask the HRMS something only an HRMS admin may. Raises NotAdmin when it refuses
    them — the one place that recognises the refusal, for every admin tool: a read
    answers with NOT_HR_ADMIN, a change refuses with it."""
    try:
        return await _ask(hrms, ctx, tool, arguments)
    except ToolError as error:
        if "access denied" in str(error).lower():
            raise NotAdmin from None
        raise


def as_json(data: Any) -> str:
    """HRMS data handed to the assistant as it is, for it to word."""
    return json.dumps(data, indent=2, ensure_ascii=False)


#: What a clash is called, by the HRMS's name for the detail.
DETAIL = {
    "company_email": "company email",
    "employee_code": "employee code",
    "personal_email": "personal email",
}


def _names(rows: Any) -> list[str]:
    return [str(r.get("name", "")).strip() for r in rows or [] if str(r.get("name", "")).strip()]


def _who(match: dict[str, Any]) -> str:
    code = str(match.get("employee_code") or "").strip()
    who = str(match.get("name") or "someone").strip()
    who = f"{who} ({code})" if code else who
    return f"{who}, who is deactivated" if match.get("deactivated") else who


def clash_sentences(matches: list[dict[str, Any]]) -> list[str]:
    """``[{matched_on: company_email, name: Ethan Parker, …}]`` →
    ``["The company email is already Ethan Parker's (H101)."]``, one per detail."""
    said = []
    for m in matches:
        detail = DETAIL.get(str(m.get("matched_on")), "detail")
        said.append(f"The {detail} is already used by {_who(m)}.")
    return said


def _same_person(m: dict[str, Any], name: str, company_email: str, code: str) -> bool:
    return (
        not m.get("deactivated")
        and str(m.get("name", "")).strip().lower() == name.strip().lower()
        and str(m.get("company_email", "")).strip().lower() == company_email.strip().lower()
        and str(m.get("employee_code", "")).strip().lower() == code.strip().lower()
    )


#: What became of the setup email, by what the HRMS said of it.
SETUP_EMAIL = {
    "sending": "The setup email is on its way to their personal email.",
    "no_personal_email": "There is no personal email yet, so the setup email waits: add it on "
                         "their profile, then press Retry on Setup email under Onboarding.",
    "switched_off": "The setup email was not sent: sending onboarding email is switched off on "
                    "this server.",
    "not_set_up": "The setup email was not sent: email is not set up on this server.",
}


def created_line(record: dict[str, Any]) -> str:
    """The new record as HR reads it: who, the role, the company, from when."""
    who = f"{record.get('name')} ({record.get('employee_code')})"
    role = record.get("designation") or "no designation"
    company = record.get("company") or "the company"
    joins = day_of(str(record.get("date_of_joining") or ""))
    status = record.get("status") or "Active"
    email = record.get("company_email")
    line = (
        f"{who}: {role} at {company}, joining {joins}, status {status}. "
        f"They sign in to HR-Assist with {email}."
    )
    setup = _setup_email_line(record)
    return f"{line} {setup}" if setup else line


def _setup_email_line(record: dict[str, Any]) -> str:
    """What became of the setup email — or, when the HRMS says nothing HR can rely
    on, that it must be checked: never silence, which reads as "it went"."""
    state = record.get("setup_email")
    if state:
        return SETUP_EMAIL.get(
            str(state),
            "The HRMS gave a state for the setup email that I don't know, so check it on "
            "their Onboarding card.",
        )
    if record.get("onboarding_started") is False:
        return (
            "Their onboarding could not be started, so no setup email was sent: ask me to "
            "start their onboarding."
        )
    return ""


def documents_answer(dashboard_url: str, employee_id: str = "") -> str:
    """Where documents go, as one answer: never in the chat, always on the
    person's own profile in HR-Assist — linked straight to it when the dashboard's
    address and their id are known."""
    base = dashboard_url.strip().rstrip("/")
    if base and employee_id:
        where = f"My Profile → Documents in HR-Assist: {base}/employees/{employee_id}"
    elif base:
        where = f"HR-Assist ({base}), under My Profile → Documents"
    else:
        where = "HR-Assist, under My Profile → Documents"
    return (
        "Documents are not taken in this chat, so nothing sent here reaches your "
        f"profile. Upload them on {where}. PDF, JPG or PNG, one document at a "
        "time, with its type (ID proof, PAN, Aadhaar, bank proof, certificates). "
        "Only you and HR can see them there."
    )


def register(server: MCPServer, hrms: Hrms, dashboard_url: str = "") -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def where_to_upload_documents(ctx: Context) -> str:
        """Where an employee uploads their documents — ID proof, PAN, Aadhaar,
        bank proof, certificates — since none is taken in the chat.

        Call it whenever someone asks how or where to send, share, upload or
        submit a document, or sends a file in the chat, and answer with what it
        says, citing it. Never ask anyone to send a document, a PAN, an Aadhaar
        number or bank details in the chat.
        """
        try:
            session = await _ask(hrms, ctx, "get_user_session", {})
        except ToolError:
            session = {}
        employee_id = str(session.get("employee_id") or "") if isinstance(session, dict) else ""
        return documents_answer(dashboard_url, employee_id)

    async def _existing(ctx: Context, **details: str) -> list[dict[str, Any]]:
        found = await ask_admin(
            hrms, ctx, "find_existing_employee", {k: v for k, v in details.items() if v}
        )
        return list((found or {}).get("matches") or []) if isinstance(found, dict) else []

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_new_employee_options(ctx: Context) -> str:
        """Start here to onboard a new employee: the designations, companies
        and statuses their record can have, and the domain their company email
        must be at. HRMS admins only: anyone else is told so — tell them,
        citing it, and stop.

        When HR asks to onboard, add or create an employee — "Onboard Ethan
        Parker ethanparker@hamon.in":
        1. Take the full name and company email from what HR wrote. Never ask
           again for anything they already said.
        2. Before asking anything, call this and check_new_employee with the
           company email, together.
        3. The email is not at the domain this names: say so, and ask for the
           right address. A detail is already used: say by whom, and go on
           only with other details from HR.
        4. Ask, in one short message, only for what is still missing of: the
           employee code, the designation (offer this tool's as choices), the
           date of joining and a phone number. Only one company: use it, do
           not ask. The status is Active: never ask about it. Ask for their
           personal email too — the setup email goes there; without it, it
           waits until HR adds one. The company email is never one.
        5. Never ask for, or pass on, PAN, Aadhaar, bank details, a date of
           birth or an address, even when HR offers them: the employee adds
           them on their own profile.
        6. A date in words ("next Monday"): call get_leave_context and take
           the date from it.
        7. With the employee code, call check_new_employee again with it, then
           create_employee once with everything. Its card is HR's
           confirmation: never ask "shall I go ahead?" yourself.
        """
        try:
            options = await ask_admin(hrms, ctx, "get_employee_form_options", {})
        except NotAdmin:
            return NOT_HR_ADMIN
        if not isinstance(options, dict):
            raise ToolError("The HRMS did not say what a new employee's record can hold.")
        lines = [
            "Designations: " + (", ".join(_names(options.get("designations"))) or "none set up"),
            "Companies: " + (", ".join(_names(options.get("companies"))) or "none set up"),
            "Statuses: " + (", ".join(_names(options.get("statuses"))) or "none set up")
            + " (Active unless HR says otherwise)",
        ]
        domain = str(options.get("company_email_domain") or "").strip()
        lines.append(
            f"Company email: must be an address at {domain}."
            if domain
            else "Company email: any address (no company domain is set)."
        )
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def check_new_employee(
        company_email: str, ctx: Context, employee_code: str = "", personal_email: str = ""
    ) -> str:
        """Before onboarding someone: whether anyone, active or deactivated,
        already has this company email, employee code or personal email.

        HRMS admins only. Call it as soon as you have the company email, and
        again once you have the employee code. When a detail is used, tell HR
        by whom — a deactivated employee still holds theirs — and never create
        until HR gives another.
        """
        try:
            matches = await _existing(
                ctx,
                company_email=company_email.strip(),
                employee_code=employee_code.strip(),
                personal_email=personal_email.strip(),
            )
        except NotAdmin:
            return NOT_HR_ADMIN
        if not matches:
            return "None of these details is used by anyone yet."
        return " ".join(clash_sentences(matches))

    @server.tool(annotations=FILES, structured_output=False)
    async def create_employee(
        name: str,
        company_email: str,
        employee_code: str,
        designation: str,
        company: str,
        date_of_joining: str,
        phone_number: str,
        ctx: Context,
        status: str = "",
        personal_email: str = "",
    ) -> str:
        """Create a new employee's record in the HRMS, as the dashboard's
        Employees > Create employee does. HR confirms it first.

        HRMS admins only: anyone else is told so, and nothing is created.
        Every argument comes from what HR said, or from
        get_new_employee_options for the designation, company and status —
        never guess one. date_of_joining is YYYY-MM-DD. status is Active when
        left empty. personal_email only if HR gave it — never the company email.
        PAN, Aadhaar and bank details are not taken here: the employee adds
        them on their own profile.

        Once created, give HR its line, and say what comes next: the employee
        signs in to HR-Assist with their company Google account, completes
        their details under My Profile, and uploads their documents there.
        "Already created" means nothing changed: say so, and do not call it
        again. A refusal: say plainly what it said, and do not retry the same
        details.
        """
        name, company_email = name.strip(), company_email.strip().lower()
        employee_code, personal_email = employee_code.strip(), personal_email.strip().lower()
        if personal_email and personal_email == company_email:
            raise ToolError(
                "The personal email is the company email. Ask HR for the employee's "
                "personal address, or leave it out."
            )

        try:
            matches = await _existing(
                ctx,
                company_email=company_email,
                employee_code=employee_code,
                personal_email=personal_email,
            )
        except NotAdmin:
            raise ToolError(NOT_HR_ADMIN) from None
        if matches and all(_same_person(m, name, company_email, employee_code) for m in matches):
            return f"Already created — nothing was changed. {_who(matches[0])} is in the HRMS."
        clashes = [m for m in matches if not _same_person(m, name, company_email, employee_code)]
        if clashes:
            raise ToolError(
                " ".join(clash_sentences(clashes))
                + " Nothing was created. Ask HR which details to use instead."
            )

        arguments = {
            "name": name,
            "company_email": company_email,
            "employee_code": employee_code,
            "designation": designation.strip(),
            "company": company.strip(),
            "date_of_joining": date_of_joining.strip(),
            "phone_number": phone_number.strip(),
        }
        if status.strip():
            arguments["status"] = status.strip()
        if personal_email:
            arguments["personal_email"] = personal_email
        try:
            record = await ask_admin(hrms, ctx, "create_employee", arguments)
        except NotAdmin:
            raise ToolError(NOT_HR_ADMIN) from None
        if not isinstance(record, dict) or not record.get("employee_code"):
            raise ToolError("The HRMS did not confirm the new employee. Check the Employees page.")
        if record.get("created") is False:
            return f"Already created — nothing was changed. {created_line(record)}"
        return f"Created. {created_line(record)}"
