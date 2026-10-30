"""What HR's onboarding and orientation tools say, in plain sentences.

The HRMS answers these tools with data (names, steps, statuses). The assistant used
to be handed that data and asked to word it, and sometimes showed it as it was — field
names, quotes and braces. These functions do the wording instead, so HR always reads
a sentence, the same every time: names and dates as people write them, no field names,
no tool names, no ids. Nothing here is a person's pronoun: a person is "they"/"their".
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

#: Said at the end of every tool description that answers with these sentences.
RELAY = (
    "Its answer is already written in plain words: relay it as it is (you may shorten "
    "it, keeping every name, date and fact) — never show data, field names, tool names "
    "or JSON."
)


def _need(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ToolError(f"The HRMS did not say {what}. Check the Onboarding card.")
    return data


def _told(state: str) -> str:
    return {
        "sent": "They have been told.",
        "failed": "I could not deliver it to them, so please let them know yourself.",
        "skipped": "Slack messages are switched off, so they were not told — please let "
        "them know yourself.",
    }.get(state, "They could not be reached by Slack or email, so please let them know yourself.")


def _calendar(r: dict[str, Any]) -> str:
    """Whether the orientation reached Google Calendar, said only when it matters."""
    state = r.get("calendar")
    if state == "added":
        return " It is on your calendar, and a calendar invitation has been sent to them."
    if state == "updated":
        return " The calendar event was updated, and they have been sent the change."
    if state == "failed":
        why = f" ({r['calendar_error']})" if r.get("calendar_error") else ""
        return f" I could not put it on the calendar{why}, so please add it yourself."
    return ""


def orientation_confirmed(data: Any) -> str:
    r = _need(data, "whether the orientation was confirmed")
    who, when, place = r.get("employee"), r.get("when"), r.get("place")
    if not who or not when:
        raise ToolError("The HRMS did not confirm the orientation. Check the Onboarding card.")
    was_when, was_place = r.get("was_when"), r.get("was_place")
    if r.get("rescheduled") and was_when and was_when != when:
        head = f"{who}'s HR orientation has been rescheduled from {was_when} to {when}, at {place}."
    elif r.get("rescheduled"):
        head = (
            f"{who}'s HR orientation stays on {when}, but the place has changed from "
            f"{was_place} to {place}."
        )
    else:
        head = (
            f"{who}'s HR orientation is confirmed for {when}, at {place}. The \"HR "
            "orientation scheduled\" step is done; the onboarding finishes only after "
            "the orientation has been held, when HR marks it completed."
        )
    return f"{head} {_told(str(r.get('employee_told') or ''))}{_calendar(r)}"


def orientation_completed(data: Any) -> str:
    r = _need(data, "whether the orientation was marked completed")
    if r.get("orientation") != "completed" or not r.get("employee"):
        raise ToolError(
            "The HRMS did not mark the orientation completed. Check the Onboarding card."
        )
    head = f"{r['employee']}'s HR orientation is marked as completed."
    if r.get("onboarding_complete"):
        return f"{head} Their onboarding is now complete — every step is done."
    return f"{head} Their onboarding is not complete yet: {r.get('progress')}."


def onboarding_list(data: Any) -> str:
    rows = list(_need(data, "who is being onboarded").get("in_progress") or [])
    if not rows:
        return "No one is being onboarded right now."
    lines = [
        f"{len(rows)} {'person is' if len(rows) == 1 else 'people are'} being onboarded:"
    ]
    for r in rows:
        line = f"• {r.get('employee')} ({r.get('employee_code')}) — {r.get('progress')}"
        if r.get("current_step"):
            line += f"; now on: {r['current_step']}"
        if r.get("needs_hr"):
            line += "; waiting on HR: " + "; ".join(str(n) for n in r["needs_hr"])
        lines.append(line + ".")
    return "\n".join(lines)


_STATUS_WORDS = {
    "completed": "done",
    "in_progress": "in progress",
    "pending": "not started",
    "failed": "failed",
    "not_required": "not required",
}


def onboarding_status(data: Any) -> str:
    s = _need(data, "where the onboarding stands")
    state = "complete" if s.get("status") == "completed" else "in progress"
    head = (
        f"{s.get('employee')} ({s.get('employee_code')}) — onboarding {state}: "
        f"{s.get('progress')}"
    )
    if s.get("started"):
        head += f". Started {s['started']}"
    if s.get("completed"):
        head += f", completed {s['completed']}"
    lines = [head + "."]
    for st in s.get("steps") or []:
        word = _STATUS_WORDS.get(str(st.get("status")), str(st.get("status")))
        line = f"• {st.get('step')} — {word}"
        if st.get("error"):
            line += f": {st['error']}"
        elif st.get("note"):
            line += f": {st['note']}"
        lines.append(line)
    o = s.get("orientation")
    if isinstance(o, dict):
        if o.get("status") == "confirmed":
            lines.append(f"HR orientation: {o.get('confirmed_for')}, at {o.get('place')}.")
        elif o.get("proposed_times"):
            lines.append(
                "HR orientation: they proposed " + "; ".join(o["proposed_times"])
                + " — waiting for HR to confirm one."
            )
    reminders = s.get("reminders_latest") or []
    if reminders:
        lines.append(
            "Latest reminders: "
            + "; ".join(
                f"{r.get('when')} by {r.get('channel')} ({r.get('status')})" for r in reminders
            )
            + "."
        )
    return "\n".join(lines)


_CHANGE_WORDS = {
    "completed": "is done",
    "in_progress": "is being done now",
    "pending": "is back on the list to be done",
    "failed": "has failed",
    "not_required": "is marked not required",
}


def step_change(data: Any) -> str:
    r = _need(data, "what happened to the step")
    word = _CHANGE_WORDS.get(str(r.get("status")), f"is now {r.get('status')}")
    line = f"{r.get('step')} for {r.get('employee')} {word}. Onboarding: {r.get('progress')}."
    if r.get("note"):
        line += f" Note: {r['note']}"
    return line


def onboarding_started(data: Any) -> str:
    r = _need(data, "whether the onboarding started")
    who = r.get("employee")
    if r.get("started"):
        return (
            f"Started {who}'s onboarding on {r.get('started_on')}; the setup email is on its "
            f"way. {r.get('progress')}."
        )
    return (
        f"{who}'s onboarding had already begun on {r.get('started_on')} — nothing was "
        f"changed. {r.get('progress')}."
    )
