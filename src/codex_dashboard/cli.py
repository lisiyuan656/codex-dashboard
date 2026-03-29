from __future__ import annotations

import argparse
import getpass
import json
import os
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


def launch_managed_session(
    *,
    server_url: str,
    username: str,
    password: str,
    agent_id: str,
    cwd: str,
    name: str,
    initial_prompt: str,
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
    launch.add_argument("--prompt", default="", dest="initial_prompt")
    launch.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="Read the initial prompt from stdin instead of --prompt.",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command != "launch":
        parser.error(f"Unsupported command: {args.command}")

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
        )
    except DashboardCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    session_id = result.get("session_id")
    print(json.dumps({"ok": True, "session_id": session_id}, indent=2))
