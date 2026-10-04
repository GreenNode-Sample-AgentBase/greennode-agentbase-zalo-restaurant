FROM python:3.12-slim

# Restaurant MCP server. Build context is src/mcp-server/ (docker build ... src/mcp-server/).
# Runs OUTSIDE AgentBase: on a vServer (docker compose) or VKS inside the customer VPC.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    MCP_DB_PATH=/app/data/restaurant.db

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./main.py
# Make the code readable by the non-root user regardless of host file modes.
RUN chmod 0644 /app/main.py

# Run as a non-root user. /app/data holds the SQLite database: mount a persistent
# volume here (docker named volume, or a PVC on VKS).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data
USER 10001:10001
VOLUME ["/app/data"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=3).status == 200 else 1)"]
CMD ["python", "main.py"]
