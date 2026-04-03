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

## Development Quick Start

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
codex-dashboard-cli launch \
  --agent workstation-omarchy \
  --cwd /mnt/data/Projects/codex-dashboard \
  --name "CLI Managed Session" \
  --prompt "Inspect the repository and summarize the current dashboard architecture." \
  -- resume --last
```

The remote `launch` command defaults to terminal mode. Add `--mode app_server` if you want the older browser-only native app-server flow. If you omit `--prompt`, the session starts idle. If you omit `--password`, the CLI will prompt for it. If you are only using the repo-local dev environment, prefix the command with `uv run`.

## Shell Alias

On a managed host, you can replace your usual `codex` launch with a shell function that still opens a real terminal session while registering it with the dashboard:

```bash
codex() {
  codex-dashboard-cli launch-tty --cwd "$PWD" -- "$@"
}
```

This keeps your workflow terminal-first while the dashboard gains visibility over the session. When run inside tmux, the wrapper now reuses the current pane instead of forcing a second tmux session.

## Recommended Deployment on a Machine

For a real workstation or remote box, do not keep using `uv run`. Install the package once and run the agent as a `systemd --user` service.

1. Install the commands persistently:

```bash
cd /path/to/codex-dashboard
uv tool install --editable .
uv tool update-shell
```

This makes `codex-dashboard-agent` and `codex-dashboard-cli` available directly on `PATH`.

2. Install the user service:

```bash
mkdir -p ~/.config/systemd/user ~/.config/codex-dashboard-agent
cp systemd/codex-dashboard-agent.service ~/.config/systemd/user/
cp systemd/codex-dashboard-agent.env.example ~/.config/codex-dashboard-agent/agent.env
$EDITOR ~/.config/codex-dashboard-agent/agent.env
systemctl --user daemon-reload
systemctl --user enable --now codex-dashboard-agent.service
```

Optional, if you want the user service to keep running after logout:

```bash
loginctl enable-linger "$USER"
```

3. Enable Codex hooks for CLI/tmux status reporting:

```bash
codex-dashboard-cli install-cli-hooks
```

4. Wrap your normal Codex command:

```bash
codex() {
  codex-dashboard-cli launch-tty --cwd "$PWD" -- "$@"
}
```

After that, new tmux sessions launched through the wrapper are tracked without `uv run`, and the agent survives shell exits.

## Docker Deployment for the Server

The dashboard server can be run under Docker Compose. This is only for the web/server process; `codex-dashboard-agent` still runs on the monitored Linux machines outside the container.

1. Create the server env file:

```bash
cp docker/server.env.example docker/server.env
mkdir -p docker/data
$EDITOR docker/server.env
```

2. Build and start the container:

```bash
docker compose --env-file docker/server.env up -d --build
```

3. Check status:

```bash
docker compose ps
docker compose logs -f codex-dashboard-server
```

4. Stop or upgrade:

```bash
docker compose down
docker compose --env-file docker/server.env up -d --build
```

The compose stack stores the default SQLite database in the host directory from `CODEX_DASHBOARD_DATA_DIR`, which defaults to `./docker/data`. That makes backups straightforward, for example:

```bash
tar -czf codex-dashboard-backup.tgz docker/data
```

If you want PostgreSQL instead, set `CODEX_DASHBOARD_DATABASE_URL` in `docker/server.env` to a PostgreSQL connection string. The bind mount can stay in place or be removed if you no longer need local SQLite storage.

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
- `CODEX_DASHBOARD_AGENT_DISPLAY_NAME`
- `CODEX_DASHBOARD_AGENT_TOKEN`
- `CODEX_DASHBOARD_AGENT_LABELS`
- `CODEX_DASHBOARD_CODEX_BIN`
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
