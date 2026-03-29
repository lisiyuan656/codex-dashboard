# Codex Dashboard

`codex-dashboard` is a small self-hosted control plane for `codex-cli` instances running on multiple Linux hosts.

It provides:

- a central FastAPI web app with login, machine overview, session detail, and live websocket updates
- a lightweight Python agent that connects back to the server and manages local Codex sessions
- a PTY-backed managed session runner so the browser can send text, stop sessions, and answer simple approvals
- read-only discovery of unmanaged `codex` processes on each host

## Status

This repository contains an MVP implementation of the plan:

- monitoring is functional
- remote interaction is functional for managed sessions launched through the agent
- unmanaged sessions are detected and shown but are not controllable
- approval handling is heuristic and PTY-based rather than a native Codex app-server integration

## Quick Start

1. Install dependencies:

```bash
uv sync --extra dev
```

2. Start the server:

```bash
export CODEX_DASHBOARD_SECRET_KEY="replace-me"
export CODEX_DASHBOARD_ADMIN_PASSWORD="replace-me"
uv run codex-dashboard-server
```

3. In another shell on a machine you want to monitor:

```bash
export CODEX_DASHBOARD_AGENT_WS_URL="ws://127.0.0.1:8000/ws/agent"
export CODEX_DASHBOARD_AGENT_TOKEN="change-me"
uv run codex-dashboard-agent
```

4. Open `http://127.0.0.1:8000`, log in as `admin`, and launch a managed session from an agent page.

## Configuration

### Server

- `CODEX_DASHBOARD_DATABASE_URL`
- `CODEX_DASHBOARD_SECRET_KEY`
- `CODEX_DASHBOARD_ADMIN_USERNAME`
- `CODEX_DASHBOARD_ADMIN_PASSWORD`
- `CODEX_DASHBOARD_AGENT_SHARED_SECRET`
- `CODEX_DASHBOARD_OFFLINE_AGENT_SECONDS`
- `CODEX_DASHBOARD_STALE_SESSION_SECONDS`

### Agent

- `CODEX_DASHBOARD_AGENT_WS_URL`
- `CODEX_DASHBOARD_AGENT_ID`
- `CODEX_DASHBOARD_AGENT_TOKEN`
- `CODEX_DASHBOARD_AGENT_LABELS`
- `CODEX_DASHBOARD_AGENT_HEARTBEAT_SECONDS`
- `CODEX_DASHBOARD_AGENT_WATCH_SECONDS`

## Notes

- Managed sessions run under a PTY and work best with `codex --no-alt-screen`.
- The current live UI is intentionally simple: it favors control and observability over full terminal emulation.
- The server defaults to SQLite for local development but accepts PostgreSQL URLs via `CODEX_DASHBOARD_DATABASE_URL`.
