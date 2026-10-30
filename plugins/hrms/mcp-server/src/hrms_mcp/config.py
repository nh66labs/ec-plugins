"""Everything this server must know before it can reach the HRMS.

All of it comes from the environment and nothing else reads it. The two tokens
and the API key are secrets: they are never logged and never returned.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    #: The HRMS's own MCP endpoint, and the key it expects in ``X-API-Key``.
    hrms_mcp_url: str
    hrms_api_key: str
    #: Where the HRMS finds a person by work email, and its bearer.
    hrms_lookup_url: str
    hrms_lookup_token: str
    #: What Enterprise Claw presents to this server, as a bearer, on every
    #: request but ``/health``.
    ec_token: str
    #: Kept well under Enterprise Claw's 45-second limit on a tool call, so a
    #: slow HRMS is reported as slow rather than as a call that vanished.
    hrms_timeout_seconds: float = 20.0

    #: Jira is not set up here: the HRMS reads it, through the Jira connections
    #: an admin adds on its dashboard. What follows only tunes the leave
    #: warnings. A sprint is tight when it ends within this many working days after the
    #: leave, with more than this share of its tickets still open.
    sprint_tight_days: int = 3
    sprint_tight_open_share: float = 0.3
    #: An open ticket is named in the leave warning when its deadline is within
    #: this many working days after the leave; and, when the leave starts within
    #: ``jira_leave_soon_days`` working days, when it is due before the leave or
    #: in progress with no deadline. Any other open ticket is only counted.
    jira_after_leave_days: int = 3
    jira_leave_soon_days: int = 5
    #: How long a leave check waits for the HRMS's reading of Jira before it
    #: goes on without it: with the HRMS's own calls, well inside Enterprise
    #: Claw's 45-second limit on a tool call.
    jira_wait_seconds: float = 10.0

    #: The titles of the company's two MBO guides, as the documents are named in
    #: the drive the Space's connector reads. The coach searches by title, so a
    #: renamed guide is found again once its new title is set here. Blank means
    #: the default title (``mbo.DEFAULT_ACCEPTANCE_GUIDE``, ``DEFAULT_ROLE_GUIDE``).
    mbo_acceptance_guide: str = ""
    mbo_role_guide: str = ""

    #: The HR-Assist dashboard's address as people open it in a browser (not the
    #: API's), e.g. ``https://hr.example.com``. Where the assistant sends people
    #: to upload their documents, which are never taken in a chat. Blank means
    #: the answer names the page without a link.
    hr_assist_dashboard_url: str = ""
