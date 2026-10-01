"""What a leave would leave undone in Jira, said as a person would say it.

Pure rules over tickets already read — no I/O — so each is tested on its own.
Two warnings, both of which warn and never block (the Confirm that follows is
the person's answer):

1. **Their own open work.** Tickets assigned to the person asking that are not
   done — worth handing over before they go.
2. **A tight sprint with a teammate already off.** Someone on the same project
   has already applied for those days, and a sprint of that project's ends
   during the leave or soon after it with much of its work still open. Named
   with the teammate's own open tickets in it, since those are what nobody will
   be there to finish. It follows the sentence saying who is off, so it does
   not name them again.

The first is also said to a project manager asking about someone else's leave
(``their_work``): there it lists more, since the manager is matching the tickets
against a piece of work they named ("will the payment integration be affected?"),
and it says so when there are none, since "nothing pending" is an answer.

"Tight" is the operator's to tune: the sprint ends within ``tight_days`` working
days after the leave, and more than ``tight_share`` of its tickets are still to
do or in progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

#: How many tickets a sentence names before it says "and N more".
LISTED = 3

#: How many a project manager is shown: enough to find the one they asked about.
LISTED_FOR_MANAGER = 10


@dataclass(frozen=True)
class Ticket:
    key: str
    summary: str
    status: str
    #: Jira's status category: ``new`` (to do), ``indeterminate`` (in
    #: progress) or ``done``.
    category: str
    assignee_name: str = ""
    assignee_email: str = ""
    #: Jira's own id for the assignee — the one match that does not depend on
    #: a name being spelt alike in two systems or an email Jira chose to show.
    assignee_account: str = ""
    due: date | None = None

    @property
    def open(self) -> bool:
        return self.category != "done"


@dataclass(frozen=True)
class Sprint:
    name: str
    ends: date
    tickets: list[Ticket] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectWork:
    """One Jira project the person works on: its open tickets, and its sprint."""

    key: str
    open_tickets: list[Ticket] = field(default_factory=list)
    sprint: Sprint | None = None


def _day(value: date) -> str:
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b')}"


def working_days_after(start: date, end: date) -> int:
    """Weekdays after ``start`` up to and including ``end``; 0 when ``end`` is not later."""
    days, cursor = 0, start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _is(ticket: Ticket, name: str, email: str, account: str = "") -> bool:
    """Whether a ticket is this person's: by Jira account when both are known —
    and then only by it — else by email, else by name. Names are the last resort:
    "navaneeth" in Jira and "Navaneeth K" in the HRMS are one person."""
    if account and ticket.assignee_account:
        return ticket.assignee_account == account
    if email and ticket.assignee_email and ticket.assignee_email.casefold() == email.casefold():
        return True
    return bool(name) and ticket.assignee_name.casefold() == name.casefold()


def _ordered(tickets: list[Ticket]) -> list[Ticket]:
    """In progress before to do, then soonest due, then by key."""
    return sorted(
        tickets,
        key=lambda t: (t.category != "indeterminate", t.due or date.max, t.key),
    )


def _listed(
    tickets: list[Ticket], leave_from: date, leave_to: date, limit: int = LISTED
) -> str:
    shown = []
    for ticket in _ordered(tickets)[:limit]:
        detail = ticket.status
        if ticket.due and leave_from <= ticket.due <= leave_to:
            detail += f", due {_day(ticket.due)}"
        shown.append(f"{ticket.key} {ticket.summary} ({detail})")
    more = len(tickets) - len(shown)
    if more:
        shown.append(f"{more} more")
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def own_work(
    projects: list[ProjectWork],
    name: str,
    email: str,
    leave_from: date,
    leave_to: date,
    account: str = "",
) -> str:
    mine = [t for p in projects for t in p.open_tickets if t.open and _is(t, name, email, account)]
    if not mine:
        return ""
    count = f"{len(mine)} open Jira ticket{'s' if len(mine) != 1 else ''}"
    return (
        f"You have {count}: {_listed(mine, leave_from, leave_to)} — "
        "worth handing over before you go."
    )


def their_work(
    projects: list[ProjectWork],
    name: str,
    leave_from: date,
    leave_to: date,
    *,
    email: str = "",
    account: str = "",
) -> str:
    """Someone else's open tickets, for the project manager asking about their
    leave, said with their HRMS name."""
    keys = ", ".join(p.key for p in projects)
    theirs = [
        t for p in projects for t in p.open_tickets if t.open and _is(t, name, email, account)
    ]
    if not theirs:
        return f"{name} has no open Jira tickets in {keys}."
    count = f"{len(theirs)} open Jira ticket{'s' if len(theirs) != 1 else ''}"
    listed = _listed(theirs, leave_from, leave_to, LISTED_FOR_MANAGER)
    return f"{name} has {count} in {keys}: {listed}."


def tight_sprints(
    projects: list[ProjectWork],
    teammates_off: list[str],
    leave_from: date,
    leave_to: date,
    *,
    tight_days: int,
    tight_share: float,
) -> list[str]:
    if not teammates_off:
        return []
    said = []
    for project in projects:
        sprint = project.sprint
        if sprint is None or not sprint.tickets or sprint.ends < leave_from:
            continue
        if working_days_after(leave_to, sprint.ends) > tight_days:
            continue
        still_open = [t for t in sprint.tickets if t.open]
        if len(still_open) / len(sprint.tickets) <= tight_share:
            continue
        theirs = [t for t in still_open if any(_is(t, n, "") for n in teammates_off)]
        sentence = (
            f"{project.key}'s sprint “{sprint.name}” ends on {_day(sprint.ends)} with "
            f"{len(still_open)} of {len(sprint.tickets)} tickets still to do or in progress"
        )
        if theirs:
            whose = f"{teammates_off[0]}'s" if len(teammates_off) == 1 else "their"
            sentence += f", including {whose} {_listed(theirs, leave_from, leave_to)}"
        said.append(sentence + ".")
    return said
