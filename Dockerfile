FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY quake_agent ./quake_agent
COPY static ./static
COPY web ./web

RUN groupadd --system quakeagent \
    && useradd --system --gid quakeagent --home-dir /app quakeagent \
    && chown -R quakeagent:quakeagent /app

USER quakeagent

EXPOSE 8080

CMD ["python", "-m", "web.app"]
