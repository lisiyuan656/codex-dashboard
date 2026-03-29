from __future__ import annotations

import asyncio
import os
import pty
import re
import shlex
import signal
from typing import Awaitable, Callable


EventCallback = Callable[[dict], Awaitable[None]]
APPROVAL_RE = re.compile(r"(approve|approval|allow|deny|permission)", re.IGNORECASE)


class ManagedPtySession:
    def __init__(self, *, session_id: str, name: str, command: str, cwd: str, emit: EventCallback) -> None:
        self.session_id = session_id
        self.name = name
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self.process: asyncio.subprocess.Process | None = None
        self.master_fd: int | None = None
        self._read_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._approval_open = False

    async def start(self) -> None:
        argv = shlex.split(self.command)
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        os.set_blocking(master_fd, False)
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._read_task = asyncio.create_task(self._read_loop())
        self._wait_task = asyncio.create_task(self._wait_loop())
        await self.emit(
            {
                "type": "session_event",
                "event_type": "started",
                "session_id": self.session_id,
                "name": self.name,
                "command": self.command,
                "cwd": self.cwd,
                "pid": self.process.pid,
                "source": "managed",
            }
        )

    async def _read_loop(self) -> None:
        assert self.master_fd is not None
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except BlockingIOError:
                if self.process is not None and self.process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
                continue
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            await self.emit(
                {
                    "type": "session_event",
                    "event_type": "output",
                    "session_id": self.session_id,
                    "text": text,
                    "state": "running",
                }
            )
            if APPROVAL_RE.search(text) and not self._approval_open:
                self._approval_open = True
                await self.emit(
                    {
                        "type": "session_event",
                        "event_type": "approval_requested",
                        "session_id": self.session_id,
                        "prompt": text[-500:],
                    }
                )

    async def _wait_loop(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        if self._read_task is not None:
            self._read_task.cancel()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        event_type = "stopped" if code == 0 else "failed"
        await self.emit(
            {
                "type": "session_event",
                "event_type": event_type,
                "session_id": self.session_id,
                "exit_code": code,
                "state": "finished" if code == 0 else "failed",
            }
        )

    async def send_input(self, text: str) -> None:
        if self.master_fd is None:
            return
        os.write(self.master_fd, text.encode("utf-8"))

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        self.process.send_signal(signal.SIGTERM)

    async def resolve_approval(self, *, approved: bool, approval_id: str) -> None:
        await self.send_input("y\n" if approved else "n\n")
        self._approval_open = False
        await self.emit(
            {
                "type": "session_event",
                "event_type": "approval_resolved",
                "session_id": self.session_id,
                "approval_id": approval_id,
                "status": "approved" if approved else "denied",
            }
        )
