from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
from pathlib import Path
import platform
import shutil
import socket
import subprocess
from typing import Any
import uuid

import websockets

from .config import AgentConfig
from .session import AppServerSession, SessionRuntime, TmuxTerminalSession


class DashboardAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.hostname = socket.gethostname()
        self.sessions: dict[str, SessionRuntime] = {}
        self.ws: websockets.ClientConnection | None = None
        self._socket_server: asyncio.AbstractServer | None = None

    async def emit(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps(payload))

    async def run_forever(self) -> None:
        await self._start_local_socket_server()
        try:
            while True:
                try:
                    await self._run_once()
                except Exception as exc:
                    print(f"[agent] connection loop failed: {exc}")
                    await asyncio.sleep(5)
        finally:
            await self._stop_local_socket_server()

    async def run_once(self) -> None:
        await self._start_local_socket_server()
        try:
            await self._run_once()
        finally:
            await self._stop_local_socket_server()

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
            await self._sync_sessions()
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
            managed_sessions = [session for session in self.sessions.values() if session.is_alive]
            await self.emit(
                {
                    "type": "heartbeat",
                    "meta": {
                        **self._machine_meta(),
                        "managed_sessions": len(managed_sessions),
                        "terminal_sessions": len([session for session in managed_sessions if session.transport == "tmux_terminal"]),
                    },
                }
            )
            for session in list(self.sessions.values()):
                if not session.is_alive:
                    continue
                await session.emit_heartbeat()
            await asyncio.sleep(self.config.heartbeat_seconds)

    async def _watch_loop(self) -> None:
        while True:
            for item in self._discover_unmanaged_codex():
                await self.emit({"type": "session_event", "event_type": "detected", **item})
            await asyncio.sleep(self.config.watch_seconds)

    async def _handle_action(self, payload: dict[str, Any]) -> None:
        action_type = payload.get("type")
        if action_type == "launch_session":
            try:
                await self._launch_from_action(payload)
            except Exception:
                return
            return
        if action_type == "restore_sessions":
            await self._restore_sessions(payload.get("sessions", []))
            return

        session_id = payload.get("session_id")
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

        try:
            if action_type == "send_input":
                await session.send_input(payload["input"])
            elif action_type == "send_enter":
                await session.send_enter()
            elif action_type == "interrupt_session":
                await session.interrupt()
            elif action_type == "approve":
                await session.resolve_approval(approved=True, approval_id=payload["approval_id"])
            elif action_type == "deny":
                await session.resolve_approval(approved=False, approval_id=payload["approval_id"])
            elif action_type == "stop_session":
                await session.stop()
        except Exception as exc:
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "output",
                    "session_id": session_id,
                    "text": f"[agent action error] {exc}\n",
                    "state": session.state,
                    "meta": {
                        "transport": session.transport,
                    },
                }
            )

    async def _launch_from_action(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("session_id", uuid.uuid4().hex)
        name = payload.get("name", "Managed Codex Session")
        cwd = payload.get("cwd", ".")
        launch_mode = payload.get("launch_mode", "terminal")
        initial_prompt = payload.get("prompt", "")
        argv = [str(item) for item in payload.get("argv", [])]
        tmux_pane = payload.get("tmux_pane")

        if launch_mode == "app_server":
            session: SessionRuntime = AppServerSession(
                session_id=session_id,
                name=name,
                cwd=cwd,
                initial_prompt=initial_prompt,
                codex_bin=self.config.codex_bin,
                emit=self.emit,
            )
        else:
            session = TmuxTerminalSession(
                session_id=session_id,
                name=name,
                cwd=cwd,
                codex_bin=self.config.codex_bin,
                tmux_bin=self.config.tmux_bin,
                emit=self.emit,
                spool_dir=self.config.spool_dir,
                argv=argv,
                initial_prompt=initial_prompt,
                existing_pane_id=tmux_pane,
            )

        self.sessions[session_id] = session
        try:
            await session.start()
        except Exception as exc:
            self.sessions.pop(session_id, None)
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "failed",
                    "session_id": session_id,
                    "state": "failed",
                    "exit_code": -1,
                    "name": name,
                    "command": getattr(session, "command", self.config.codex_bin),
                    "cwd": cwd,
                    "source": "managed",
                    "meta": {
                        "transport": launch_mode if launch_mode == "app_server" else "tmux_terminal",
                        "tmux_launch_mode": "detached_session" if not tmux_pane else "current_pane",
                    },
                    "text": f"[agent launch error] {exc}",
                }
            )
            raise

    async def _restore_sessions(self, payloads: list[dict[str, Any]]) -> None:
        for item in payloads:
            session_id = item.get("session_id")
            if not session_id:
                continue
            existing = self.sessions.get(session_id)
            if existing is not None:
                await existing.sync()
                continue

            session = TmuxTerminalSession(
                session_id=session_id,
                name=item.get("name", "Managed Codex Session"),
                cwd=item.get("cwd", "."),
                codex_bin=self.config.codex_bin,
                tmux_bin=self.config.tmux_bin,
                emit=self.emit,
                spool_dir=self.config.spool_dir,
                argv=[str(value) for value in item.get("argv", [])],
                initial_prompt=item.get("initial_prompt", ""),
                existing_pane_id=item.get("tmux_pane"),
            )
            self.sessions[session_id] = session
            try:
                await session.restore(
                    session_name=item.get("tmux_session"),
                    launch_mode=item.get("tmux_launch_mode", "detached_session"),
                    repo_path=item.get("repo_path"),
                    git_branch=item.get("git_branch"),
                    pid=item.get("pid"),
                    pane_tty=item.get("pane_tty"),
                    attached_clients=int(item.get("attached_clients") or 0),
                    pane_current_command=item.get("pane_current_command"),
                )
                await session.sync()
                if not session.is_alive:
                    self.sessions.pop(session_id, None)
            except Exception as exc:
                self.sessions.pop(session_id, None)
                await self.emit(
                    {
                        "type": "session_event",
                        "event_type": "output",
                        "session_id": session_id,
                        "text": f"[agent restore error] {exc}\n",
                        "state": "failed",
                        "meta": {
                            "transport": "tmux_terminal",
                        },
                    }
                )

    async def _start_local_socket_server(self) -> None:
        if self._socket_server is not None:
            return
        socket_path = Path(self.config.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self._socket_server = await asyncio.start_unix_server(self._handle_local_client, path=str(socket_path))
        socket_path.chmod(0o600)

    async def _stop_local_socket_server(self) -> None:
        if self._socket_server is not None:
            self._socket_server.close()
            await self._socket_server.wait_closed()
            self._socket_server = None
        Path(self.config.socket_path).unlink(missing_ok=True)

    async def _handle_local_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            response = await self._handle_local_request(request)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _handle_local_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_type = payload.get("type")
        if request_type == "launch_terminal":
            session_id = payload.get("session_id", uuid.uuid4().hex)
            await self._launch_from_action(
                {
                    "type": "launch_session",
                    "session_id": session_id,
                    "launch_mode": "terminal",
                    "name": payload.get("name", "Managed Codex Session"),
                    "cwd": payload.get("cwd", "."),
                    "prompt": payload.get("initial_prompt", ""),
                    "argv": payload.get("argv", []),
                    "tmux_pane": payload.get("tmux_pane"),
                }
            )
            session = self.sessions[session_id]
            meta = {
                "transport": session.transport,
            }
            if isinstance(session, TmuxTerminalSession):
                meta.update(session._meta())
            return {
                "ok": True,
                "session_id": session_id,
                "name": session.name,
                "cwd": session.cwd,
                "command": session.command,
                "meta": meta,
            }
        if request_type == "complete_terminal":
            session = self._require_session(payload["session_id"], transport="tmux_terminal")
            await session.report_completion(payload.get("exit_code"))
            return {"ok": True}
        if request_type == "send_terminal_input":
            session = self._require_session(payload["session_id"], transport="tmux_terminal")
            await session.send_input(payload["input"])
            return {"ok": True}
        if request_type == "interrupt_terminal":
            session = self._require_session(payload["session_id"], transport="tmux_terminal")
            await session.interrupt()
            return {"ok": True}
        if request_type == "session_status":
            session_id = payload.get("session_id")
            if session_id:
                session = self._require_session(session_id)
                return {"ok": True, "session": self._session_snapshot(session)}
            return {"ok": True, "sessions": [self._session_snapshot(session) for session in self.sessions.values()]}
        raise RuntimeError(f"Unsupported local request: {request_type}")

    def _require_session(self, session_id: str, *, transport: str | None = None) -> SessionRuntime:
        session = self.sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"Unknown session: {session_id}")
        if transport and session.transport != transport:
            raise RuntimeError(f"Session {session_id} is not a {transport} session")
        return session

    def _session_snapshot(self, session: SessionRuntime) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "name": session.name,
            "cwd": session.cwd,
            "command": session.command,
            "state": session.state,
            "pid": session.pid,
            "transport": session.transport,
        }

    async def _sync_sessions(self) -> None:
        for session in list(self.sessions.values()):
            await session.sync()

    def _machine_meta(self) -> dict[str, Any]:
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex_path": shutil.which(self.config.codex_bin) or self.config.codex_bin,
            "tmux_path": shutil.which(self.config.tmux_bin) or self.config.tmux_bin,
            "socket_path": self.config.socket_path,
            "spool_dir": self.config.spool_dir,
        }

    def _discover_unmanaged_codex(self) -> Iterable[dict[str, Any]]:
        managed_pids: set[int] = set()
        managed_ttys: set[str] = set()
        for session in self.sessions.values():
            managed_pids.update(session.discovery_pids())
            managed_ttys.update(session.discovery_ttys())

        codex_names = {Path(self.config.codex_bin).name}
        try:
            output = subprocess.check_output(["ps", "-eo", "pid=,comm=,tty=,args="], text=True)
        except Exception:
            return []

        discovered = []
        for line in output.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            pid = int(parts[0])
            comm = parts[1]
            tty = parts[2]
            args = parts[3]
            if comm not in codex_names:
                continue
            if pid in managed_pids or tty in managed_ttys:
                continue
            discovered.append(
                {
                    "session_id": f"unmanaged-{self.config.agent_id}-{pid}",
                    "source": "unmanaged",
                    "state": "detected",
                    "name": f"Unmanaged Codex {pid}",
                    "command": args,
                    "cwd": ".",
                    "pid": pid,
                    "meta": {
                        "transport": "unmanaged",
                        "tty": tty,
                    },
                }
            )
        return discovered
