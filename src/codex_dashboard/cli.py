from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


class DashboardCliError(RuntimeError):
    pass


def _trimmed_base_url(value: str) -> str:
    return value.rstrip("/")


def _default_socket_path() -> str:
    runtime_dir = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp"))
    return os.getenv("CODEX_DASHBOARD_AGENT_SOCKET_PATH", str(runtime_dir / "codex-dashboard-agent.sock"))


def _normalize_codex_args(items: list[str]) -> list[str]:
    if items and items[0] == "--":
        return items[1:]
    return items


def _post_json(opener: Any, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            detail = json.loads(raw).get("detail", raw)
        except json.JSONDecodeError:
            detail = raw or exc.reason
        raise DashboardCliError(f"{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise DashboardCliError(f"Failed to reach dashboard: {exc.reason}") from exc

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DashboardCliError(f"Unexpected non-JSON response from dashboard: {raw[:200]}") from exc


def _post_local_socket(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = []
        while True:
            data = client.recv(8192)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    except FileNotFoundError as exc:
        raise DashboardCliError(f"Agent socket not found: {socket_path}") from exc
    except OSError as exc:
        raise DashboardCliError(f"Failed to reach local agent socket: {exc}") from exc
    finally:
        client.close()

    raw = b"".join(chunks).decode("utf-8").strip()
    if not raw:
        raise DashboardCliError("Local agent returned an empty response")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DashboardCliError(f"Unexpected non-JSON response from local agent: {raw[:200]}") from exc
    if not data.get("ok"):
        raise DashboardCliError(str(data.get("error", "Local agent request failed")))
    return data


def launch_managed_session(
    *,
    server_url: str,
    username: str,
    password: str,
    agent_id: str,
    cwd: str,
    name: str,
    initial_prompt: str,
    launch_mode: str = "terminal",
    argv: list[str] | None = None,
) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    server_url = _trimmed_base_url(server_url)

    _post_json(
        opener,
        f"{server_url}/api/login",
        {
            "username": username,
            "password": password,
        },
    )
    return _post_json(
        opener,
        f"{server_url}/api/agents/{urllib.parse.quote(agent_id, safe='')}/sessions",
        {
            "name": name,
            "cwd": cwd,
            "initial_prompt": initial_prompt,
            "launch_mode": launch_mode,
            "argv": list(argv or []),
        },
    )


def launch_local_terminal_session(
    *,
    socket_path: str,
    cwd: str,
    name: str,
    initial_prompt: str,
    argv: list[str] | None = None,
) -> dict[str, Any]:
    return _post_local_socket(
        socket_path,
        {
            "type": "launch_terminal",
            "cwd": cwd,
            "name": name,
            "initial_prompt": initial_prompt,
            "argv": list(argv or []),
        },
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control Codex Dashboard from the command line.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="Launch a managed session on a connected agent.")
    launch.add_argument("--server", default=os.getenv("CODEX_DASHBOARD_URL", "http://127.0.0.1:8000"))
    launch.add_argument("--username", default=os.getenv("CODEX_DASHBOARD_USERNAME", "admin"))
    launch.add_argument("--password", default=os.getenv("CODEX_DASHBOARD_PASSWORD"))
    launch.add_argument("--agent", required=True, dest="agent_id")
    launch.add_argument("--cwd", required=True, help="Working directory on the target machine.")
    launch.add_argument("--name", default="Managed Codex Session")
    launch.add_argument("--mode", choices=["terminal", "app_server"], default="terminal")
    launch.add_argument("--prompt", default="", dest="initial_prompt")
    launch.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="Read the initial prompt from stdin instead of --prompt.",
    )
    launch.add_argument("codex_args", nargs=argparse.REMAINDER, help="Extra Codex args after -- for terminal launches.")

    launch_tty = subparsers.add_parser(
        "launch-tty",
        help="Launch a local tmux-backed managed session through the local machine agent.",
    )
    launch_tty.add_argument("--socket", default=_default_socket_path(), dest="socket_path")
    launch_tty.add_argument("--cwd", default=os.getcwd())
    launch_tty.add_argument("--name", default="Managed Codex Session")
    launch_tty.add_argument("--prompt", default="", dest="initial_prompt")
    launch_tty.add_argument("--detach", action="store_true", help="Do not attach to tmux after creating the session.")
    launch_tty.add_argument("codex_args", nargs=argparse.REMAINDER, help="Codex args after --.")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "launch":
        password = args.password or getpass.getpass("Dashboard password: ")
        initial_prompt = args.initial_prompt
        if args.prompt_stdin:
            initial_prompt = sys.stdin.read()

        try:
            result = launch_managed_session(
                server_url=args.server,
                username=args.username,
                password=password,
                agent_id=args.agent_id,
                cwd=args.cwd,
                name=args.name,
                initial_prompt=initial_prompt,
                launch_mode=args.mode,
                argv=_normalize_codex_args(args.codex_args),
            )
        except DashboardCliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        session_id = result.get("session_id")
        print(json.dumps({"ok": True, "session_id": session_id}, indent=2))
        return

    if args.command == "launch-tty":
        try:
            result = launch_local_terminal_session(
                socket_path=args.socket_path,
                cwd=args.cwd,
                name=args.name,
                initial_prompt=args.initial_prompt,
                argv=_normalize_codex_args(args.codex_args),
            )
        except DashboardCliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        meta = result.get("meta", {})
        tmux_session = meta.get("tmux_session")
        attach_command = meta.get("attach_command")
        if not args.detach and sys.stdin.isatty() and sys.stdout.isatty() and tmux_session:
            os.execvp("tmux", ["tmux", "attach-session", "-t", tmux_session])
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": result.get("session_id"),
                    "attach_command": attach_command,
                    "tmux_session": tmux_session,
                },
                indent=2,
            )
        )
        return

    parser.error(f"Unsupported command: {args.command}")
