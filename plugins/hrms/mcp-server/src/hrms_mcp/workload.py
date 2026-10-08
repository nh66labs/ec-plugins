"""What a leave would leave undone in Jira, said as a person would say it.

Pure rules over tickets already read — no I/O — so each is tested on its own.
Two warnings, both of which warn and never block (the Confirm that follows is
the person's answer):

1. **Their own open work that the leave touches.** Each open ticket of theirs
   is placed by its deadline — its due date, or else the end of the active
   sprint it is in — against the leave:

   - due **while they are away**;
   - due **just after they are back** (within ``after_days`` working days);
   - due **before they go**, said only when the leave starts within
     ``soon_days`` working days of today — a ticket due next week is not a
     handover for a leave six weeks off;
   - **in progress with no deadline at all**, said only when the leave is as
     soon, since that is work that stops when they do.

   Those are named, with the date and what it means. Every other open ticket
   is only counted: naming work the leave does not touch teaches people to
   skip the warning.
2. **A tight sprint with a teammate already off.** Someone on the same project
   has already applied for those days, and a sprint of that project's ends
   during the leave or soon after it with much of its work still open. Named
   with the teammate's own open tickets in it, since those are what nobody will
   be there to finish. It follows the sentence saying who is off, so it does
   not name them again.

The first is also said to a project manager asking about someone else's leave
(``their_work``): there the tickets the leave touches come first, and then the
rest are named too while there is room, since the manager is matching them
against a piece of work they named ("will the payment integration be
affected?"). It says so when there are none, since "nothing pending" is an
answer.

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

#: A deadline this many working days after the leave still falls on it: the
#: work is done during the days off.
AFTER_DAYS = 3

#: A leave starting within this many working days of today is soon enough that
#: work due before it, or in progress with no deadline, is a handover.
SOON_DAYS = 5

# Where a ticket stands against the leave, most pressing first. Every one but
# ``UNTOUCHED`` is named.
AWAY, AFTER, BEFORE, UNDATED, UNTOUCHED = range(5)


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
    #: False when Jira had more open tickets than were read, so someone's may
    #: be among those that were not.
    complete: bool = True


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


@dataclass(frozen=True)
class _Placed:
    ticket: Ticket
    where: int
    deadline: date | None
    #: What the deadline means for this leave, after the ticket's status.
    note: str

    def __str__(self) -> str:
        detail = f"{self.ticket.status}, {self.note}" if self.note else self.ticket.status
        return f"{self.ticket.key} {self.ticket.summary} ({detail})"


def _sprint_ends(projects: list[ProjectWork]) -> dict[str, Sprint]:
    """Each ticket in an active sprint, to the sprint — the deadline of one that has
    no due date of its own."""
    return {
        t.key: p.sprint for p in projects if p.sprint is not None for t in p.sprint.tickets
    }


def _place(
    ticket: Ticket,
    sprint: Sprint | None,
    leave_from: date,
    leave_to: date,
    today: date | None,
    after_days: int,
    soon_days: int,
    you: bool,
) -> _Placed:
    away, back, go = (
        ("you're away", "you're back", "you go") if you
        else ("they're away", "they're back", "they go")
    )
    soon = today is not None and working_days_after(today, leave_from) <= soon_days
    deadline = ticket.due or (sprint.ends if sprint else None)
    if deadline is None:
        if ticket.category == "indeterminate" and soon:
            return _Placed(ticket, UNDATED, None, "no due date")
        return _Placed(ticket, UNTOUCHED, None, "")
    when = (
        f"due {_day(deadline)}" if ticket.due
        else f"no due date; sprint “{sprint.name}” ends {_day(deadline)}"  # type: ignore[union-attr]
    )
    if leave_from <= deadline <= leave_to:
        return _Placed(ticket, AWAY, deadline, f"{when}, while {away}")
    if deadline > leave_to:
        after = working_days_after(leave_to, deadline)
        if after == 0:
            # The weekend or holiday straight after the leave: it passes before
            # they are back.
            return _Placed(ticket, AWAY, deadline, f"{when}, while {away}")
        if after <= after_days:
            day = f"the day {back}" if after == 1 else f"just after {back}"
            return _Placed(ticket, AFTER, deadline, f"{when}, {day}")
        return _Placed(ticket, UNTOUCHED, deadline, when)
    if not soon:
        return _Placed(ticket, UNTOUCHED, deadline, when)
    if today is not None and deadline < today:
        if ticket.due:
            return _Placed(ticket, BEFORE, deadline, f"overdue since {_day(deadline)}")
        # A sprint left open past its end: the date is missed, not ahead.
        return _Placed(
            ticket, BEFORE, deadline,
            f"no due date; sprint “{sprint.name}” ended {_day(deadline)}",  # type: ignore[union-attr]
        )
    return _Placed(ticket, BEFORE, deadline, f"{when}, before {go}")


def _placed(
    projects: list[ProjectWork],
    tickets: list[Ticket],
    leave_from: date,
    leave_to: date,
    today: date | None,
    after_days: int,
    soon_days: int,
    you: bool,
) -> list[_Placed]:
    """Most pressing first; then soonest deadline, in progress before to do, by key."""
    sprints = _sprint_ends(projects)
    placed = [
        _place(t, sprints.get(t.key), leave_from, leave_to, today, after_days, soon_days, you)
        for t in tickets
    ]
    return sorted(placed, key=lambda p: (
        p.where, p.deadline or date.max, p.ticket.category != "indeterminate", p.ticket.key,
    ))


def _and(items: list[str]) -> str:
    """``[A]`` → "A"; ``[A, B]`` → "A and B"; ``[A, B, C]`` → "A, B and C"."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _named(placed: list[_Placed], limit: int, more: str = "more") -> str:
    shown = [str(p) for p in placed[:limit]]
    rest = len(placed) - len(shown)
    if rest:
        shown.append(f"{rest} {more}")
    return _and(shown)


