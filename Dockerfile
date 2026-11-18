# MCP DayPlanner – web app + both MCP servers in one container (Azure App Service friendly)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    SESSION_DIR=/tmp/mcp-assistant-sessions

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY mcp_client.py weather_server.py task_server.py ./
# Sample data served as MCP resources (file://delivery_log, file://meeting_notes)
COPY delivery_log.txt meeting_notes.txt ./
COPY web ./web

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# One worker on purpose: sessions and rate limits are kept in memory.
# --proxy-headers so client IPs come from Azure's front end (used for rate limiting).
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
