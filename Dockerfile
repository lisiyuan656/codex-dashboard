FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e . \
    && useradd --create-home --shell /usr/sbin/nologin codex \
    && mkdir -p /var/lib/codex-dashboard \
    && chown -R codex:codex /app /var/lib/codex-dashboard

USER codex

ENV CODEX_DASHBOARD_DATABASE_URL=sqlite:////var/lib/codex-dashboard/codex_dashboard.db

EXPOSE 8000

CMD ["codex-dashboard-server"]
