"""A person's open Jira work, for the leave check, as the HRMS reads it.

Jira is set up once, in the HRMS: an admin adds the company's Jira connections
on its dashboard and links each project to its Jira project. This server never
talks to Jira — it asks the HRMS (``get_jira_work``), which reads the person's
open tickets and each project's active sprint: in every project of theirs when
they ask, and only in the projects a project manager manages when that manager
asks about their leave (HR sees all). What it answers is turned here into what
the rules in ``workload`` read.

An HRMS that does not answer is skipped — a leave check never fails because Jira
did. A project the HRMS could not read, or that is not linked to Jira, is said
as such: its tickets are unknown, never none.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mcp.server.mcpserver import Context

from hrms_mcp.config import Settings
from hrms_mcp.hrms import Hrms, HrmsError
from hrms_mcp.workload import ProjectWork, Sprint, Ticket

log = logging.getLogger("hrms_mcp.jira")

#: How long an answer is reused where a recent one will do: a heads-up asked
#: again. A check before Confirm, or a manager's, reads afresh: a ticket closed
#: since must not be warned of.
FRESH_SECONDS = 300


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def ticket_of(issue: dict[str, Any], *, account: str = "") -> Ticket:
    """A ticket as the HRMS gives it; ``account`` files it under that Jira
    account whatever the issue says — for the person's own open tickets, which
    the HRMS read as theirs."""
    return Ticket(
        key=str(issue.get("key") or ""),
        summary=str(issue.get("summary") or "").strip(),
        status=str(issue.get("status") or ""),
        category=str(issue.get("status_category") or "new"),
        assignee_name=str(issue.get("assignee_name") or ""),
        assignee_email=str(issue.get("assignee_email") or ""),
        assignee_account=account or str(issue.get("assignee_account") or ""),
        due=_date(issue.get("due_date")),
    )


@dataclass(frozen=True)
class JiraWork:
    """One person's open work in the projects the HRMS read for the asker."""

    projects: list[ProjectWork]
    #: Who the person is in Jira: their open tickets are filed under it.
    account: str = ""
    #: Projects in the asker's view that are not linked to Jira.
    not_linked: list[str] = field(default_factory=list)
    #: Linked projects whose Jira did not answer, as ``(name, why)``.
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    #: Whose view it was read for: "own", "managed" (the asking manager's
    #: projects) or "all" (every project of theirs, for HR).
    view: str = "own"
    #: The Jira keys of the ``unreadable`` projects, where the HRMS gave them.
    unreadable_keys: list[str] = field(default_factory=list)


def work_of(answer: Any) -> JiraWork | None:
    """The HRMS's ``get_jira_work`` answer, read; None when it names no project
    at all — Jira is not in use for the person — or is not that shape."""
    if not isinstance(answer, dict):
        return None
    raw = [p for p in answer.get("projects") or [] if isinstance(p, dict)]
    not_linked = [str(n) for n in answer.get("not_linked") or [] if str(n).strip()]
    unreadable = [
        (str(u.get("name") or u.get("key") or ""), str(u.get("reason") or "Jira didn't answer"))
        for u in answer.get("unreadable") or []
        if isinstance(u, dict)
    ]
    unreadable_keys = [
        str(u.get("key")) for u in answer.get("unreadable") or []
        if isinstance(u, dict) and u.get("key")
    ]
    if not raw and not not_linked and not unreadable:
        return None
    account = str(answer.get("account_id") or "") or next(
        (str(p.get("account_id")) for p in raw if p.get("account_id")), ""
    )
    projects = []
    for p in raw:
        sprint = p.get("sprint") if isinstance(p.get("sprint"), dict) else None
        ends = _date(sprint.get("end_date")) if sprint else None
        projects.append(ProjectWork(
            key=str(p.get("key") or p.get("name") or ""),
            open_tickets=[
                ticket_of(i, account=account) for i in p.get("open_tickets") or []
                if isinstance(i, dict)
            ],
            sprint=Sprint(
                name=str(sprint.get("name") or "the sprint"),
                ends=ends,
                tickets=[ticket_of(i) for i in sprint.get("issues") or [] if isinstance(i, dict)],
            ) if sprint and ends else None,
            complete=p.get("complete") is not False,
        ))
    return JiraWork(
        projects, account, not_linked, unreadable, str(answer.get("view") or "own"),
        unreadable_keys,
    )


@dataclass
class JiraReader:
    """Asks the HRMS for a person's Jira work, as the person asking."""

    hrms: Hrms
    #: The asker's own work, by who they are, with when (``FRESH_SECONDS``).
    _said: dict[str, tuple[float, JiraWork]] = field(default_factory=dict, init=False, repr=False)

    @property
    def settings(self) -> Settings:
        """The operator's tuning of the warnings: how soon, how tight."""
        return self.hrms.settings

    async def read(
        self, ctx: Context, *, leave_id: str = "", reuse: bool = False
    ) -> JiraWork | None:
        """The asker's own open work, or with ``leave_id`` that leave's
        applicant's in the projects the asker manages; None when the HRMS does
        not say within ``jira_wait_seconds``. With ``reuse``, the asker's own
        work from the last ``FRESH_SECONDS`` is given again — only theirs is
        kept, and only for someone the identity names."""
        identity = (ctx.request_context.params or {}).get("_identity")
        if not isinstance(identity, dict):
            return None
        who = str(identity.get("user_id") or "")
        keep = bool(who) and not leave_id
        now = time.monotonic()
        if reuse and keep and (said := self._said.get(who)) and now - said[0] < FRESH_SECONDS:
            return said[1]
        try:
            answer = await asyncio.wait_for(
                self.hrms.call(
                    "get_jira_work", {"leave_id": leave_id} if leave_id else {}, identity
                ),
                timeout=self.settings.jira_wait_seconds,
            )
        except (HrmsError, TimeoutError):
            # Only that it was skipped — never the identity or the answer.
            log.info("jira work skipped: the HRMS did not say in time")
            return None
        except Exception:
            # An answer not of the shape asked for: still never the leave check's end.
            log.warning("jira work skipped: the HRMS's answer could not be read")
            return None
        work = work_of(answer)
        if work is not None and keep:
            self._said = {k: v for k, v in self._said.items() if now - v[0] < FRESH_SECONDS}
            self._said[who] = (now, work)
        return work
