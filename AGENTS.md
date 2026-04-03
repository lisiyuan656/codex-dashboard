# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `src/codex_dashboard/`. The FastAPI server entrypoint is in `main.py`, persistence and session ingest logic are in `store.py`, and browser templates/assets live under `templates/` and `static/`. Agent runtime code is under `src/codex_dashboard/agent/`, including tmux session management and the local socket service. CLI helpers, including `launch-tty` and hook installation, live in `src/codex_dashboard/cli.py` and `src/codex_dashboard/codex_hooks.py`. Tests are in `tests/` and generally mirror runtime modules by behavior.

## Build, Test, and Development Commands
- `uv sync --extra dev`: install runtime and test dependencies.
- `uv run codex-dashboard-server`: start the FastAPI dashboard locally.
- `uv run codex-dashboard-agent`: start the local machine agent.
- `uv run codex-dashboard-cli launch-tty --cwd "$PWD" -- resume --last`: launch a managed tmux-backed Codex session from the current shell.
- `uv run codex-dashboard-cli install-cli-hooks`: install the dashboard’s Codex hooks into `~/.codex`.
- `uv run python -m pytest`: run the full test suite.

## Coding Style & Naming Conventions
Use 4-space indentation, ASCII by default, and Python type hints consistent with the existing codebase. Follow current naming patterns: `snake_case` for functions and variables, `PascalCase` for classes, and short, explicit helper names. Keep modules focused on one responsibility. There is no formatter configuration checked in, so match the surrounding style and keep imports/order clean.

## Testing Guidelines
Tests use `pytest`. Add or update tests for every behavior change, especially around session state transitions, tmux recovery, websocket updates, and store ingest logic. Name tests `test_<behavior>()` and keep fixtures lightweight. For targeted runs, use examples like `uv run python -m pytest tests/test_store.py tests/test_agent_service.py`.

## Commit & Pull Request Guidelines
Recent commits use short imperative summaries such as `Fix current-pane tmux session state` and `Track tmux CLI status with Codex hooks`. Keep commit messages concise and behavior-focused. Pull requests should explain the user-visible change, note any server/agent restart requirement, include screenshots for UI changes, and list the exact test command used.

## Security & Configuration Tips
Do not commit real secrets from `~/.codex/config.toml` or dashboard environment variables. Prefer environment variables for `CODEX_DASHBOARD_*` settings during local development. When changing tmux/CLI session behavior, note whether the change affects existing running sessions or only newly launched ones.
