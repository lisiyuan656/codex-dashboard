from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import Any


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AppServerProtocolError(RuntimeError):
    pass


class AppServerSession:
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
        self.session_id = session_id
        self.name = name
        self.cwd = cwd
        self.initial_prompt = initial_prompt
        self.codex_bin = codex_bin
        self.emit = emit
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self.state = "launching"
        self._request_id = 1
        self._write_lock = asyncio.Lock()
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._pending_server_requests: dict[str, dict[str, Any]] = {}
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None

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
                "command": f"{self.codex_bin} app-server",
                "cwd": self.cwd,
                "pid": self.process.pid,
                "source": "managed",
                "repo_path": self.cwd,
                "git_branch": ((thread.get("gitInfo") or {}).get("branch") if thread.get("gitInfo") else None),
                "meta": {
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
        await self.emit(
            {
                "type": "session_event",
                "event_type": "heartbeat",
                "session_id": self.session_id,
                "state": self.state,
                "meta": {
                    "thread_id": self.thread_id,
                    "turn_id": self.active_turn_id,
                },
            }
        )

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
                "meta": {
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
            await self._emit_heartbeat()
            return

        if method == "turn/started":
            turn = params["turn"]
            self.active_turn_id = turn["id"]
            self.state = self._map_turn_status(turn.get("status"))
            await self._emit_heartbeat()
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
                    }
                )
            await self._emit_heartbeat()
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
                        "thread_id": params.get("threadId", self.thread_id),
                        "turn_id": params.get("turnId", self.active_turn_id),
                        "last_item_type": (params.get("item") or {}).get("type"),
                    },
                }
            )

    async def _emit_heartbeat(self) -> None:
        await self.emit(
            {
                "type": "session_event",
                "event_type": "heartbeat",
                "session_id": self.session_id,
                "state": self.state,
                "meta": {
                    "thread_id": self.thread_id,
                    "turn_id": self.active_turn_id,
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
