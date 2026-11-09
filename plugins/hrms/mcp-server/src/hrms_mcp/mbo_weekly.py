"""The Weekly Plan under each MBO: the task planned for each week, and which are done.

The HRMS keeps, for every MBO of a quarter's plan, a Baseline (where it starts), a
Target (what it must reach) and up to twelve planned weeks (W1-W12), each a task
that is done, not done or not yet marked. The person sees them on their MBO Sheet;
this module lets an assistant read and keep them as that person:

- **Reading** is ``get_my_mbo_plan`` (see ``mbo.plan_lines``): each MBO's weeks come
  back as lines with their dates, so "what is the W1 task?" is answered from what
  the HRMS holds, not from a table the assistant once made.
- **``save_my_weekly_plan`` sets one MBO's weeks, one field per week.** Enterprise
  Claw confirms every write by showing each argument as a line, so a week's task
  reads as "Week 6: …" where a list of records would read as a blob. One MBO per
  call keeps that card short; a plan of three MBOs is three cards.
- **``mark_my_week`` marks one week**, only when the person says they did or did
  not finish it.

Both writes are safe to repeat and never submit the plan. The HRMS decides what
may change (only a Draft plan) and counts the weeks; this module checks what the
HRMS would refuse, and names the MBO by its number *and* its title, so a repeated
call can never land on a different MBO.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import _ask

#: A quarter's Weekly Plan covers W1..W12.
WEEKS = 12
#: The longest a week's task, a baseline or a target may be (the HRMS's limit).
MAX_TEXT = 1000

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Replaces one MBO's weeks with the set given — a write, and safe to repeat.
SETS_WEEKS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

#: Marks one planned week — a write that can be taken back, and safe to repeat.
MARKS_A_WEEK = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

#: What a person may say for a mark, and the value the HRMS takes.
_STATUS_IN = {
    "done": "done",
    "not_done": "not_done",
    "not done": "not_done",
    "unmarked": "unmarked",
}
_STATUS_SAID = {"done": "Done", "not_done": "Not done", "unmarked": "not marked"}


# ---- the weeks as lines --------------------------------------------------------


def _day(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]}"


def week_dates(first_monday: date, week: int) -> str:
    """A week's working days as people write them: "9–13 Nov", or "30 Nov–4 Dec"
    across a month end. ``week`` counts from 1."""
    first = first_monday + timedelta(days=7 * (week - 1))
    last = first + timedelta(days=4)
    if first.month == last.month:
        return f"{first.day}–{last.day} {_MONTHS[first.month - 1]}"
    return f"{_day(first)}–{_day(last)}"


def weeks_lines(mbo: dict[str, Any]) -> list[str]:
    """One MBO's Baseline, Target and planned weeks, indented under its line.
    Nothing for an MBO with none of them — and for an HRMS that does not send
    them yet."""
    lines: list[str] = []
    parts = []
    if baseline := str(mbo.get("baseline") or "").strip():
        parts.append(f"Baseline: {baseline}")
    if target := str(mbo.get("target") or "").strip():
        parts.append(f"Target: {target}")
    if parts:
        lines.append("   " + " · ".join(parts))
    planned = mbo.get("weekly_plan")
    for week in planned if isinstance(planned, list) else []:
        if not isinstance(week, dict):
            continue
        dates = f" ({week['dates']})" if week.get("dates") else ""
        status = week.get("status") or "unmarked"
        lines.append(f"   W{week.get('week')}{dates} [{status}]: {week.get('task', '')}")
    return lines


def current_week_line(plan: dict[str, Any]) -> str | None:
    """Which week today falls in, from the plan's own answer: ``current_week`` is
    left out before W1 and after W12, and then nothing is said."""
    week = plan.get("current_week")
    if not isinstance(week, int) or isinstance(week, bool) or not 1 <= week <= WEEKS:
        return None
    dates = ""
    if starts := str(plan.get("weekly_plan_starts") or ""):
        try:
            dates = f" ({week_dates(date.fromisoformat(starts[:10]), week)})"
        except ValueError:
            pass
    return f"Today falls in week W{week}{dates}."


def _mbo_at(plan: Any, number: int, title: str) -> dict[str, Any]:
    """The MBO the caller means: the ``number``-th of the plan (from 1), which must
    carry the title the caller expects. The title is what makes a repeated call
    safe: a number alone names whatever sits there now."""
    rows = (plan.get("mbos") or []) if isinstance(plan, dict) else []
    if not rows:
        raise ToolError("The plan has no MBOs yet. Save the MBOs first.")
    if number > len(rows):
        raise ToolError(
            f"The plan has {len(rows)} MBO(s); there is no MBO {number}. Read the plan "
            "with get_my_mbo_plan."
        )
    row = rows[number - 1]
    if str(row.get("title", "")).strip().casefold() != title.strip().casefold():
        raise ToolError(
            f"MBO {number} on the plan is {row.get('title', '')!r}, not {title.strip()!r}. "
            "Read the plan again with get_my_mbo_plan."
        )
    return row


def _check_place(fiscal_year: str, quarter: int, number: int, objective: str) -> dict[str, Any]:
    if not 1 <= quarter <= 4:
        raise ToolError("quarter must be 1-4")
    if number < 1:
        raise ToolError("mbo_number counts from 1")
    if not objective.strip():
        raise ToolError("Say which MBO: its objective (title) is needed.")
    return {"fiscal_year": fiscal_year.strip(), "quarter": quarter}


# ---- the tools -------------------------------------------------------------------


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=SETS_WEEKS, structured_output=False)
    async def save_my_weekly_plan(
        fiscal_year: str,
        quarter: int,
        mbo_number: int,
        objective: str,
        ctx: Context,
        baseline: str = "",
        target: str = "",
        week_1: str = "",
        week_2: str = "",
        week_3: str = "",
        week_4: str = "",
        week_5: str = "",
        week_6: str = "",
        week_7: str = "",
        week_8: str = "",
        week_9: str = "",
        week_10: str = "",
        week_11: str = "",
        week_12: str = "",
    ) -> str:
        """Saves the weekly plan of ONE MBO on the person's Draft plan: the task for
        each week.

        Use it when the person asks to split an MBO into weekly tasks, and only
        after their MBOs are saved and they have agreed the weekly tasks. Propose
        first, and say the tasks are your suggestion: one or two concrete sentences
        per week, starting with the current week (get_my_mbo_plan says which, and
        gives every week's dates) and ending the week before the MBO's KPI date;
        never a past week.

        mbo_number is the MBO's place in the plan, counting from 1, and objective
        is its title, which must match. Fill one field per week: week_6 is W6;
        leave a week empty for no task. The weeks given become the MBO's whole
        weekly plan: a week left out is cleared, so give every week to keep. A task
        already saved as written keeps its Done mark; a changed task starts
        unmarked. baseline (where the MBO starts) and target (what it must reach)
        are optional; left empty they stay as they are.

        Call it once per MBO. It does not submit the plan. Tell the person that
        once an MBO has planned weeks, its completion is worked out from them (the
        weeks done out of the weeks planned) and replaces a self rating typed
        before.
        """
        period = _check_place(fiscal_year, quarter, mbo_number, objective)
        weeks = [
            {"week": number, "task": task.strip()}
            for number, task in enumerate(
                [week_1, week_2, week_3, week_4, week_5, week_6,
                 week_7, week_8, week_9, week_10, week_11, week_12],
                start=1,
            )
            if task.strip()
        ]
        if not weeks:
            raise ToolError("Give a task for at least one week: week_6 is W6.")
        for week in weeks:
            if len(week["task"]) > MAX_TEXT:
                raise ToolError(
                    f"The task for week {week['week']} is too long (at most {MAX_TEXT} characters)."
                )
        for name, value in (("baseline", baseline), ("target", target)):
            if len(value.strip()) > MAX_TEXT:
                raise ToolError(f"The {name} is too long (at most {MAX_TEXT} characters).")

        # Read first, so an MBO that is not there, or is another one now, is said
        # so before anything is written — and so the person can be told when
        # their typed self rating gives way to the weeks. The HRMS checks the same
        # again.
        row = _mbo_at(await _ask(hrms, ctx, "get_my_mbo_plan", period), mbo_number, objective)
        before = float(row.get("completion_percent") or 0)

        entry: dict[str, Any] = {
            "mbo_number": mbo_number, "title": objective.strip(), "weeks": weeks,
        }
        if baseline.strip():
            entry["baseline"] = baseline.strip()
        if target.strip():
            entry["target"] = target.strip()
        saved = await _ask(
            hrms, ctx, "save_my_weekly_plan", {**period, "weekly_plan": json.dumps([entry])}
        )

        lines = [f"Saved the weekly plan of MBO {mbo_number}: {objective.strip()}."]
        after_rows = (saved.get("mbos") or []) if isinstance(saved, dict) else []
        if len(after_rows) >= mbo_number:
            after = after_rows[mbo_number - 1]
            lines += weeks_lines(after)
            now = float(after.get("completion_percent") or 0)
            if before and now != before:
                lines.append(
                    f"Its completion is now worked out from the weeks ({now:g}%), replacing "
                    f"the {before:g}% self rating."
                )
        lines.append("The plan is still a Draft.")
        return "\n".join(lines)

    @server.tool(annotations=MARKS_A_WEEK, structured_output=False)
    async def mark_my_week(
        fiscal_year: str,
        quarter: int,
        mbo_number: int,
        objective: str,
        week: int,
        status: str,
        ctx: Context,
    ) -> str:
        """Marks one week of one MBO's weekly plan as done, not done, or unmarked
        again.

        Call it only when the person says they did, or did not, finish that
        week's task; never mark a week on their behalf, and never to answer a
        question about progress. Read the plan first. mbo_number is the MBO's
        place in the plan, counting from 1, and objective is its title, which must
        match. week is 1 to 12 and must already have a task. status is "done",
        "not_done", or "unmarked" to take a mark back. Only a Draft plan can be
        changed.
        """
        period = _check_place(fiscal_year, quarter, mbo_number, objective)
        if not 1 <= week <= WEEKS:
            raise ToolError(f"week must be between 1 and {WEEKS}")
        wanted = _STATUS_IN.get(" ".join(status.lower().split()))
        if wanted is None:
            raise ToolError('status must be "done", "not_done" or "unmarked"')

        row = _mbo_at(await _ask(hrms, ctx, "get_my_mbo_plan", period), mbo_number, objective)
        planned = {
            w.get("week"): w for w in (row.get("weekly_plan") or []) if isinstance(w, dict)
        }
        if week not in planned:
            raise ToolError(
                f"Nothing is planned for week W{week} of {row.get('title', '')!r}. "
                "Save the weekly plan first."
            )
        if (planned[week].get("status") or "unmarked") == wanted:
            return (
                f"W{week} of MBO {mbo_number} is already {_STATUS_SAID[wanted]}. "
                "Nothing changed."
            )

        saved = await _ask(
            hrms, ctx, "mark_my_week",
            {**period, "mbo_number": mbo_number, "title": objective.strip(),
             "week": week, "status": wanted},
        )
        title = row.get("title", "")
        lines = [f"Marked W{week} of MBO {mbo_number} ({title}) as {_STATUS_SAID[wanted]}."]
        after_rows = (saved.get("mbos") or []) if isinstance(saved, dict) else []
        if len(after_rows) >= mbo_number:
            now = float(after_rows[mbo_number - 1].get("completion_percent") or 0)
            lines.append(f"This MBO's completion is now {now:g}%.")
        return "\n".join(lines)
