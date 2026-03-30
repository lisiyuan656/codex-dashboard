from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import shlex
from typing import Any, Protocol


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class SessionRuntime(Protocol):
    session_id: str
    name: str
    cwd: str
    command: str
    transport: str
    state: str

    @property
    def pid(self) -> int | None: ...

    @property
    def is_alive(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_input(self, text: str) -> None: ...

    async def send_enter(self) -> None: ...

    async def interrupt(self) -> None: ...

    async def resolve_approval(self, *, approved: bool, approval_id: str) -> None: ...

    async def emit_heartbeat(self) -> None: ...

    async def sync(self) -> None: ...

    async def report_completion(self, exit_code: int | None) -> None: ...

    def discovery_pids(self) -> set[int]: ...

    def discovery_ttys(self) -> set[str]: ...


class ManagedSessionBase:
    transport = "managed"

    def __init__(self, *, session_id: str, name: str, cwd: str, emit: EventCallback) -> None:
        self.session_id = session_id
        self.name = name
        self.cwd = cwd
        self.emit = emit
        self.state = "launching"

    @property
    def pid(self) -> int | None:
        return None

    @property
    def is_alive(self) -> bool:
        return self.state not in {"stopped", "finished", "failed"}

    async def send_enter(self) -> None:
        raise RuntimeError(f"{self.transport} sessions do not support send_enter")

    async def interrupt(self) -> None:
        raise RuntimeError(f"{self.transport} sessions do not support interrupt")

    async def resolve_approval(self, *, approved: bool, approval_id: str) -> None:
        raise RuntimeError(f"{self.transport} sessions do not support approvals")

    async def emit_heartbeat(self) -> None:
        return None

    async def sync(self) -> None:
        return None

    async def report_completion(self, exit_code: int | None) -> None:
        del exit_code
        return None

    def discovery_pids(self) -> set[int]:
        return {self.pid} if self.pid is not None else set()

    def discovery_ttys(self) -> set[str]:
        return set()


class AppServerProtocolError(RuntimeError):
    pass


class AppServerSession(ManagedSessionBase):
    transport = "app_server"

    def __init__(
        self,
        *,
        session_id: str,
        name: str,
        cwd: str,
        initial_prompt: str,
        codex_bin: str,
        emit: EventCallback,
    ) -> None:
        super().__init__(session_id=session_id, name=name, cwd=cwd, emit=emit)
        self.initial_prompt = initial_prompt
        self.codex_bin = codex_bin
        self.command = f"{self.codex_bin} app-server"
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self._request_id = 1
        self._write_lock = asyncio.Lock()
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._pending_server_requests: dict[str, dict[str, Any]] = {}
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._stdout_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._wait_task = asyncio.create_task(self._wait_loop())

        await self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-dashboard",
                    "version": "0.1.0",
                }
            },
        )
        await self._send_notification("initialized", {})

        thread_result = await self._send_request(
            "thread/start",
            {
                "cwd": self.cwd,
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
            },
        )
        thread = thread_result["thread"]
        self.thread_id = thread["id"]
        self.state = self._map_thread_status(thread.get("status"))

        await self.emit(
            {
                "type": "session_event",
                "event_type": "started",
                "session_id": self.session_id,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "repo_path": self.cwd,
                "git_branch": ((thread.get("gitInfo") or {}).get("branch") if thread.get("gitInfo") else None),
                "meta": {
                    "transport": self.transport,
                    "thread_id": self.thread_id,
                    "initial_prompt": self.initial_prompt,
                },
            }
        )

        if self.initial_prompt.strip():
            await self.send_input(self.initial_prompt.strip())

    async def send_input(self, text: str) -> None:
        if not self.thread_id:
            raise AppServerProtocolError("Session thread is not initialized")
        input_items = [{"type": "text", "text": text}]
        if self.active_turn_id:
            result = await self._send_request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": self.active_turn_id,
                    "input": input_items,
                },
            )
        else:
            result = await self._send_request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": input_items,
                },
            )
        turn = result["turn"]
        self.active_turn_id = turn["id"]
        self.state = self._map_turn_status(turn.get("status"))
        await self.emit_heartbeat()

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        if self.thread_id and self.active_turn_id:
            try:
                await self._send_request(
                    "turn/interrupt",
                    {
                        "threadId": self.thread_id,
                        "turnId": self.active_turn_id,
                    },
                )
            except Exception:
                pass
        self.process.terminate()

    async def send_enter(self) -> None:
        await self.send_input("")

    async def interrupt(self) -> None:
        await self.stop()

    async def resolve_approval(self, *, approved: bool, approval_id: str) -> None:
        request = self._pending_server_requests.pop(approval_id, None)
        if request is None:
            return

        method = request["method"]
        params = request["params"]
        if method == "item/commandExecution/requestApproval":
            result = {"decision": "accept" if approved else "decline"}
        elif method == "item/fileChange/requestApproval":
            result = {"decision": "accept" if approved else "decline"}
        elif method == "item/permissions/requestApproval":
            permissions = params.get("permissions", {}) if approved else {}
            result = {"permissions": permissions, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            result = {}

        await self._send_response(int(approval_id), result)
        await self.emit(
            {
                "type": "session_event",
                "event_type": "approval_resolved",
                "session_id": self.session_id,
                "approval_id": approval_id,
                "status": "approved" if approved else "denied",
                "meta": {
                    "transport": self.transport,
                },
            }
        )

    async def emit_heartbeat(self) -> None:
        await self.emit(
            {
                "type": "session_event",
                "event_type": "heartbeat",
                "session_id": self.session_id,
                "state": self.state,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "meta": {
                    "transport": self.transport,
                    "thread_id": self.thread_id,
                    "turn_id": self.active_turn_id,
                },
            }
        )

    async def sync(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            await self.emit_heartbeat()
            return
        await self.emit(
            {
                "type": "session_event",
                "event_type": "stopped" if self.process.returncode == 0 else "failed",
                "session_id": self.session_id,
                "state": "finished" if self.process.returncode == 0 else "failed",
                "exit_code": self.process.returncode,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "meta": {
                    "transport": self.transport,
                    "thread_id": self.thread_id,
                    "turn_id": self.active_turn_id,
                },
            }
        )

    async def _stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            await self._handle_message(payload)

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "output",
                    "session_id": self.session_id,
                    "text": f"[app-server stderr] {text}",
                    "state": self.state,
                    "meta": {
                        "transport": self.transport,
                    },
                }
            )

    async def _wait_loop(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        error = AppServerProtocolError(f"App-server exited before request completed (code {code})")
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()
        if self._stdout_task is not None:
            self._stdout_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        state = "finished" if code == 0 else "failed"
        self.state = state
        await self.emit(
            {
                "type": "session_event",
                "event_type": "stopped" if code == 0 else "failed",
                "session_id": self.session_id,
                "exit_code": code,
                "state": state,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "meta": {
                    "transport": self.transport,
                    "thread_id": self.thread_id,
                    "turn_id": self.active_turn_id,
                },
            }
        )

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        if "id" in payload and "method" in payload:
            await self._handle_server_request(payload)
            return
        if "id" in payload:
            future = self._pending_requests.pop(int(payload["id"]), None)
            if future is None:
                return
            if "error" in payload:
                future.set_exception(AppServerProtocolError(payload["error"].get("message", "Unknown app-server error")))
            else:
                future.set_result(payload.get("result"))
            return
        if "method" in payload:
            await self._handle_notification(payload)

    async def _handle_server_request(self, payload: dict[str, Any]) -> None:
        request_id = str(payload["id"])
        method = payload["method"]
        params = payload.get("params", {})
        self._pending_server_requests[request_id] = {
            "method": method,
            "params": params,
        }

        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
        }:
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "approval_requested",
                    "session_id": self.session_id,
                    "approval_id": request_id,
                    "prompt": self._format_approval_prompt(method, params),
                    "state": "awaiting_approval",
                    "meta": {
                        "transport": self.transport,
                        "thread_id": params.get("threadId", self.thread_id),
                        "turn_id": params.get("turnId", self.active_turn_id),
                        "approval_method": method,
                    },
                }
            )
            self.state = "awaiting_approval"
            return

        await self._send_response(int(request_id), {})

    async def _handle_notification(self, payload: dict[str, Any]) -> None:
        method = payload["method"]
        params = payload.get("params", {})

        if method == "thread/started":
            thread = params["thread"]
            self.thread_id = thread["id"]
            self.state = self._map_thread_status(thread.get("status"))
            return

        if method == "thread/status/changed":
            self.state = self._map_thread_status(params.get("status"))
            await self.emit_heartbeat()
            return

        if method == "turn/started":
            turn = params["turn"]
            self.active_turn_id = turn["id"]
            self.state = self._map_turn_status(turn.get("status"))
            await self.emit_heartbeat()
            return

        if method == "turn/completed":
            turn = params["turn"]
            self.active_turn_id = None
            self.state = self._map_turn_completion(turn)
            if turn.get("error"):
                await self.emit(
                    {
                        "type": "session_event",
                        "event_type": "output",
                        "session_id": self.session_id,
                        "text": f"[turn error] {turn['error'].get('message', 'unknown error')}\n",
                        "state": self.state,
                        "meta": {
                            "transport": self.transport,
                        },
                    }
                )
            await self.emit_heartbeat()
            return

        if method in {"item/agentMessage/delta", "item/commandExecution/outputDelta", "item/plan/delta"}:
            text = params.get("delta", "")
            if text:
                await self.emit(
                    {
                        "type": "session_event",
                        "event_type": "output",
                        "session_id": self.session_id,
                        "text": text,
                        "state": self.state,
                        "meta": {
                            "transport": self.transport,
                            "thread_id": params.get("threadId", self.thread_id),
                            "turn_id": params.get("turnId", self.active_turn_id),
                        },
                    }
                )
            return

        if method == "serverRequest/resolved":
            request_id = str(params["requestId"])
            self._pending_server_requests.pop(request_id, None)
            return

        if method == "error":
            error = params.get("error", {})
            text = error.get("message", "Unknown app-server error")
            details = error.get("additionalDetails")
            if details:
                text = f"{text}: {details}"
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "output",
                    "session_id": self.session_id,
                    "text": f"[app-server error] {text}\n",
                    "state": self.state,
                    "meta": {
                        "transport": self.transport,
                        "thread_id": params.get("threadId", self.thread_id),
                        "turn_id": params.get("turnId", self.active_turn_id),
                    },
                }
            )
            return

        if method in {"item/started", "item/completed"}:
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "heartbeat",
                    "session_id": self.session_id,
                    "state": self.state,
                    "meta": {
                        "transport": self.transport,
                        "thread_id": params.get("threadId", self.thread_id),
                        "turn_id": params.get("turnId", self.active_turn_id),
                        "last_item_type": (params.get("item") or {}).get("type"),
                    },
                }
            )

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _send_response(self, request_id: int, result: dict[str, Any]) -> None:
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        )

    async def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._request_id
        self._request_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending_requests[request_id] = future
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return await future

    async def _write_json(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerProtocolError("App-server process is not running")
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()

    def _map_thread_status(self, status: Any) -> str:
        if isinstance(status, dict):
            status_type = status.get("type")
        else:
            status_type = status
        return {
            "idle": "idle",
            "active": "running",
            "archived": "finished",
            "closed": "finished",
        }.get(status_type, "running")

    def _map_turn_status(self, status: Any) -> str:
        if isinstance(status, dict):
            status_type = status.get("type")
        else:
            status_type = status
        return {
            "inProgress": "running",
            "completed": "idle",
            "interrupted": "idle",
            "failed": "failed",
        }.get(status_type, "running")

    def _map_turn_completion(self, turn: dict[str, Any]) -> str:
        return self._map_turn_status(turn.get("status"))

    def _format_approval_prompt(self, method: str, params: dict[str, Any]) -> str:
        if method == "item/commandExecution/requestApproval":
            parts = ["Command approval requested"]
            if params.get("command"):
                parts.append(f"Command: {params['command']}")
            if params.get("cwd"):
                parts.append(f"CWD: {params['cwd']}")
            if params.get("reason"):
                parts.append(f"Reason: {params['reason']}")
            return "\n".join(parts)

        if method == "item/fileChange/requestApproval":
            parts = ["File change approval requested"]
            if params.get("grantRoot"):
                parts.append(f"Grant root: {params['grantRoot']}")
            if params.get("reason"):
                parts.append(f"Reason: {params['reason']}")
            return "\n".join(parts)

        if method == "item/permissions/requestApproval":
            return json.dumps(
                {
                    "label": "Permission approval requested",
                    "reason": params.get("reason"),
                    "permissions": params.get("permissions"),
                },
                indent=2,
                sort_keys=True,
            )

        if method == "item/tool/requestUserInput":
            return json.dumps(
                {
                    "label": "Tool requested user input",
                    "payload": params,
                },
                indent=2,
                sort_keys=True,
            )

        return json.dumps(params, indent=2, sort_keys=True)


class TmuxSessionError(RuntimeError):
    pass


class TmuxTerminalSession(ManagedSessionBase):
    transport = "tmux_terminal"

    def __init__(
        self,
        *,
        session_id: str,
        name: str,
        cwd: str,
        codex_bin: str,
        tmux_bin: str,
        emit: EventCallback,
        spool_dir: str,
        argv: list[str] | None = None,
        initial_prompt: str = "",
        existing_pane_id: str | None = None,
    ) -> None:
        super().__init__(session_id=session_id, name=name, cwd=cwd, emit=emit)
        self.codex_bin = codex_bin
        self.tmux_bin = tmux_bin
        self.argv = list(argv or [])
        self.initial_prompt = initial_prompt
        self.command = shlex.join([self.codex_bin, "--no-alt-screen", *self.argv])
        self._requested_session_name = f"codexdash-{self.session_id[:12]}"
        self.session_name: str | None = None
        self.log_path = Path(spool_dir) / f"{self.session_id}.log"
        self.repo_path: str | None = None
        self.git_branch: str | None = None
        self.pane_id: str | None = existing_pane_id
        self.pane_tty: str | None = None
        self._pid: int | None = None
        self.attached_clients = 0
        self.exit_code: int | None = None
        self._log_offset = 0
        self._log_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._final_event_sent = False
        self._completion_reported = False
        self._observed_foreground_codex = False
        self._pane_current_command: str | None = None
        self._pipe_started = False
        self._owns_session = existing_pane_id is None
        self._expected_commands = {Path(self.codex_bin).name, "codex"}

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def is_alive(self) -> bool:
        return not self._final_event_sent and self.state not in {"stopped", "finished", "failed"}

    def discovery_ttys(self) -> set[str]:
        if not self.pane_tty:
            return set()
        return {Path(self.pane_tty).name, self.pane_tty.removeprefix("/dev/")}

    async def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")
        self.repo_path = await self._git_value("rev-parse", "--show-toplevel")
        self.git_branch = await self._git_value("branch", "--show-current")

        if self._owns_session:
            shell_command = f"exec {self.command}"
            await self._tmux("new-session", "-d", "-s", self._requested_session_name, "-c", self.cwd, shell_command)
            await self._tmux("set-option", "-t", self._requested_session_name, "remain-on-exit", "on")
        await self._refresh_tmux_state()
        if self.pane_id is None:
            raise TmuxSessionError("tmux did not return a pane id")
        await self._tmux("pipe-pane", "-t", self.pane_id, f"cat >> {shlex.quote(str(self.log_path))}")
        self._pipe_started = True
        self._log_task = asyncio.create_task(self._log_loop())

        await self.emit(
            {
                "type": "session_event",
                "event_type": "started",
                "session_id": self.session_id,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "repo_path": self.repo_path,
                "git_branch": self.git_branch,
                "meta": self._meta(),
            }
        )

        if self._owns_session and self.initial_prompt.strip():
            await asyncio.sleep(0.4)
            await self.send_input(self.initial_prompt.strip())
            await self.send_enter()

    async def stop(self) -> None:
        if self._final_event_sent:
            return
        self._stop_requested = True
        if self.pane_id is not None:
            try:
                await self._tmux("send-keys", "-t", self.pane_id, "C-c")
                await asyncio.sleep(0.3)
            except TmuxSessionError:
                pass
        if self._owns_session:
            await self._tmux("kill-session", "-t", self.session_name or self._requested_session_name, check=False)
            await self._finalize("stopped", 130)
            return
        await self._refresh_tmux_state()
        if not self._observed_foreground_codex or (self._pane_current_command not in self._expected_commands):
            await self._finalize("stopped", 130)

    async def send_input(self, text: str) -> None:
        if self.pane_id is None:
            raise TmuxSessionError("Terminal session is not initialized")
        pieces = text.splitlines(keepends=True) or [text]
        for piece in pieces:
            stripped = piece.rstrip("\n")
            if stripped:
                await self._tmux("send-keys", "-t", self.pane_id, "-l", "--", stripped)
            if piece.endswith("\n"):
                await self.send_enter()

    async def send_enter(self) -> None:
        if self.pane_id is None:
            raise TmuxSessionError("Terminal session is not initialized")
        await self._tmux("send-keys", "-t", self.pane_id, "Enter")

    async def interrupt(self) -> None:
        if self.pane_id is None:
            raise TmuxSessionError("Terminal session is not initialized")
        await self._tmux("send-keys", "-t", self.pane_id, "C-c")

    async def emit_heartbeat(self) -> None:
        await self._refresh_tmux_state()
        if self._final_event_sent:
            return
        await self.emit(
            {
                "type": "session_event",
                "event_type": "heartbeat",
                "session_id": self.session_id,
                "state": self.state,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "repo_path": self.repo_path,
                "git_branch": self.git_branch,
                "meta": self._meta(),
            }
        )

    async def sync(self) -> None:
        await self._refresh_tmux_state()
        if self._final_event_sent:
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "failed" if self.state == "failed" else "stopped",
                    "session_id": self.session_id,
                    "state": self.state,
                    "exit_code": self.exit_code,
                    "name": self.name,
                    "command": self.command,
                    "cwd": self.cwd,
                    "pid": self.pid,
                    "source": "managed",
                    "repo_path": self.repo_path,
                    "git_branch": self.git_branch,
                    "meta": self._meta(),
                }
            )
            return
        await self.emit_heartbeat()

    async def report_completion(self, exit_code: int | None) -> None:
        if self._final_event_sent:
            return
        self.exit_code = exit_code
        self._completion_reported = True
        await self._drain_log()
        state = "failed" if exit_code not in {None, 0, 130} else "stopped"
        await self._finalize(state, exit_code)

    async def resolve_approval(self, *, approved: bool, approval_id: str) -> None:
        raise RuntimeError("tmux terminal sessions do not support structured approvals")

    async def _log_loop(self) -> None:
        while True:
            await self._drain_log()
            await self._refresh_tmux_state()
            if self._final_event_sent:
                await self._drain_log()
                break
            await asyncio.sleep(0.25)

    async def _drain_log(self) -> None:
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._log_offset)
            chunk = handle.read()
            self._log_offset = handle.tell()
        if not chunk:
            return
        await self.emit(
            {
                "type": "session_event",
                "event_type": "output",
                "session_id": self.session_id,
                "text": chunk,
                "state": self.state,
                "meta": self._meta(),
            }
        )

    async def _refresh_tmux_state(self) -> None:
        if self._final_event_sent:
            return
        target = self.pane_id if not self._owns_session and self.pane_id else (self.session_name or self._requested_session_name)
        output = await self._tmux(
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}\t#{pane_dead_status}\t#{session_attached}\t#{pane_current_command}",
            check=False,
        )
        if output is None:
            await self._finalize("stopped" if self._stop_requested else "failed", self.exit_code)
            return

        fields = output.strip().split("\t")
        if len(fields) != 8:
            raise TmuxSessionError(f"Unexpected tmux state line: {output!r}")
        session_name, pane_id, pane_pid, pane_tty, pane_dead, pane_dead_status, attached, pane_current_command = fields
        self.session_name = session_name
        self.pane_id = pane_id
        self._pid = int(pane_pid) if pane_pid.isdigit() else self._pid
        self.pane_tty = pane_tty
        self.attached_clients = int(attached or "0")
        self._pane_current_command = pane_current_command

        if pane_dead == "1":
            exit_code = int(pane_dead_status) if pane_dead_status.strip("-").isdigit() else self.exit_code
            final_state = "stopped" if (exit_code or 0) == 0 or self._stop_requested else "failed"
            await self._finalize(final_state, exit_code)
            return

        if self._owns_session:
            self.state = "running"
            return

        if pane_current_command in self._expected_commands:
            self._observed_foreground_codex = True
            self.state = "running"
            return

        if self._completion_reported:
            state = "failed" if self.exit_code not in {None, 0, 130} else "stopped"
            await self._finalize(state, self.exit_code)
            return

        if self._observed_foreground_codex or self._stop_requested:
            await self._finalize("stopped", self.exit_code)
            return

        self.state = "launching"

    async def _finalize(self, state: str, exit_code: int | None) -> None:
        if self._final_event_sent:
            return
        self._final_event_sent = True
        self.state = state
        self.exit_code = exit_code
        await self._stop_pipe()
        await self.emit(
            {
                "type": "session_event",
                "event_type": "failed" if state == "failed" else "stopped",
                "session_id": self.session_id,
                "state": state,
                "exit_code": exit_code,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.pid,
                "source": "managed",
                "repo_path": self.repo_path,
                "git_branch": self.git_branch,
                "meta": self._meta(),
            }
        )

    async def _git_value(self, *args: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                self.cwd,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return None
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            return None
        value = stdout.decode("utf-8", errors="replace").strip()
        return value or None

    async def _tmux(self, *args: str, check: bool = True) -> str | None:
        process = await asyncio.create_subprocess_exec(
            self.tmux_bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            if check:
                message = stderr.decode("utf-8", errors="replace").strip() or f"tmux exited with {process.returncode}"
                raise TmuxSessionError(message)
            return None
        return stdout.decode("utf-8", errors="replace")

    async def _stop_pipe(self) -> None:
        if not self._pipe_started or self.pane_id is None:
            return
        await self._tmux("pipe-pane", "-t", self.pane_id, check=False)
        self._pipe_started = False

    def _meta(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "initial_prompt": self.initial_prompt,
            "tmux_session": self.session_name or self._requested_session_name,
            "tmux_pane": self.pane_id,
            "pane_tty": self.pane_tty,
            "attached_clients": self.attached_clients,
            "attach_command": f"{self.tmux_bin} attach-session -t {self.session_name or self._requested_session_name}",
            "tmux_launch_mode": "detached_session" if self._owns_session else "current_pane",
            "pane_current_command": self._pane_current_command,
            "argv": self.argv,
        }
