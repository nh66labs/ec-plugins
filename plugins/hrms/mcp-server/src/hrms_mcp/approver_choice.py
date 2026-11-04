"""Choosing who approves a request (EC-D342).

A project is in one of two approver modes. In "fixed" mode a request goes to
every project manager of the project, as it always has. In "choose" mode the
person picks which of the project's managers it goes to — one or more, any one
of whom may decide it. The HRMS owns the rule and enforces it; this module only
asks the person what the HRMS says they may choose, and says who the request
will go to.

Nothing here changes a request from someone with no "choose" project: the
options come back empty or all "fixed", no question is asked, no manager is sent
with the request, and the sentences are the ones this server always said. An
HRMS that does not offer the options at all (an older one) is read the same way.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from hrms_mcp.hrms import Hrms

#: The name of the form question and of the write's argument that carries the picks.
ARGUMENT = "approvers"

CHOOSE = "choose"


async def read_options(hrms: Hrms, ctx: Context) -> list[dict[str, Any]]:
    """The asker's projects with each one's managers and approver mode; none
    when the HRMS cannot say. A request is never held up by this read: without
    it nobody is asked, and the HRMS refuses a request it needed a pick for."""
    from hrms_mcp.tools import _ask  # noqa: PLC0415 - tools imports this module

    try:
        result = await _ask(hrms, ctx, "get_approver_options", {})
    except ToolError:
        return []
    if not isinstance(result, list):
        return []
    return [p for p in result if isinstance(p, dict)]


def _leads(project: dict[str, Any]) -> list[dict[str, Any]]:
    leads = project.get("leads")
    if not isinstance(leads, list):
        return []
    return [m for m in leads if isinstance(m, dict) and m.get("id")]


def choosing(projects: list[dict[str, Any]]) -> bool:
    """Whether any of the asker's projects lets them choose."""
    return any(p.get("approver_mode") == CHOOSE for p in projects)


def pickable(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The projects that need a pick: in "choose" mode with several managers. A
    project with one manager goes to that manager, and asks nothing."""
    return [p for p in projects if p.get("approver_mode") == CHOOSE and len(_leads(p)) > 1]


def question(projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The form question that asks who should approve, or None when nothing is
    to be chosen. One question covers every project that needs a pick; where
    there are several, each manager is named with their project."""
    needing = pickable(projects)
    if not needing:
        return None
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for project in needing:
        for lead in _leads(project):
            key = str(lead["id"])
            if key in seen:
                continue
            seen.add(key)
            name = str(lead.get("name") or "").strip() or "One with no name"
            label = f"{name} · {project.get('project_name')}" if len(needing) > 1 else name
            options.append({"value": key, "label": label})
    return {
        "name": ARGUMENT,
        "label": "Select your team leads",
        "kind": "multichoice",
        "options": options,
    }


def parse(value: str) -> list[str]:
    """The ids a person picked, as the platform sends them: a JSON list of the
    chosen values. A comma-separated list is read too."""
    text = (value or "").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except ValueError:
        loaded = text.split(",")
    if not isinstance(loaded, list):
        loaded = [loaded]
    ids: list[str] = []
    for item in loaded:
        item = str(item).strip()
        if item and item not in ids:
            ids.append(item)
    return ids


def picks_for(projects: list[dict[str, Any]], value: str) -> list[str]:
    """The managers to send the request to, checked against what the asker may
    choose: none for someone with no "choose" project (a pick is not used
    there). Refuses, in words for the person, a pick that is no manager of
    theirs, or a project that needed one and has none."""
    if not choosing(projects):
        return []
    picked = parse(value)
    known = {str(lead["id"]) for project in projects for lead in _leads(project)}
    if any(p not in known for p in picked):
        raise ToolError(
            "One of the approvers chosen is not a project manager of the person's. "
            "Ask them again who should approve it."
        )
    for project in pickable(projects):
        if not any(str(lead["id"]) in picked for lead in _leads(project)):
            raise ToolError(
                "Ask the person who should approve it: they need to choose at least one "
                f"project manager of {project.get('project_name') or 'the project'}."
            )
    return picked


def receivers(projects: list[dict[str, Any]], picked: list[str]) -> list[str]:
    """The names of the managers the request goes to: every manager of a
    "fixed" project, the one manager of a "choose" project that has one, and the
    managers picked from the rest — as the HRMS works it out."""
    names: dict[str, str] = {}
    for project in projects:
        leads = _leads(project)
        take = project.get("approver_mode") != CHOOSE or len(leads) == 1
        for lead in leads:
            if take or str(lead["id"]) in picked:
                name = str(lead.get("name") or "").strip() or "one with no name"
                names.setdefault(str(lead["id"]), name)
    return list(names.values())


def sentence(projects: list[dict[str, Any]], picked: list[str], *, filed: bool) -> str:
    """Who the request goes to when the person chose, said to them — or "" when
    they had nothing to choose, and the usual sentence applies. ``filed`` says it
    of a request just filed rather than one about to be."""
    if not choosing(projects):
        return ""
    names = receivers(projects, picked)
    if not names:
        return ""
    from hrms_mcp import workload  # noqa: PLC0415 - kept beside its only use

    if len(names) == 1:
        if filed:
            return f"It is waiting for {names[0]} to approve."
        return f"{names[0]} will approve it."
    anyone = workload.joined(names)
    if filed:
        return f"It is waiting for {anyone} — it is settled by whichever of them decides first."
    return f"It goes to {anyone}; any one of them can approve it."


def context_sentence(projects: list[dict[str, Any]]) -> str:
    """Who approves, said when the asker is only being told how things stand
    (no request in hand): where they choose, among whom; elsewhere, who."""
    from hrms_mcp import workload  # noqa: PLC0415 - kept beside its only use

    parts = []
    for project in projects:
        names = [str(m.get("name") or "").strip() or "one with no name" for m in _leads(project)]
        if not names:
            continue
        where = project.get("project_name") or "a project"
        if project in pickable(projects):
            parts.append(f"{where}: you choose among {workload.joined(names)}")
        else:
            parts.append(f"{where}: {workload.joined(names)}")
    if not parts:
        return ""
    return (
        "Who approves: " + "; ".join(parts) + ". Where you choose, a request goes to the "
        "managers you pick, and any one of them can approve it."
    )


async def picks_from(hrms: Hrms, ctx: Context, value: str) -> list[str]:
    """``picks_for`` the asker's own projects: the managers a write is sent to,
    none when they had nothing to choose."""
    return picks_for(await read_options(hrms, ctx), value)
