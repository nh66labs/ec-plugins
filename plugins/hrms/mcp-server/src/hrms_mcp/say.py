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


def _s(value: Any) -> str:
    """A field as text, empty when the HRMS left it out — never the word "None"."""
    return "" if value is None else str(value).strip()


def _rows(value: Any) -> list[dict[str, Any]]:
    """The dict rows of a list the HRMS sent; anything else in it is left out."""
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _texts(value: Any) -> list[str]:
    """A list of texts; a lone text is one item, not a string to split into letters."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [text for item in items if (text := _s(item))]


def _told(state: str) -> str:
    return {
        "sent": "They have been told.",
        "failed": "I could not deliver it to them, so please let them know yourself.",
        "skipped": "Slack messages are switched off, so they were not told — please let "
        "them know yourself.",
        "not_sent": "They could not be reached by Slack or email, so please let them know "
        "yourself.",
    }.get(state, "I could not tell whether they were told, so please check with them.")


def _calendar(r: dict[str, Any]) -> str:
    """Whether the orientation reached Google Calendar, said only when it matters."""
    state = r.get("calendar")
    if state == "added":
        return " The calendar event has been created."
    if state == "updated":
        return " The calendar event was updated."
    if state == "failed":
        why = f" ({r['calendar_error']})" if r.get("calendar_error") else ""
        return f" I could not put it on the calendar{why}, so please add it yourself."
    return ""


def orientation_confirmed(data: Any) -> str:
    r = _need(data, "whether the orientation was confirmed")
    # The write is already done by now, so only what the sentence is about is required:
    # a missing name or place is left out, not turned into a failure.
    when = _s(r.get("when"))
    if not when:
        raise ToolError("The HRMS did not confirm the orientation. Check the Onboarding card.")
    who, place = _s(r.get("employee")), _s(r.get("place"))
    owner = f"{who}'s HR orientation" if who else "The HR orientation"
    at = f", at {place}" if place else ""
    was_when, was_place = _s(r.get("was_when")), _s(r.get("was_place"))
    time_moved = bool(was_when) and was_when != when
    place_moved = bool(was_place) and bool(place) and was_place != place
    if r.get("rescheduled") and time_moved:
        head = f"{owner} has been rescheduled from {was_when} to {when}{at}."
    elif r.get("rescheduled") and place_moved:
        head = f"{owner} stays on {when}, but the place has changed from {was_place} to {place}."
    elif r.get("rescheduled"):
        head = f"{owner} has been rescheduled; it is now on {when}{at}."
    else:
        head = (
            f"{owner} is confirmed for {when}{at}. The \"HR "
            "orientation scheduled\" step is done; the onboarding finishes only after "
            "the orientation has been held, when HR marks it completed."
        )
    return f"{head} {_told(_s(r.get('employee_told')))}{_calendar(r)}"


def orientation_completed(data: Any) -> str:
    r = _need(data, "whether the orientation was marked completed")
    if r.get("orientation") != "completed":
        raise ToolError(
            "The HRMS did not mark the orientation completed. Check the Onboarding card."
        )
    who = _s(r.get("employee"))
    head = f"{who}'s HR orientation is marked as completed." if who else (
        "The HR orientation is marked as completed."
    )
    if r.get("onboarding_complete"):
        return f"{head} Their onboarding is now complete — every step is done."
    progress = _s(r.get("progress"))
    return f"{head} Their onboarding is not complete yet" + (f": {progress}." if progress else ".")


def _who(r: dict[str, Any]) -> str:
    """"Priya (EC123)", the code only when the HRMS sent one."""
    name, code = _s(r.get("employee")), _s(r.get("employee_code"))
    return f"{name} ({code})" if name and code else name or code or "Someone"


def onboarding_list(data: Any) -> str:
    rows = _rows(_need(data, "who is being onboarded").get("in_progress"))
    if not rows:
        return "No one is being onboarded right now."
    lines = [
        f"{len(rows)} {'person is' if len(rows) == 1 else 'people are'} being onboarded:"
    ]
    for r in rows:
        line = f"• {_who(r)}"
        if progress := _s(r.get("progress")):
            line += f" — {progress}"
        if step := _s(r.get("current_step")):
            line += f"; now on: {step}"
        if needs := _texts(r.get("needs_hr")):
            line += "; waiting on HR: " + "; ".join(needs)
        lines.append(line + ".")
    return "\n".join(lines)


_STATUS_WORDS = {
    "completed": "done",
    "in_progress": "in progress",
    "pending": "not started",
    "failed": "failed",
    "not_required": "not required",
}


def _plain(status: Any) -> str:
    """A status in words people use; one we have no word for is shown as it is."""
    key = _s(status)
    return _STATUS_WORDS.get(key, key.replace("_", " ")) or "unknown"


def onboarding_status(data: Any) -> str:
    s = _need(data, "where the onboarding stands")
    status = _s(s.get("status"))
    state = "complete" if status == "completed" else _plain(status) if status else "in progress"
    head = f"{_who(s)} — onboarding {state}"
    if progress := _s(s.get("progress")):
        head += f": {progress}"
    if started := _s(s.get("started")):
        head += f". Started {started}"
    if completed := _s(s.get("completed")):
        head += f", completed {completed}"
    lines = [head + "."]
    for st in _rows(s.get("steps")):
        line = f"• {_s(st.get('step')) or 'A step'} — {_plain(st.get('status'))}"
        if detail := _s(st.get("error")) or _s(st.get("note")):
            line += f": {detail}"
        lines.append(line)
    o = s.get("orientation")
    if isinstance(o, dict):
        if o.get("status") == "confirmed":
            place = _s(o.get("place"))
            when = _s(o.get("confirmed_for"))
            lines.append(f"HR orientation: {when}" + (f", at {place}." if place else "."))
        elif times := _texts(o.get("proposed_times")):
            lines.append(
                "HR orientation: they proposed " + "; ".join(times)
                + " — waiting for HR to confirm one."
            )
    reminders = [
        f"{_s(r.get('when'))} by {_s(r.get('channel'))} ({_s(r.get('status'))})"
        for r in _rows(s.get("reminders_latest"))
    ]
    if reminders:
        lines.append("Latest reminders: " + "; ".join(reminders) + ".")
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
    status = _s(r.get("status"))
    word = _CHANGE_WORDS.get(status, f"is now {_plain(status)}")
    line = f"{_s(r.get('step')) or 'The step'} for {_s(r.get('employee')) or 'them'} {word}."
    if progress := _s(r.get("progress")):
        line += f" Onboarding: {progress}."
    if note := _s(r.get("note")):
        line += f" Note: {note}"
    return line


def onboarding_started(data: Any) -> str:
    r = _need(data, "whether the onboarding started")
    who = _s(r.get("employee")) or "The"
    on = _s(r.get("started_on"))
    progress = f" {_s(r.get('progress'))}." if _s(r.get("progress")) else ""
    if r.get("started"):
        return (
            f"Started {who}'s onboarding" + (f" on {on}" if on else "")
            + f"; the setup email is on its way.{progress}"
        )
    return (
        f"{who}'s onboarding had already begun" + (f" on {on}" if on else "")
        + f" — nothing was changed.{progress}"
    )
