"""The HRMS plugin's MCP server, assembled.

Run with ``uvicorn --factory hrms_mcp.app:create_app``. Serves:

- ``POST /mcp`` — MCP over HTTP, **stateless**: Enterprise Claw sends each call
  as its own request with no session, and the signed identity of the person
  asking beside every tool call's arguments.
- ``GET /people/lookup?value=<work email>`` — who that is in the HRMS, so
  Enterprise Claw needs one address for this plugin rather than two.
- ``GET /health`` — open, for probes.
"""

from __future__ import annotations

import logging

import httpx
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from hrms_mcp import forms, tools, writes
from hrms_mcp.auth import BearerAuth
from hrms_mcp.config import Settings
from hrms_mcp.hrms import Hrms


def create_app(
    settings: Settings | None = None, hrms_transport: httpx.AsyncBaseTransport | None = None
) -> BearerAuth:
    settings = settings or Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    hrms = Hrms(settings, transport=hrms_transport)

    server = MCPServer("HRMS", instructions=tools.INSTRUCTIONS, version="0.3.0")
    tools.register(server, hrms)
    writes.register(server, hrms)
    forms.register(server, hrms)

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    @server.custom_route("/people/lookup", methods=["GET"])
    async def lookup(request: Request) -> Response:
        email = request.query_params.get("value", "").strip()
        if not email:
            return JSONResponse({"error": "missing value"}, status_code=400)
        status, body = await hrms.lookup(email)
        if body is None:
            return JSONResponse({"error": "not found" if status == 404 else "lookup failed"},
                                status_code=status)
        return JSONResponse(body)

    app = server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")  # noqa: S104 - it serves other containers
    return BearerAuth(app, settings.ec_token)
