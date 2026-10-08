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
#: Days left in a plan's period at which the person is told it is nearly over.
NEARLY_OVER_DAYS = 14

#: How the coach works. Kept short on purpose: the platform keeps the first
#: 4,000 characters of a server's instructions (its ``INSTRUCTIONS_LIMIT``), and
#: these follow the leave instructions — a longer version was cut mid-sentence,
#: and the rules after the cut never reached the model. How suggestions are laid
#: out travels with the context instead (``presentation_lines``).
INSTRUCTIONS = """\
Setting MBOs (the person's own quarterly objectives):
1. Call get_my_mbo_context first, and lay suggestions out as its result says.
   Work on the quarter they name, else the open one; if none is open, say why
   in its words.
2. Search this Space only for their project's milestones, deadlines and risks
   (memory, Jira, GitHub). Use only facts about that project; never invent one.
3. Suggest exactly three MBOs straight away, weightages totalling exactly 100.
4. When one is turned down, ask briefly what did not fit, then offer a
   genuinely different direction. Push back on KPIs that cannot be measured or
   that they cannot influence.
5. Never save before they have agreed a set of three to five totalling exactly
   100; then call save_my_mbo_plan. It saves a Draft they submit in the HRMS.
6. Only ever discuss the person's own MBOs, never a colleague's.
"""


# ---- the period ---------------------------------------------------------------


#: The quarters' windows as the HRMS names them (the 15th is the cutoff):
#: month and day of the start and end, and whether the end is the next year.
QUARTER_WINDOWS = {
    1: ((4, 1), (6, 15), False),
    2: ((6, 16), (9, 15), False),
    3: ((9, 16), (12, 15), False),
    4: ((12, 16), (3, 15), True),
}


def period_dates(plan: dict[str, Any]) -> tuple[date, date] | None:
    """The plan's first and last day: its own dates, else the HRMS's window for
    that quarter; None when neither can be read."""
    start, end = str(plan.get("period_start") or ""), str(plan.get("period_end") or "")
    if start and end:
        try:
            return date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        except ValueError:
            pass
    try:
        year = int(str(plan.get("fiscal_year", "")).split("-")[0])
        (sm, sd), (em, ed), next_year = QUARTER_WINDOWS[int(plan.get("quarter", 0))]
    except (ValueError, KeyError):
        return None
    start_year = year + (1 if sm < 4 else 0)
    end_year = year + (1 if next_year or em < 4 else 0)
    return date(start_year, sm, sd), date(end_year, em, ed)


def period_of(plan: dict[str, Any]) -> str:
    """The plan's period as a person reads it — "16 Jun 2026 to 15 Sep 2026"."""
    dates = period_dates(plan)
    return f"{dates[0]:%d %b %Y} to {dates[1]:%d %b %Y}" if dates else ""


def next_quarter(fiscal_year: str, quarter: int) -> tuple[str, int]:
    """``("2026-27", 4)`` → ``("2027-28", 1)``; otherwise the quarter after."""
    if quarter < 4:
        return fiscal_year, quarter + 1
    start = int(fiscal_year.split("-")[0]) + 1
    return f"{start}-{(start + 1) % 100:02d}", 1


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
    dates = period_of(plan)
    if dates:
        period += f", {dates}"
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


def open_plan(*plans: Any) -> dict[str, Any] | None:
    """The first plan MBOs can be saved to — this quarter's, else next's."""
    return next((p for p in plans if isinstance(p, dict) and p.get("can_edit")), None)


