from codex_dashboard.agent.config import AgentConfig
from codex_dashboard.agent.service import DashboardAgent


class FakeSession:
    def __init__(self, *, pid: int | None = None, ttys: set[str] | None = None) -> None:
        self.pid = pid
        self._ttys = ttys or set()

    def discovery_pids(self) -> set[int]:
        return {self.pid} if self.pid is not None else set()

    def discovery_ttys(self) -> set[str]:
        return set(self._ttys)


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
