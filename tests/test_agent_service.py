import asyncio

from codex_dashboard.agent.config import AgentConfig
from codex_dashboard.agent.service import DashboardAgent
from codex_dashboard.agent.session import CliHookSession


class FakeSession:
    def __init__(self, *, pid: int | None = None, ttys: set[str] | None = None) -> None:
        self.pid = pid
        self._ttys = ttys or set()
        self.sync_calls = 0
        self.transport = "tmux_terminal"

    def discovery_pids(self) -> set[int]:
        return {self.pid} if self.pid is not None else set()

    def discovery_ttys(self) -> set[str]:
        return set(self._ttys)

    @property
    def is_alive(self) -> bool:
        return True

    async def sync(self) -> None:
        self.sync_calls += 1


def test_discover_unmanaged_codex_ignores_managed_tmux_tty(monkeypatch) -> None:
    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
        tmux_bin="tmux",
        socket_path="/tmp/codex-dashboard-agent.sock",
        spool_dir="/tmp/codex-dashboard-agent",
    )
    agent = DashboardAgent(config)
    agent.sessions = {
        "app-server": FakeSession(pid=200),
        "terminal": FakeSession(ttys={"pts/7", "/dev/pts/7"}),
    }

    sample_ps = "\n".join(
        [
            "100 python ? uv run codex-dashboard-agent",
            "101 python ? uv run uvicorn codex_dashboard.main:app --port 8899",
            "200 codex ? codex app-server",
            "201 codex pts/7 codex --no-alt-screen",
            "202 codex pts/8 codex resume --last",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: sample_ps)

    discovered = list(agent._discover_unmanaged_codex())

    assert [item["pid"] for item in discovered] == [202]


def test_restore_sessions_rehydrates_tmux_runtime(monkeypatch) -> None:
    restored: list[dict[str, object]] = []

    class FakeTmuxTerminalSession:
        transport = "tmux_terminal"

        def __init__(
            self,
            *,
            session_id: str,
            name: str,
            cwd: str,
            codex_bin: str,
            tmux_bin: str,
            emit,
            spool_dir: str,
            argv: list[str] | None = None,
            initial_prompt: str = "",
            existing_pane_id: str | None = None,
        ) -> None:
            del codex_bin, tmux_bin, emit, spool_dir
            self.session_id = session_id
            self.name = name
            self.cwd = cwd
            self.argv = list(argv or [])
            self.initial_prompt = initial_prompt
            self.existing_pane_id = existing_pane_id
            self.command = "codex --no-alt-screen"
            self.state = "launching"
            self.pid = None
            self.sync_calls = 0
            self.alive = True

        @property
        def is_alive(self) -> bool:
            return self.alive

        async def restore(
            self,
            *,
            session_name: str | None,
            launch_mode: str,
            repo_path: str | None,
            git_branch: str | None,
            pid: int | None,
            state: str | None,
            pane_tty: str | None,
            attached_clients: int,
            pane_current_command: str | None,
            codex_session_id: str | None,
            status_detail: str | None,
            last_hook_event: str | None,
        ) -> None:
            self.state = "running"
            self.pid = pid
            restored.append(
                {
                    "session_id": self.session_id,
                    "session_name": session_name,
                    "launch_mode": launch_mode,
                    "repo_path": repo_path,
                    "git_branch": git_branch,
                    "pid": pid,
                    "state": state,
                    "pane_tty": pane_tty,
                    "attached_clients": attached_clients,
                    "pane_current_command": pane_current_command,
                    "codex_session_id": codex_session_id,
                    "status_detail": status_detail,
                    "last_hook_event": last_hook_event,
                    "existing_pane_id": self.existing_pane_id,
                }
            )

        async def sync(self) -> None:
            self.sync_calls += 1

        def discovery_pids(self) -> set[int]:
            return {self.pid} if self.pid is not None else set()

        def discovery_ttys(self) -> set[str]:
            return set()

    monkeypatch.setattr("codex_dashboard.agent.service.TmuxTerminalSession", FakeTmuxTerminalSession)

    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
        tmux_bin="tmux",
        socket_path="/tmp/codex-dashboard-agent.sock",
        spool_dir="/tmp/codex-dashboard-agent",
    )
    agent = DashboardAgent(config)

    asyncio.run(
        agent._handle_action(
            {
                "type": "restore_sessions",
                "sessions": [
                    {
                        "session_id": "session-1",
                        "name": "Recovered Session",
                        "cwd": "/repo",
                        "argv": ["resume", "--last"],
                        "initial_prompt": "",
                        "tmux_pane": "%7",
                        "tmux_session": "work",
                        "tmux_launch_mode": "current_pane",
                        "repo_path": "/repo",
                        "git_branch": "main",
                        "pid": 4321,
                        "state": "idle",
                        "pane_tty": "/dev/pts/7",
                        "attached_clients": 1,
                        "pane_current_command": "uv",
                        "codex_session_id": "codex-session",
                        "status_detail": "Waiting for input",
                        "last_hook_event": "Stop",
                    }
                ],
            }
        )
    )

    assert restored == [
        {
            "session_id": "session-1",
            "session_name": "work",
            "launch_mode": "current_pane",
            "repo_path": "/repo",
            "git_branch": "main",
            "pid": 4321,
            "state": "idle",
            "pane_tty": "/dev/pts/7",
            "attached_clients": 1,
            "pane_current_command": "uv",
            "codex_session_id": "codex-session",
            "status_detail": "Waiting for input",
            "last_hook_event": "Stop",
            "existing_pane_id": "%7",
        }
    ]
    assert "session-1" in agent.sessions
    assert agent.sessions["session-1"].sync_calls == 1