def _touched(
    projects: list[ProjectWork],
    name: str,
    email: str,
    leave_from: date,
    leave_to: date,
    account: str,
    today: date | None,
    after_days: int,
    soon_days: int,
) -> tuple[list[_Placed], int] | None:
    """The person's own open tickets this leave touches, most pressing first, and
    how many others of theirs it does not; None when they have no open ticket.
    What ``own_work`` and ``own_heads_up`` both name, so they name the same."""
    mine = [t for p in projects for t in p.open_tickets if t.open and _is(t, name, email, account)]
    if not mine:
        return None
    placed = _placed(projects, mine, leave_from, leave_to, today, after_days, soon_days, True)
    named = [p for p in placed if p.where != UNTOUCHED]
    return named, len(placed) - len(named)


def _tickets(count: int) -> str:
    return f"{count} open Jira ticket{'s' if count != 1 else ''}"


def own_work(
    projects: list[ProjectWork],
    name: str,
    email: str,
    leave_from: date,
    leave_to: date,
    account: str = "",
    *,
    today: date | None = None,
    after_days: int = AFTER_DAYS,
    soon_days: int = SOON_DAYS,
) -> tuple[str, str]:
    """The person's own open work, as ``(warning, note)``: the warning names the
    tickets the leave touches; the note, when it touches none, says the rest were
    looked at — information, not a reason to think twice. Either may be empty.
    ``today`` unknown, the leave is taken to be far off."""
    partial = [p.key for p in projects if not p.complete]
    caveat = (
        f"Only some of the open tickets in {', '.join(partial)} could be read, so some "
        "of yours may not be counted." if partial else ""
    )
    touched = _touched(
        projects, name, email, leave_from, leave_to, account, today, after_days, soon_days
    )
    if touched is None:
        return "", caveat
    named, rest = touched
    if not named:
        if rest == 1:
            note = "Your 1 open Jira ticket is not due around your leave."
        else:
            note = f"None of your {_tickets(rest)} are due around your leave."
        return "", f"{note} {caveat}".rstrip()
    warning = (
        f"You have {_tickets(len(named))} to hand over before you go: {_named(named, LISTED)}."
    )
    if rest:
        other = f"{rest} other open ticket{'s' if rest != 1 else ''}"
        warning += f" {other} of yours {'are' if rest != 1 else 'is'} not due around your leave."
    return f"{warning} {caveat}".rstrip(), ""


#: Longest summary the early heads-up shows a ticket by; the check shows it whole.
HEADS_UP_SUMMARY = 40


