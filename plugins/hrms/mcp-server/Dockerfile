# The HRMS plugin's MCP server. Built by this repository's CI and run by an
# operator beside Enterprise Claw; never served by the registry.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore .

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "--factory", "hrms_mcp.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
