# Codex Dashboard

`codex-dashboard` is a small self-hosted control plane for `codex-cli` instances running on multiple Linux hosts.

It provides:

- a central FastAPI web app with login, machine overview, session detail, and live websocket updates
- a lightweight Python agent that connects back to the server and manages local Codex sessions
- tmux-backed terminal sessions so you can keep using real remote terminals while the dashboard tracks and controls them
- a secondary native Codex app-server session runner over stdio JSON-RPC for browser-only managed sessions
- read-only discovery of unmanaged `codex` processes on each host

## Status

This repository contains an MVP implementation of the plan:

- monitoring is functional
- remote interaction is functional for managed tmux terminal sessions and native app-server sessions launched through the agent
- unmanaged sessions are detected and shown but are not controllable
- terminal sessions are the default managed path; app-server sessions remain available as a secondary mode
- approval handling is structured only for app-server sessions; tmux terminal sessions use raw terminal input

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

5. For a terminal-first workflow on the host that runs the agent, run the wrapper inside your existing tmux pane:

```bash
uv run codex-dashboard-cli launch-tty \
  --cwd /mnt/data/Projects/codex-dashboard \
  --name "Local Terminal Session" \
  -- resume --last
```

If `TMUX_PANE` is set, the wrapper reuses that current pane and runs Codex in the foreground there. If you are not already inside tmux, it falls back to creating a detached tmux session and can attach you to it.

## CLI Launch

You can also launch a managed session without the browser:

```bash
export CODEX_DASHBOARD_URL="http://127.0.0.1:8000"
export CODEX_DASHBOARD_USERNAME="admin"
export CODEX_DASHBOARD_PASSWORD="replace-me"
uv run codex-dashboard-cli launch \
  --agent workstation-omarchy \
  --cwd /mnt/data/Projects/codex-dashboard \
  --name "CLI Managed Session" \
  --prompt "Inspect the repository and summarize the current dashboard architecture." \
  -- resume --last
```

The remote `launch` command defaults to terminal mode. Add `--mode app_server` if you want the older browser-only native app-server flow. If you omit `--prompt`, the session starts idle. If you omit `--password`, the CLI will prompt for it.

## Shell Alias

On a managed host, you can replace your usual `codex` launch with a shell function that still opens a real terminal session while registering it with the dashboard:

```bash
codex() {
  uv run codex-dashboard-cli launch-tty --cwd "$PWD" -- "$@"
}
```

This keeps your workflow terminal-first while the dashboard gains visibility over the session. When run inside tmux, the wrapper now reuses the current pane instead of forcing a second tmux session.

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
- `CODEX_DASHBOARD_AGENT_SOCKET_PATH`
- `CODEX_DASHBOARD_AGENT_SPOOL_DIR`
- `CODEX_DASHBOARD_TMUX_BIN`

## Notes

- Managed sessions default to tmux-backed terminals using `codex --no-alt-screen`.
- App-server sessions are still available, mainly for structured approvals and browser-only control.
- The current live UI favors observability plus text/Enter/Ctrl-C over full browser terminal emulation.
- The server defaults to SQLite for local development but accepts PostgreSQL URLs via `CODEX_DASHBOARD_DATABASE_URL`.