def own_heads_up(
    projects: list[ProjectWork],
    name: str,
    email: str,
    leave_from: date,
    leave_to: date,
    account: str = "",
    *,
    today: date | None = None,
    after_days: int = AFTER_DAYS,
    soon_days: int = SOON_DAYS,
    limit: int = 220,
) -> str:
    """The person's own tickets this leave touches, said once, briefly, as soon as
    they name the dates — "Heads-up: you have 2 open Jira tickets to hand over
    before you go: ECP-1 Fix login and ECP-2 Ship v2. …". Empty when it touches
    none. Said as a handover, not a due date: some are overdue or have none.

    The same tickets as ``own_work`` names (``_touched``), in the same order, but
    only their key and a short summary: it sits above the choices still to make, and the check
    before Confirm gives each one's status and deadline. Never longer than
    ``limit``: fewer tickets are listed, then summaries dropped, then the tickets
    and the closing words, before it would be cut mid-sentence by whatever shows
    it; empty when even the lead does not fit.
    """
    touched = _touched(
        projects, name, email, leave_from, leave_to, account, today, after_days, soon_days
    )
    named = [p.ticket for p in touched[0]] if touched else []
    if not named:
        return ""
    count = "an open Jira ticket" if len(named) == 1 else f"{len(named)} open Jira tickets"
    # A project read only in part may hold more of theirs: the count is a floor.
    if any(not p.complete for p in projects):
        count = f"at least {_tickets(len(named))}"
    lead = f"Heads-up: you have {count} to hand over before you go"
    close = "You can still apply."

    def short(ticket: Ticket, room: int) -> str:
        summary = " ".join(ticket.summary.split())
        if len(summary) > room:
            summary = summary[: room - 1].rstrip() + "…"
        return f"{ticket.key} {summary}" if room and summary else ticket.key

    for shown in range(min(len(named), LISTED), 0, -1):
        for room in (HEADS_UP_SUMMARY, 0):
            items = [short(t, room) for t in named[:shown]]
            if len(named) > shown:
                items.append(f"{len(named) - shown} more")
            said = f"{lead}: {_and(items)}. {close}"
            if len(said) <= limit:
                return said
    return next((said for said in (f"{lead}. {close}", f"{lead}.") if len(said) <= limit), "")


def their_work(
    projects: list[ProjectWork],
    name: str,
    leave_from: date,
    leave_to: date,
    *,
    email: str = "",
    account: str = "",
    today: date | None = None,
    after_days: int = AFTER_DAYS,
    soon_days: int = SOON_DAYS,
) -> str:
    """Someone else's open tickets, for the project manager asking about their
    leave, said with their HRMS name: those the leave touches first, then the
    rest while there is room.

    "None" is said only when it is sure: when their Jira account is known and
    every open ticket was read. Otherwise it says what may have been missed —
    a manager told "none" takes it as the answer."""
    keys = ", ".join(p.key for p in projects)
    theirs = [
        t for p in projects for t in p.open_tickets if t.open and _is(t, name, email, account)
    ]
    unsure = []
    partial = [p.key for p in projects if not p.complete]
    if partial:
        unsure.append(
            f"only some of the open tickets in {', '.join(partial)} could be read, "
            "so some of theirs may not be listed"
        )
    if not account and not theirs:
        unsure.append(
            "their Jira account could not be confirmed, so tickets assigned to them "
            "under another name in Jira would not be found"
        )
    caveat = f" Not certain: {'; and '.join(unsure)}." if unsure else ""
    if not theirs:
        if unsure:
            return f"No open Jira tickets in {keys} were found for {name}.{caveat}"
        return f"{name} has no open Jira tickets in {keys}."
    placed = _placed(projects, theirs, leave_from, leave_to, today, after_days, soon_days, False)
    named = [p for p in placed if p.where != UNTOUCHED]
    rest = [p for p in placed if p.where == UNTOUCHED]
    room = max(LISTED_FOR_MANAGER - len(named), 0)
    if named:
        if not rest:
            which = f", {'all ' if len(named) != 1 else ''}affected by this leave"
        else:
            verb = "are" if len(named) != 1 else "is"
            which = f"; {len(named)} {verb} affected by this leave"
        said = (
            f"{name} has {_tickets(len(placed))} in {keys}{which}: "
            f"{_named(named, LISTED_FOR_MANAGER)}."
        )
        if rest:
            also = _named(rest, room) if room else f"{len(rest)} more"
            said += f" Also open, not affected by the leave: {also}."
    else:
        none = (
            f"{name}'s 1 open Jira ticket in {keys} is not" if len(placed) == 1
            else f"None of {name}'s {_tickets(len(placed))} in {keys} are"
        )
        said = f"{none} affected by this leave: {_named(rest, LISTED_FOR_MANAGER)}."
    return said + caveat


def tight_sprints(
    projects: list[ProjectWork],
    teammates_off: list[str],
    leave_from: date,
    leave_to: date,
    *,
    tight_days: int,
    tight_share: float,
    email: str = "",
    account: str = "",
) -> list[str]:
    """``email`` and ``account`` are those of the one person off, when only one
    is and they are known; anyone else is found by name."""
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
        if len(teammates_off) == 1:
            theirs = [t for t in still_open if _is(t, teammates_off[0], email, account)]
        else:
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
