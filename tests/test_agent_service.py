import asyncio

from codex_dashboard.agent.config import AgentConfig
from codex_dashboard.agent.service import DashboardAgent


class FakeSession:
    def __init__(self, *, pid: int | None = None, ttys: set[str] | None = None) -> None:
        self.pid = pid
        self._ttys = ttys or set()
        self.sync_calls = 0

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
            pane_tty: str | None,
            attached_clients: int,
            pane_current_command: str | None,
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
                    "pane_tty": pane_tty,
                    "attached_clients": attached_clients,
                    "pane_current_command": pane_current_command,
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
                        "pane_tty": "/dev/pts/7",
                        "attached_clients": 1,
                        "pane_current_command": "uv",
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
            "pane_tty": "/dev/pts/7",
            "attached_clients": 1,
            "pane_current_command": "uv",
            "existing_pane_id": "%7",
        }
    ]
    assert "session-1" in agent.sessions
    assert agent.sessions["session-1"].sync_calls == 1