def presentation_lines(plan: dict[str, Any] | None, today: date) -> list[str]:
    """How the suggestions are laid out for the person, with the open plan's
    dates in the KPI line, a warning when its period is over or nearly so, and
    which plan a save goes to — or, with no plan open, that none can be made.

    Here rather than in ``INSTRUCTIONS``: they did not fit in what the platform
    keeps of those, and here they are read only on the turns that set MBOs, right
    after the facts they shape. Written for a phone screen in Slack, where a
    table arrives as raw pipes and a list of sources pushes the MBOs off screen.
    """
    dates = period_dates(plan) if plan else None
    kpi = "a number or a date" + (
        f" between {dates[0]:%d %b %Y} and {dates[1]:%d %b %Y}" if dates else ""
    )
    late: list[str] = []
    if dates and dates[1] < today:
        late = [f"- Its period ended on {dates[1]:%d %b %Y}: say so before suggesting."]
    elif dates and (dates[1] - today).days <= NEARLY_OVER_DAYS:
        left = (dates[1] - today).days
        when = (
            "today" if left == 0
            else f"on {dates[1]:%d %b %Y}, {left} day{'s' if left != 1 else ''} from now"
        )
        late = [
            f"- Its period ends {when}: say so before suggesting, and keep targets to what fits."
        ]
    if plan:
        close = [
            "- Once they agree a set, show the whole set, then call save_my_mbo_plan with the"
            " fiscal year and quarter of the plan worked on (the open one: fiscal_year"
            f" \"{plan.get('fiscal_year')}\", quarter {plan.get('quarter')}).",
            "- End by asking whether to refine them (and what they want to grow in) or save"
            " them as a Draft. Do not save yet.",
        ]
    else:
        close = [
            "- No plan is open, so nothing can be saved until HR opens one. End by asking"
            " whether to refine them (and what they want to grow in); do not offer to save.",
        ]
    return [
        "How to present suggestions (short enough to read on a phone):",
        *late,
        "- Open with one or two sentences: their role, mentor, project and this quarter's"
        " plan status, citing this result once.",
        "- If timesheet notes, project milestones or project sources are missing, say so in"
        " one short sentence. Mention no other Slack activity, and do not describe how"
        " anything was looked up.",
        "- If what is known is thin, call the suggestions preliminary and use only the facts"
        " above.",
        "- Then three numbered MBOs, each on its own lines:",
        "  *Objective* — one actionable goal",
        f"  *KPI* — {kpi}",
        "  *Weightage* — n%",
        "  *Why it matters* — one sentence tied to their role and this quarter",
        "  The three weightages total exactly 100. No tables; say nothing twice.",
        "- Cite a project fact once, where it is used; no list of sources.",
        *close,
    ]


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
        have logged in the last three months, last quarter's MBOs, and this and
        next quarter's plans with their dates and whether each can still be
        changed — then how to lay suggestions out. Call it first.
        """
        _identity(ctx)  # nothing is asked of the HRMS for no one
        session = await _ask(hrms, ctx, "get_user_session", {})
        profile = await _ask(hrms, ctx, "get_my_profile", {})
        current = await _ask(hrms, ctx, "get_my_mbo_plan", {})
        lines = profile_lines(profile) + [""] + plan_lines(current, "This quarter's plan")
        upcoming: Any = None
        if isinstance(current, dict) and current.get("fiscal_year"):
            fiscal_year, quarter = previous_quarter(
                str(current["fiscal_year"]), int(current["quarter"])
            )
            previous = await _ask(
                hrms, ctx, "get_my_mbo_plan", {"fiscal_year": fiscal_year, "quarter": quarter}
            )
            lines += [""] + plan_lines(previous, "Last quarter's plan")
            fiscal_year, quarter = next_quarter(
                str(current["fiscal_year"]), int(current["quarter"])
            )
            upcoming = await _ask(
                hrms, ctx, "get_my_mbo_plan", {"fiscal_year": fiscal_year, "quarter": quarter}
            )
            lines += [""] + plan_lines(upcoming, "Next quarter's plan")
        lines += [""] + project_lines(await _ask(hrms, ctx, "get_my_projects", {}))
        since = _today(session) - timedelta(days=RECENT_WORK_DAYS)
        entries = await _ask(
            hrms, ctx, "get_timesheet",
            {"start_date": since.isoformat(), "end_date": _today(session).isoformat()},
        )
        lines += [""] + work_lines(entries, since)
        lines += [""] + presentation_lines(open_plan(current, upcoming), _today(session))
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
