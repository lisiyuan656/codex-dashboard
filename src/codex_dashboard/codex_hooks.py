from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Mapping


HOOK_EVENT_STATES = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "PreToolUse": "running",
    "PostToolUse": "running",
    "Stop": "idle",
}

HOOK_MATCHERS = {
    "SessionStart": "startup|resume",
    "PreToolUse": "Bash",
    "PostToolUse": "Bash",
}


def default_codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", Path.home() / ".codex"))


def default_config_path() -> Path:
    return default_codex_home() / "config.toml"


def default_hooks_path() -> Path:
    return default_codex_home() / "hooks.json"


def resolve_hook_command() -> str:
    cli_path = shutil.which("codex-dashboard-cli")
    if cli_path:
        return shlex.join([cli_path, "hook-event"])
    return shlex.join([sys.executable, "-m", "codex_dashboard.cli", "hook-event"])


def _looks_like_dashboard_hook(command: str) -> bool:
    return "hook-event" in command and ("codex-dashboard-cli" in command or "codex_dashboard.cli" in command)


def ensure_codex_hooks_enabled(config_path: Path) -> bool:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
        return True

    text = config_path.read_text(encoding="utf-8")
    updated = text

    dotted_pattern = re.compile(r"(?m)^(?P<prefix>\s*features\.codex_hooks\s*=\s*)(?P<value>.+?)\s*$")
    match = dotted_pattern.search(updated)
    if match:
        if match.group("value").strip() != "true":
            updated = dotted_pattern.sub(r"\g<prefix>true", updated, count=1)
    else:
        lines = updated.splitlines(keepends=True)
        features_start = next((index for index, line in enumerate(lines) if re.match(r"^\s*\[features\]\s*$", line)), None)
        if features_start is not None:
            section_end = len(lines)
            for index in range(features_start + 1, len(lines)):
                if re.match(r"^\s*\[", lines[index]):
                    section_end = index
                    break
            feature_line = next(
                (index for index in range(features_start + 1, section_end) if re.match(r"^\s*codex_hooks\s*=", lines[index])),
                None,
            )
            if feature_line is not None:
                if lines[feature_line].strip() != "codex_hooks = true":
                    lines[feature_line] = "codex_hooks = true\n"
            else:
                lines.insert(section_end, "codex_hooks = true\n")
            updated = "".join(lines)
        else:
            suffix = "" if updated.endswith("\n") else "\n"
            updated = f"{updated}{suffix}\n[features]\ncodex_hooks = true\n" if updated else "[features]\ncodex_hooks = true\n"

    if updated == text:
        return False
    config_path.write_text(updated, encoding="utf-8")
    return True


def install_dashboard_hooks(hooks_path: Path, *, hook_command: str | None = None) -> bool:
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hook_command = hook_command or resolve_hook_command()
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("hooks.json must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json field 'hooks' must be a JSON object")

    changed = False
    for event_name in HOOK_EVENT_STATES:
        groups = hooks.setdefault(event_name, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.json field hooks.{event_name} must be a JSON array")
        existing_group: dict[str, Any] | None = None
        existing_handler: dict[str, Any] | None = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not isinstance(handler, dict):
                    continue
                if _looks_like_dashboard_hook(str(handler.get("command", ""))):
                    existing_group = group
                    existing_handler = handler
                    break
            if existing_group is not None:
                break

        matcher = HOOK_MATCHERS.get(event_name)
        if existing_group is None or existing_handler is None:
            new_group: dict[str, Any] = {"hooks": [{"type": "command", "command": hook_command}]}
            if matcher:
                new_group["matcher"] = matcher
            groups.append(new_group)
            changed = True
            continue

        if existing_handler.get("type") != "command":
            existing_handler["type"] = "command"
            changed = True
        if existing_handler.get("command") != hook_command:
            existing_handler["command"] = hook_command
            changed = True
        if matcher:
            if existing_group.get("matcher") != matcher:
                existing_group["matcher"] = matcher
                changed = True
        elif "matcher" in existing_group:
            existing_group.pop("matcher", None)
            changed = True

    if not changed and hooks_path.exists():
        return False
    hooks_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def install_cli_hooks(
    *,
    config_path: Path | None = None,
    hooks_path: Path | None = None,
    hook_command: str | None = None,
) -> dict[str, Any]:
    config_path = config_path or default_config_path()
    hooks_path = hooks_path or default_hooks_path()
    hook_command = hook_command or resolve_hook_command()
    config_changed = ensure_codex_hooks_enabled(config_path)
    hooks_changed = install_dashboard_hooks(hooks_path, hook_command=hook_command)
    return {
        "config_path": str(config_path),
        "hooks_path": str(hooks_path),
        "hook_command": hook_command,
        "config_changed": config_changed,
        "hooks_changed": hooks_changed,
    }


def _status_detail(event_name: str, payload: dict[str, Any]) -> str:
    command = ((payload.get("tool_input") or {}) if isinstance(payload.get("tool_input"), dict) else {}).get("command")
    if event_name == "SessionStart":
        return "Waiting for input"
    if event_name == "UserPromptSubmit":
        return "Processing prompt"
    if event_name == "PreToolUse":
        return f"Running Bash: {command}" if command else "Running Bash command"
    if event_name == "PostToolUse":
        return "Processing command result"
    if event_name == "Stop":
        return "Waiting for input"
    return event_name


def _parent_process_info(parent_pid: int) -> tuple[str | None, str | None]:
    try:
        output = subprocess.check_output(
            ["ps", "-p", str(parent_pid), "-o", "tty=", "-o", "args="],
            text=True,
        )
    except Exception:
        return None, None
    line = output.strip()
    if not line:
        return None, None
    parts = line.split(None, 1)
    if len(parts) == 1:
        tty, args = parts[0], None
    else:
        tty, args = parts
    if tty == "?":
        tty = None
    return tty, args


def normalize_hook_event(
    payload: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
    tty: str | None = None,
    command: str | None = None,
) -> dict[str, Any] | None:
    event_name = str(payload.get("hook_event_name", "")).strip()
    if event_name not in HOOK_EVENT_STATES:
        return None

    env = environ if environ is not None else os.environ
    parent_pid = parent_pid if parent_pid is not None else os.getppid()
    if tty is None or command is None:
        detected_tty, detected_command = _parent_process_info(parent_pid)
        tty = tty if tty is not None else detected_tty
        command = command if command is not None else detected_command

    transport = "tmux_terminal" if env.get("TMUX_PANE") else "cli_terminal"
    codex_session_id = str(payload.get("session_id", "")).strip() or None
    turn_id = str(payload.get("turn_id", "")).strip() or None
    cwd = str(payload.get("cwd", "")).strip() or os.getcwd()
    pid = parent_pid if parent_pid > 0 else None
    label = "Tmux Codex" if transport == "tmux_terminal" else "CLI Codex"
    if pid is not None:
        label = f"{label} {pid}"

    return {
        "hook_event_name": event_name,
        "dashboard_session_id": env.get("CODEX_DASHBOARD_SESSION_ID") or None,
        "codex_session_id": codex_session_id,
        "turn_id": turn_id,
        "state": HOOK_EVENT_STATES[event_name],
        "status_detail": _status_detail(event_name, payload),
        "pid": pid,
        "tty": tty,
        "tmux_pane": env.get("TMUX_PANE") or None,
        "cwd": cwd,
        "command": command or "codex",
        "name": label,
        "transport": transport,
    }
