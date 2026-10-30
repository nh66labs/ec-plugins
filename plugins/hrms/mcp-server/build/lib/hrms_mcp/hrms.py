"""Talking to the HRMS: its MCP endpoint, and its person lookup.

The HRMS decides everything — who the person is, what they may see. This module
only carries a call there and brings the answer back, with the signed identity
Enterprise Claw sent passed through **unchanged**: the HRMS verifies it, so this
server needs no key to check it and could not forge one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from hrms_mcp.config import Settings


class HrmsError(Exception):
    """Why the HRMS gave no answer, in a sentence the assistant can relay."""


@dataclass
class Hrms:
    settings: Settings
    #: Test seam: a fake HRMS answers through an ``httpx.MockTransport``.
    transport: httpx.AsyncBaseTransport | None = None

    def _client(self) -> httpx.AsyncClient:
        # Redirects are refused: following one would hand the API key or the
        # lookup token to whatever host the redirect names.
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings.hrms_timeout_seconds,
            follow_redirects=False,
        )

    async def call(self, tool: str, arguments: dict[str, Any], identity: dict[str, Any]) -> Any:
        """Run one of the HRMS's tools as the person the identity names.

        Returns its result parsed as JSON where it is JSON, else as text.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_identity": identity},
        }
        try:
            async with self._client() as http:
                response = await http.post(
                    self.settings.hrms_mcp_url,
                    json=payload,
                    headers={
                        "X-API-Key": self.settings.hrms_api_key,
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise HrmsError("The HRMS did not answer in time. Try again shortly.") from None
        except httpx.HTTPError:
            raise HrmsError("The HRMS could not be reached.") from None

        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise HrmsError("The HRMS refused this server's API key.")
        if response.is_redirect or response.status_code >= httpx.codes.BAD_REQUEST:
            raise HrmsError(f"The HRMS answered {response.status_code}.")
        try:
            envelope = response.json()
        except ValueError:
            raise HrmsError("The HRMS did not answer with MCP JSON.") from None

        error = envelope.get("error")
        if error:
            if error.get("code") == -32001:
                raise HrmsError(
                    "The HRMS did not accept who is asking. Tell the person this could "
                    "not be checked for them right now."
                )
            raise HrmsError(f"The HRMS refused the request: {error.get('message', 'unknown')}")

        result = envelope.get("result") or {}
        text = "".join(
            part.get("text", "") for part in result.get("content", []) if isinstance(part, dict)
        )
        if result.get("isError"):
            raise HrmsError(text or "The HRMS reported an error.")
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def lookup(self, email: str) -> tuple[int, dict[str, Any] | None]:
        """Find a person by work email: the status, and only the two fields
        Enterprise Claw needs — never the rest of their record."""
        try:
            async with self._client() as http:
                response = await http.get(
                    self.settings.hrms_lookup_url,
                    params={"value": email},
                    headers={"Authorization": f"Bearer {self.settings.hrms_lookup_token}"},
                )
        except httpx.HTTPError:
            return 502, None
        if response.status_code != httpx.codes.OK:
            status = response.status_code if response.status_code == 404 else 502
            return status, None
        try:
            body = response.json()
        except ValueError:
            return 502, None
        user_id = body.get("saas_user_id") if isinstance(body, dict) else None
        if not isinstance(user_id, str) or not user_id:
            return 502, None
        return 200, {"saas_user_id": user_id, "persona_id": body.get("persona_id") or "employee"}