def test_handle_hook_event_updates_managed_tmux_runtime(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class FakeTmuxTerminalSession:
        transport = "tmux_terminal"

        def __init__(self) -> None:
            self.session_id = "managed-session"
            self.name = "Managed Session"
            self.cwd = "/repo"
            self.command = "codex --no-alt-screen"
            self.state = "launching"
            self.pid = 4321

        @property
        def is_alive(self) -> bool:
            return True

        async def apply_hook_event(self, payload: dict[str, object]) -> None:
            self.state = str(payload["state"])
            events.append(payload)

        async def sync(self) -> None:
            return None

        def discovery_pids(self) -> set[int]:
            return {self.pid}

        def discovery_ttys(self) -> set[str]:
            return {"pts/7"}

    monkeypatch.setattr("codex_dashboard.agent.service.TmuxTerminalSession", FakeTmuxTerminalSession)

    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
        tmux_bin="tmux",
        socket_path="/tmp/codex-dashboard-agent.sock",
        spool_dir="/tmp/codex-dashboard-agent",
    )
    agent = DashboardAgent(config)
    agent.sessions = {"managed-session": FakeTmuxTerminalSession()}

    result = asyncio.run(
        agent._handle_local_request(
            {
                "type": "hook_event",
                "hook": {
                    "dashboard_session_id": "managed-session",
                    "hook_event_name": "Stop",
                    "state": "idle",
                    "status_detail": "Waiting for input",
                    "pid": 4321,
                    "tty": "pts/7",
                },
            }
        )
    )

    assert result["ok"] is True
    assert agent.sessions["managed-session"].state == "idle"
    assert events == [
        {
            "dashboard_session_id": "managed-session",
            "hook_event_name": "Stop",
            "state": "idle",
            "status_detail": "Waiting for input",
            "pid": 4321,
            "tty": "pts/7",
        }
    ]


def test_handle_hook_event_creates_unmanaged_cli_session() -> None:
    events: list[dict[str, object]] = []

    async def fake_emit(payload: dict[str, object]) -> None:
        events.append(payload)

    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
        tmux_bin="tmux",
        socket_path="/tmp/codex-dashboard-agent.sock",
        spool_dir="/tmp/codex-dashboard-agent",
    )
    agent = DashboardAgent(config)
    agent.emit = fake_emit  # type: ignore[method-assign]

    result = asyncio.run(
        agent._handle_local_request(
            {
                "type": "hook_event",
                "hook": {
                    "hook_event_name": "SessionStart",
                    "codex_session_id": "codex-session",
                    "state": "idle",
                    "status_detail": "Waiting for input",
                    "pid": 4321,
                    "tty": "pts/8",
                    "cwd": "/repo",
                    "command": "codex --no-alt-screen",
                    "name": "CLI Codex 4321",
                    "transport": "cli_terminal",
                    "tmux_pane": None,
                },
            }
        )
    )

    assert result["ok"] is True
    assert "unmanaged-agent-1-4321" in agent.sessions
    session = agent.sessions["unmanaged-agent-1-4321"]
    assert isinstance(session, CliHookSession)
    assert session.state == "idle"
    assert events[-1]["event_type"] == "hook_status"


def test_reconcile_hook_sessions_stops_missing_cli_sessions() -> None:
    events: list[dict[str, object]] = []

    async def fake_emit(payload: dict[str, object]) -> None:
        events.append(payload)

    session = CliHookSession(
        session_id="unmanaged-agent-1-4321",
        name="CLI Codex 4321",
        cwd="/repo",
        command="codex --no-alt-screen",
        transport="cli_terminal",
        pid=4321,
        tty="pts/8",
        tmux_pane=None,
        codex_session_id="codex-session",
        state="idle",
        status_detail="Waiting for input",
        emit=fake_emit,
    )

    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
        tmux_bin="tmux",
        socket_path="/tmp/codex-dashboard-agent.sock",
        spool_dir="/tmp/codex-dashboard-agent",
    )
    agent = DashboardAgent(config)
    agent.sessions = {session.session_id: session}

    asyncio.run(agent._reconcile_hook_sessions([]))

    assert agent.sessions == {}
    assert events[-1]["event_type"] == "stopped"
