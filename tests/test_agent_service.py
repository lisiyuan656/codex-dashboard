from types import SimpleNamespace

import pytest

from codex_dashboard.agent.config import AgentConfig
from codex_dashboard.agent.service import DashboardAgent


def test_discover_unmanaged_codex_only_matches_real_codex_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(
        ws_url="ws://127.0.0.1:8000/ws/agent",
        agent_id="agent-1",
        token="token",
        display_name="Agent",
        labels=[],
        heartbeat_seconds=15,
        watch_seconds=15,
        codex_bin="codex",
    )
    agent = DashboardAgent(config)
    agent.sessions = {
        "managed": SimpleNamespace(process=SimpleNamespace(pid=200)),
    }

    sample_ps = "\n".join(
        [
            "100 python uv run codex-dashboard-agent",
            "101 python uv run uvicorn codex_dashboard.main:app --port 8899",
            "200 codex codex app-server",
            "201 codex codex --no-alt-screen",
            "202 codex codex resume --last",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: sample_ps)

    discovered = list(agent._discover_unmanaged_codex())

    assert [item["pid"] for item in discovered] == [201, 202]
