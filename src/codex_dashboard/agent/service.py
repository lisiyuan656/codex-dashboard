from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
import platform
import shutil
import socket
import subprocess
from typing import Any

import websockets

from .config import AgentConfig
from .session import ManagedPtySession


class DashboardAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.hostname = socket.gethostname()
        self.sessions: dict[str, ManagedPtySession] = {}
        self.ws: websockets.ClientConnection | None = None

    async def emit(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps(payload))

    async def run_forever(self) -> None:
        while True:
            try:
                await self._run_once()
            except Exception as exc:
                print(f"[agent] connection loop failed: {exc}")
                await asyncio.sleep(5)

    async def _run_once(self) -> None:
        url = f"{self.config.ws_url}?agent_id={self.config.agent_id}&token={self.config.token}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
            self.ws = websocket
            await self.emit(
                {
                    "type": "hello",
                    "hostname": self.hostname,
                    "display_name": self.config.display_name,
                    "labels": self.config.labels,
                    "meta": self._machine_meta(),
                }
            )
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            watch_task = asyncio.create_task(self._watch_loop())
            try:
                async for raw in websocket:
                    payload = json.loads(raw)
                    await self._handle_action(payload)
            finally:
                heartbeat_task.cancel()
                watch_task.cancel()
                self.ws = None

    async def _heartbeat_loop(self) -> None:
        while True:
            await self.emit(
                {
                    "type": "heartbeat",
                    "meta": {
                        **self._machine_meta(),
                        "managed_sessions": len([s for s in self.sessions.values() if s.process and s.process.returncode is None]),
                    },
                }
            )
            for session_id, session in list(self.sessions.items()):
                if session.process is None or session.process.returncode is not None:
                    continue
                await self.emit({"type": "session_event", "event_type": "heartbeat", "session_id": session_id, "state": "running"})
            await asyncio.sleep(self.config.heartbeat_seconds)

    async def _watch_loop(self) -> None:
        while True:
            for item in self._discover_unmanaged_codex():
                await self.emit({"type": "session_event", "event_type": "detected", **item})
            await asyncio.sleep(self.config.watch_seconds)

    async def _handle_action(self, payload: dict[str, Any]) -> None:
        action_type = payload.get("type")
        session_id = payload.get("session_id")
        if action_type == "launch_session":
            session = ManagedPtySession(
                session_id=session_id,
                name=payload.get("name", "Managed Codex Session"),
                command=payload["command"],
                cwd=payload["cwd"],
                emit=self.emit,
            )
            self.sessions[session_id] = session
            await session.start()
            return
        session = self.sessions.get(session_id)
        if session is None:
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "failed",
                    "session_id": session_id,
                    "state": "failed",
                    "exit_code": -1,
                    "text": f"Unknown session for action {action_type}",
                }
            )
            return
        if action_type == "send_input":
            await session.send_input(payload["input"])
        elif action_type == "approve":
            await session.resolve_approval(approved=True, approval_id=payload["approval_id"])
        elif action_type == "deny":
            await session.resolve_approval(approved=False, approval_id=payload["approval_id"])
        elif action_type == "stop_session":
            await session.stop()

    def _machine_meta(self) -> dict[str, Any]:
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex_path": shutil.which("codex"),
        }

    def _discover_unmanaged_codex(self) -> Iterable[dict[str, Any]]:
        managed_pids = {session.process.pid for session in self.sessions.values() if session.process is not None}
        try:
            output = subprocess.check_output(["ps", "-eo", "pid=,comm=,args="], text=True)
        except Exception:
            return []

        discovered = []
        for line in output.splitlines():
            if "codex" not in line:
                continue
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid = int(parts[0])
            if pid in managed_pids:
                continue
            discovered.append(
                {
                    "session_id": f"unmanaged-{self.config.agent_id}-{pid}",
                    "source": "unmanaged",
                    "state": "detected",
                    "name": f"Unmanaged Codex {pid}",
                    "command": parts[2],
                    "cwd": ".",
                    "pid": pid,
                }
            )
        return discovered
