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
