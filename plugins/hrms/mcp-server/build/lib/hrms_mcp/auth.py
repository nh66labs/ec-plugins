"""Only Enterprise Claw may use this server.

Plain ASGI, so startup and shutdown reach the MCP session manager untouched.
Tokens are compared in constant time. ``/health`` is open so an orchestrator can
probe it without the token.
"""

from __future__ import annotations

import hmac
from typing import Any

from starlette.responses import JSONResponse

OPEN_PATHS = frozenset({"/health"})


class BearerAuth:
    def __init__(self, app: Any, token: str) -> None:
        if not token:
            raise ValueError("EC_TOKEN is empty: refusing to serve without one")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] not in OPEN_PATHS:
            presented = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(presented, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
