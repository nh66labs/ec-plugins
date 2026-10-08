"""Reading a Jira project's open work and its active sprint, for the leave check.

Optional: with no Jira configured, nothing here is called and the check says
what it always said. Read-only, with one service account the operator gives
this server; its token is a secret, never logged or returned. A Jira that is
slow or refuses is skipped — a leave check never fails because Jira did.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from hrms_mcp.config import Settings
from hrms_mcp.workload import ProjectWork, Sprint, Ticket

log = logging.getLogger("hrms_mcp.jira")

_FIELDS = "summary,status,assignee,duedate"
#: Tickets read per project or sprint, across pages; past this the warning is
#: made from what was read.
_LIMIT = 1000
#: Jira Cloud caps a page at 50 on the agile endpoints and 100 on search.
_PAGE = 50
#: How long an answer is reused where a recent one will do: a heads-up asked
#: again, or by a teammate on the same projects. Jira is read with one service
#: account, so an answer is the same for anyone. A check before Confirm, or a
#: manager's, reads afresh: a ticket closed since must not be warned of.
FRESH_SECONDS = 300


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def ticket_of(issue: dict[str, Any]) -> Ticket:
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    return Ticket(
        key=str(issue.get("key") or ""),
        summary=str(fields.get("summary") or "").strip(),
        status=str(status.get("name") or ""),
        category=str((status.get("statusCategory") or {}).get("key") or "new"),
        assignee_name=str(assignee.get("displayName") or ""),
        assignee_email=str(assignee.get("emailAddress") or ""),
        assignee_account=str(assignee.get("accountId") or ""),
        due=_date(fields.get("duedate")),
    )


def project_keys(settings: Settings, hrms_projects: list[str]) -> list[str]:
    """The Jira projects of the HRMS projects named, by the operator's map."""
    mapped = {}
    for pair in settings.jira_projects.split(";"):
        name, _, key = pair.partition("=")
        if name.strip() and key.strip():
            mapped[name.strip().casefold()] = key.strip()
    keys = [mapped[p.casefold()] for p in hrms_projects if p.casefold() in mapped]
    return list(dict.fromkeys(keys))


