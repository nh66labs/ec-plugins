"""The MBO tools: helping a person set their quarterly MBOs, as that person.

The HRMS holds MBOs as a quarterly plan per employee — each MBO an objective,
the KPI it is measured by and a weightage, the weightages totalling 100 — and
offers them over MCP for the caller alone (``get_my_mbo_plan``,
``get_my_profile``, ``save_my_mbo_plan``). This module puts an assistant in
front of them:

- **One read gives the coach what the HRMS knows.** ``get_my_mbo_context``
  gathers the person's role, mentor, projects, recent work from their timesheet,
  last quarter's MBOs and whether this quarter's plan is open, as short lines —
  the project's own situation comes from the Space the question is asked in.
- **The save is a write, one field per value.** Enterprise Claw confirms every
  write by showing the person each argument as a line; an objective, a KPI and a
  weightage per MBO read as "Objective 1: …", where a list of records would
  read as a blob. Up to five MBOs.
- **Saving is safe to repeat and never submits.** The HRMS replaces the Draft
  plan's MBOs with the set given, so a repeated save changes nothing; the person
  submits the plan in the HRMS when they are ready.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from hrms_mcp.hrms import Hrms
from hrms_mcp.tools import READ_ONLY, _ask, _identity

#: Replaces the plan's MBOs with the set given — a write, and safe to repeat.
REPLACES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

#: How many MBOs one save carries. The HRMS allows more; a quarter's plan that
#: needs more than five is one to set in the HRMS itself.
MAX_MBOS = 5

#: How far back the timesheet is read for "what they have been working on".
RECENT_WORK_DAYS = 90
#: Distinct timesheet notes shown, most recent first.
RECENT_NOTES = 8

INSTRUCTIONS = """\
Setting MBOs (a person's quarterly objectives):
1. Call get_my_mbo_context first. It gives their role, mentor, projects, what
   they have been working on, last quarter's MBOs, and whether this quarter's
   plan can be changed. If it cannot, say why in its words; you may still help
   them think, but nothing can be saved until HR opens it.
2. Look at the project's situation in this Space before suggesting anything:
   search its memory, and use its Jira and GitHub tools where there are any,
   for milestones, deadlines, open issues and risks. Base suggestions on what
   you find and say where it came from; never invent project facts.
3. If you do not know what they want to grow in, ask that once, briefly.
4. Suggest two or three MBOs at a time. Each has an objective, a KPI that can
   be measured (a number or a date), a suggested weightage, and one line on why
   it matters now — the project evidence, and how it serves their growth.
5. When they turn one down, ask in a few words what did not fit (too big, the
   wrong focus, not in their control), then offer a genuinely different
   direction, not a rewording of the same idea.
6. Push back on objectives that cannot be measured and on targets the person
   cannot influence.
7. Agree a final set of three to five MBOs whose weightages total exactly 100.
   Show the whole set, then call save_my_mbo_plan with the fiscal year and
   quarter from the context and one objective, KPI and weightage per MBO. It
   saves a Draft only; tell them to submit it in the HRMS when they are ready.
   Never save before they have agreed the set.
Only ever discuss the person's own MBOs.
"""


# ---- the period ---------------------------------------------------------------


def previous_quarter(fiscal_year: str, quarter: int) -> tuple[str, int]:
    """``("2026-27", 1)`` → ``("2025-26", 4)``; otherwise the quarter before."""
    if quarter > 1:
        return fiscal_year, quarter - 1
    start = int(fiscal_year.split("-")[0]) - 1
    return f"{start}-{(start + 1) % 100:02d}", 4


def _today(session: Any) -> date:
    """Today in the person's timezone, as the HRMS says it; ours as a fallback."""
    if isinstance(session, dict):
        try:
            return date.fromisoformat(str(session.get("current_date", ""))[:10])
        except ValueError:
            pass
    return date.today()


# ---- turning HRMS answers into lines -------------------------------------------


def plan_lines(plan: Any, heading: str) -> list[str]:
    """A plan as the coach reads it: status, whether it is open, each MBO."""
    if not isinstance(plan, dict):
        return [f"{heading}: not available."]
    period = f"Q{plan.get('quarter')} {plan.get('fiscal_year')}"
    lines = [f"{heading} ({period}): {plan.get('status', 'unknown')}."]
    if plan.get("can_edit"):
        lines.append("It is open: MBOs can be saved to it.")
    elif plan.get("why_not_editable"):
        lines.append(f"It cannot be changed: {plan['why_not_editable']}.")
    mbos = plan.get("mbos") or []
    if not mbos:
        lines.append("No MBOs on it yet.")
    for n, mbo in enumerate(mbos, start=1):
        line = (
            f"{n}. {mbo.get('title', '')} — KPI: {mbo.get('kpi', '')}"
            f" — weight {mbo.get('weightage', 0):g}"
        )
        if mbo.get("completion_percent"):
            line += f" — self rating {mbo['completion_percent']:g}%"
        if mbo.get("manager_rating"):
            line += f" — manager rating {mbo['manager_rating']:g}"
        lines.append(line)
    return lines


def profile_lines(profile: Any) -> list[str]:
    if not isinstance(profile, dict):
        return ["Profile: not available."]
    lines = [f"Name: {profile.get('name', '')}"]
    for label, key in (("Designation", "designation"), ("Mentor", "mentor"),
                       ("Joined", "date_of_joining")):
        if profile.get(key):
            lines.append(f"{label}: {profile[key]}")
    if profile.get("skills"):
        lines.append("Skills: " + ", ".join(str(s) for s in profile["skills"]))
    roles = [
        f"{e.get('role', '')} at {e.get('company', '')}"
        for e in profile.get("previous_experiences") or []
        if e.get("role") or e.get("company")
    ]
    if roles:
        lines.append("Before joining: " + "; ".join(roles))
    return lines


def project_lines(projects: Any) -> list[str]:
    if not isinstance(projects, list) or not projects:
        return ["Projects: none recorded in the HRMS."]
    lines = ["Projects:"]
    for project in projects:
        managers = ", ".join(
            str(m.get("name", "")) for m in project.get("project_managers") or [] if m.get("name")
        )
        line = f"- {project.get('name', '')}"
        if project.get("description"):
            line += f": {str(project['description'])[:200]}"
        if managers:
            line += f" (managed by {managers})"
        lines.append(line)
    return lines


def work_lines(entries: Any, since: date) -> list[str]:
    """Hours by project or activity since ``since``, and the latest notes."""
    if not isinstance(entries, list) or not entries:
        return [f"Timesheet since {since:%d %b %Y}: nothing logged."]
    hours: Counter[str] = Counter()
    for entry in entries:
        where = (
            entry.get("project_name") or entry.get("activity_type_name") or entry.get("activity")
        )
        hours[str(where or "Other")] += float(entry.get("hours") or 0)
    lines = [f"Timesheet since {since:%d %b %Y}:"]
    lines += [f"- {where}: {total:g} hours" for where, total in hours.most_common()]
    notes: list[str] = []
    for entry in sorted(entries, key=lambda e: str(e.get("date", "")), reverse=True):
        note = " ".join(str(entry.get("note") or "").split())
        if note and note not in notes:
            notes.append(note)
        if len(notes) == RECENT_NOTES:
            break
    if notes:
        lines.append("Recent notes: " + " | ".join(n[:160] for n in notes))
    return lines


# ---- the save -------------------------------------------------------------------


def agreed_set(given: list[tuple[str, str, float]]) -> list[dict[str, Any]]:
    """The MBOs to save, checked as a whole before anything reaches the HRMS.

    Each numbered slot is all three values or none; slots are filled from 1
    without gaps; the weightages total exactly 100.
    """
    rows: list[dict[str, Any]] = []
    seen_empty = False
    for n, (objective, kpi, weight) in enumerate(given, start=1):
        objective, kpi = " ".join(objective.split()), " ".join(kpi.split())
        if not objective and not kpi and not weight:
            seen_empty = True
            continue
        if seen_empty:
            raise ToolError(f"MBO {n} follows an empty one: number them from 1 without gaps.")
        if not objective or not kpi:
            raise ToolError(f"MBO {n} needs both an objective and a KPI.")
        if not 0 < weight <= 100:
            raise ToolError(f"MBO {n} needs a weightage above 0 and at most 100.")
        rows.append({"title": objective, "kpi": kpi, "weightage": weight})
    if not rows:
        raise ToolError("Give at least one MBO.")
    total = sum(r["weightage"] for r in rows)
    if abs(total - 100) > 0.01:
        raise ToolError(f"The weightages total {total:g}; they must total exactly 100.")
    return rows


def register(server: MCPServer, hrms: Hrms) -> None:
    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_my_mbo_context(ctx: Context) -> str:
        """Everything the HRMS knows that helps the person set their MBOs.

        Their role and mentor, their projects and who manages them, what they
        have logged in the last three months, last quarter's MBOs, and this
        quarter's plan with whether it can still be changed. Call it first.
        """
        _identity(ctx)  # nothing is asked of the HRMS for no one
        session = await _ask(hrms, ctx, "get_user_session", {})
        profile = await _ask(hrms, ctx, "get_my_profile", {})
        current = await _ask(hrms, ctx, "get_my_mbo_plan", {})
        lines = profile_lines(profile) + [""] + plan_lines(current, "This quarter's plan")
        if isinstance(current, dict) and current.get("fiscal_year"):
            fiscal_year, quarter = previous_quarter(
                str(current["fiscal_year"]), int(current["quarter"])
            )
            previous = await _ask(
                hrms, ctx, "get_my_mbo_plan", {"fiscal_year": fiscal_year, "quarter": quarter}
            )
            lines += [""] + plan_lines(previous, "Last quarter's plan")
        lines += [""] + project_lines(await _ask(hrms, ctx, "get_my_projects", {}))
        since = _today(session) - timedelta(days=RECENT_WORK_DAYS)
        entries = await _ask(
            hrms, ctx, "get_timesheet",
            {"start_date": since.isoformat(), "end_date": _today(session).isoformat()},
        )
        lines += [""] + work_lines(entries, since)
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_my_mbo_plan(ctx: Context, fiscal_year: str = "", quarter: int = 0) -> str:
        """The person's MBO plan for one quarter: its status, whether it can
        still be changed, and each MBO with its KPI, weightage and ratings.

        fiscal_year is like "2026-27" (April to March) and quarter is 1-4 (1 is
        April to June); leave both out for the current quarter.
        """
        if not 0 <= quarter <= 4:
            raise ToolError("quarter must be 1-4, or 0 for the current one")
        arguments: dict[str, Any] = {}
        if fiscal_year.strip():
            arguments["fiscal_year"] = fiscal_year.strip()
        if quarter:
            arguments["quarter"] = quarter
        plan = await _ask(hrms, ctx, "get_my_mbo_plan", arguments)
        return "\n".join(plan_lines(plan, "Plan"))

    @server.tool(annotations=REPLACES, structured_output=False)
    async def save_my_mbo_plan(
        fiscal_year: str,
        quarter: int,
        objective_1: str,
        kpi_1: str,
        weight_1: float,
        ctx: Context,
        objective_2: str = "",
        kpi_2: str = "",
        weight_2: float = 0,
        objective_3: str = "",
        kpi_3: str = "",
        weight_3: float = 0,
        objective_4: str = "",
        kpi_4: str = "",
        weight_4: float = 0,
        objective_5: str = "",
        kpi_5: str = "",
        weight_5: float = 0,
    ) -> str:
        """Saves the person's agreed MBOs as their Draft plan for the quarter.

        Replaces the plan's MBOs with these, in order: one objective, one KPI
        (how it is measured) and one weightage per MBO, numbered from 1, up to
        five. The weightages must total exactly 100. It does not submit the
        plan. Call it only after the person has agreed the whole set.
        """
        if not 1 <= quarter <= 4:
            raise ToolError("quarter must be 1-4")
        rows = agreed_set([
            (objective_1, kpi_1, weight_1),
            (objective_2, kpi_2, weight_2),
            (objective_3, kpi_3, weight_3),
            (objective_4, kpi_4, weight_4),
            (objective_5, kpi_5, weight_5),
        ])
        plan = await _ask(
            hrms, ctx, "save_my_mbo_plan",
            {"fiscal_year": fiscal_year.strip(), "quarter": quarter, "mbos": json.dumps(rows)},
        )
        return "\n".join(
            [f"Saved {len(rows)} MBO{'s' if len(rows) != 1 else ''} as a Draft."]
            + plan_lines(plan, "Plan")
            + ["Submit it in the HRMS when you are ready."]
        )
