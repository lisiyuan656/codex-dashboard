import json
from pathlib import Path

from codex_dashboard.codex_hooks import install_cli_hooks, normalize_hook_event


def test_install_cli_hooks_preserves_existing_config_and_merges_hooks(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    config_path = codex_home / "config.toml"
    hooks_path = codex_home / "hooks.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                'notify = ["notify-send", "Codex"]',
                "",
                "[features]",
                "collaboration_modes = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "python3 /tmp/existing_hook.py"}],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = install_cli_hooks(
        config_path=config_path,
        hooks_path=hooks_path,
        hook_command="/usr/local/bin/codex-dashboard-cli hook-event",
    )

    config_text = config_path.read_text(encoding="utf-8")
    hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert result["config_changed"] is True
    assert result["hooks_changed"] is True
    assert 'notify = ["notify-send", "Codex"]' in config_text
    assert "collaboration_modes = true" in config_text
    assert "codex_hooks = true" in config_text
    assert hooks_payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "python3 /tmp/existing_hook.py"
    assert any(
        hook["command"] == "/usr/local/bin/codex-dashboard-cli hook-event"
        for group in hooks_payload["hooks"]["SessionStart"]
        for hook in group.get("hooks", [])
    )
    assert "Stop" in hooks_payload["hooks"]


def test_install_cli_hooks_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    hooks_path = tmp_path / "hooks.json"

    first = install_cli_hooks(
        config_path=config_path,
        hooks_path=hooks_path,
        hook_command="/usr/local/bin/codex-dashboard-cli hook-event",
    )
    second = install_cli_hooks(
        config_path=config_path,
        hooks_path=hooks_path,
        hook_command="/usr/local/bin/codex-dashboard-cli hook-event",
    )

    assert first["config_changed"] is True
    assert first["hooks_changed"] is True
    assert second["config_changed"] is False
    assert second["hooks_changed"] is False


def test_normalize_hook_event_maps_tmux_status() -> None:
    payload = normalize_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "cwd": "/repo",
            "tool_input": {"command": "pytest -q"},
        },
        environ={
            "TMUX_PANE": "%7",
            "CODEX_DASHBOARD_SESSION_ID": "managed-session",
        },
        parent_pid=4321,
        tty="pts/7",
        command="codex --no-alt-screen",
    )

    assert payload == {
        "hook_event_name": "PreToolUse",
        "dashboard_session_id": "managed-session",
        "codex_session_id": "codex-session",
        "turn_id": "turn-1",
        "state": "running",
        "status_detail": "Running Bash: pytest -q",
        "pid": 4321,
        "tty": "pts/7",
        "tmux_pane": "%7",
        "cwd": "/repo",
        "command": "codex --no-alt-screen",
        "name": "Tmux Codex 4321",
        "transport": "tmux_terminal",
    }


def test_normalize_hook_event_maps_stop_to_idle() -> None:
    payload = normalize_hook_event(
        {
            "hook_event_name": "Stop",
            "session_id": "codex-session",
            "cwd": "/repo",
        },
        environ={},
        parent_pid=9001,
        tty="pts/9",
        command="codex resume --last",
    )

    assert payload is not None
    assert payload["state"] == "idle"
    assert payload["status_detail"] == "Waiting for input"
    assert payload["transport"] == "cli_terminal"