@dataclass
class Jira:
    settings: Settings
    #: Test seam: a fake Jira answers through an ``httpx.MockTransport``.
    transport: httpx.AsyncBaseTransport | None = None
    #: Answers given, by what was asked, with when (``FRESH_SECONDS``).
    _said: dict[tuple[str, ...], tuple[float, Any]] = field(
        default_factory=dict, init=False, repr=False
    )

    def _recall(self, asked: tuple[str, ...]) -> Any:
        said = self._said.get(asked)
        return said[1] if said and time.monotonic() - said[0] < FRESH_SECONDS else None

    def _keep(self, asked: tuple[str, ...], answer: Any) -> None:
        now = time.monotonic()
        self._said = {k: v for k, v in self._said.items() if now - v[0] < FRESH_SECONDS}
        self._said[asked] = (now, answer)

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.jira_url and s.jira_email and s.jira_api_token and s.jira_projects)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.jira_url.rstrip("/"),
            auth=(self.settings.jira_email, self.settings.jira_api_token),
            transport=self.transport,
            timeout=self.settings.jira_timeout_seconds,
            # Refused, as for the HRMS: a redirect would carry the credential away.
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def _get(self, http: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
        response = await http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def _sprint_issues(self, http: httpx.AsyncClient, sprint_id: Any) -> list[Any]:
        """Every issue in a sprint, page by page — the endpoint gives 50 at most."""
        issues: list[Any] = []
        while len(issues) < _LIMIT:
            page = await self._get(
                http,
                f"/rest/agile/1.0/sprint/{sprint_id}/issue",
                {"fields": _FIELDS, "startAt": len(issues), "maxResults": _PAGE},
            )
            got = page.get("issues") or []
            issues.extend(got)
            if not got or len(issues) >= int(page.get("total") or 0):
                break
        return issues

    async def _open_issues(self, http: httpx.AsyncClient, key: str) -> tuple[list[Any], bool]:
        """Every open issue in a project, page by page, so the asker's own are
        found however many the project has — and whether that was all of them,
        or reading stopped at ``_LIMIT``."""
        issues: list[Any] = []
        params: dict[str, Any] = {
            "jql": f'project = "{key}" AND statusCategory != Done',
            "fields": _FIELDS,
            "maxResults": 100,
        }
        while True:
            page = await self._get(http, "/rest/api/3/search/jql", params)
            issues.extend(page.get("issues") or [])
            token = page.get("nextPageToken")
            # A page that does not say it is the last is not taken as one while
            # it gives a next page; with none to give, only a "no" is a "no".
            if page.get("isLast") is True or not token:
                return issues, page.get("isLast") is not False
            if len(issues) >= _LIMIT:
                return issues, False
            params = {**params, "nextPageToken": token}

    async def _sprint(self, http: httpx.AsyncClient, key: str) -> Sprint | None:
        boards = await self._get(http, "/rest/agile/1.0/board", {"projectKeyOrId": key})
        for board in boards.get("values") or []:
            sprints = await self._get(
                http, f"/rest/agile/1.0/board/{board['id']}/sprint", {"state": "active"}
            )
            for sprint in sprints.get("values") or []:
                ends = _date(sprint.get("endDate"))
                if ends is None:
                    continue
                issues = await self._sprint_issues(http, sprint["id"])
                return Sprint(
                    name=str(sprint.get("name") or "the sprint"),
                    ends=ends,
                    tickets=[ticket_of(i) for i in issues],
                )
        return None

    async def _project(self, http: httpx.AsyncClient, key: str) -> ProjectWork:
        issues, complete = await self._open_issues(http, key)
        return ProjectWork(
            key=key,
            open_tickets=[ticket_of(i) for i in issues],
            sprint=await self._sprint(http, key),
            complete=complete,
        )

    async def account_of(self, email: str, *, reuse: bool = False) -> str:
        """The Jira account a work email belongs to, or "" when Jira does not say.

        Jira finds a person by their email even when it hides that email on
        their tickets, which is why this is asked rather than read off a ticket.
        Only the one person whose email Jira shows as exactly this counts. Jira's
        search matches prefixes, so a match showing another email — or hiding
        its email, which may be nav@acme.com.au for nav@acme.com — may be
        someone else; they are then found by name instead. With ``reuse``, an
        account found in the last ``FRESH_SECONDS`` is given again; one not
        found is always asked again."""
        if not email:
            return ""
        if reuse and (known := self._recall(("account", email.casefold()))):
            return known
        try:
            async with self._client() as http:
                found = await asyncio.wait_for(
                    self._get(http, "/rest/api/3/user/search", {"query": email}),
                    timeout=self.settings.jira_timeout_seconds,
                )
        except (TimeoutError, httpx.HTTPError, ValueError):
            log.info("jira account lookup skipped: no answer")
            return ""
        people = [u for u in found or [] if isinstance(u, dict) and u.get("accountId")]
        exact = [
            u for u in people if str(u.get("emailAddress") or "").casefold() == email.casefold()
        ]
        account = str(exact[0]["accountId"]) if len(exact) == 1 else ""
        if account:
            self._keep(("account", email.casefold()), account)
        return account

    async def work(self, keys: list[str], *, reuse: bool = False) -> list[ProjectWork]:
        """Each project's open work and active sprint; a project Jira would not
        give is left out, and a Jira that does not answer gives nothing. With
        ``reuse``, an answer from the last ``FRESH_SECONDS`` is given again."""
        if not keys:
            return []
        if reuse and (known := self._recall(("work", *keys))) is not None:
            return known
        try:
            async with self._client() as http:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(self._project(http, key) for key in keys), return_exceptions=True
                    ),
                    timeout=self.settings.jira_timeout_seconds,
                )
        except (TimeoutError, httpx.HTTPError):
            log.info("jira skipped: no answer")
            return []
        work = []
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                # The project only — never the credential or the response.
                log.info("jira project %s skipped", key)
                continue
            work.append(result)
        # Only a whole answer is kept: a project left out is asked again next
        # time, not missing from every answer for FRESH_SECONDS.
        if len(work) == len(keys):
            self._keep(("work", *keys), work)
        return work
