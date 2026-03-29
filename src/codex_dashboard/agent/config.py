from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import socket


@dataclass(slots=True)
class AgentConfig:
    ws_url: str
    agent_id: str
    token: str
    display_name: str
    labels: list[str]
    heartbeat_seconds: int
    watch_seconds: int
    codex_bin: str
    tmux_bin: str
    socket_path: str
    spool_dir: str


def load_config() -> AgentConfig:
    hostname = socket.gethostname()
    agent_id = os.getenv("CODEX_DASHBOARD_AGENT_ID", hostname)
    labels_value = os.getenv("CODEX_DASHBOARD_AGENT_LABELS", "")
    labels = [item.strip() for item in labels_value.split(",") if item.strip()]
    runtime_dir = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp"))
    state_dir = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return AgentConfig(
        ws_url=os.getenv("CODEX_DASHBOARD_AGENT_WS_URL", "ws://127.0.0.1:8000/ws/agent"),
        agent_id=agent_id,
        token=os.getenv("CODEX_DASHBOARD_AGENT_TOKEN", os.getenv("CODEX_DASHBOARD_AGENT_SHARED_SECRET", "change-me")),
        display_name=os.getenv("CODEX_DASHBOARD_AGENT_DISPLAY_NAME", hostname),
        labels=labels,
        heartbeat_seconds=int(os.getenv("CODEX_DASHBOARD_AGENT_HEARTBEAT_SECONDS", "15")),
        watch_seconds=int(os.getenv("CODEX_DASHBOARD_AGENT_WATCH_SECONDS", "15")),
        codex_bin=os.getenv("CODEX_DASHBOARD_CODEX_BIN", "codex"),
        tmux_bin=os.getenv("CODEX_DASHBOARD_TMUX_BIN", "tmux"),
        socket_path=os.getenv("CODEX_DASHBOARD_AGENT_SOCKET_PATH", str(runtime_dir / "codex-dashboard-agent.sock")),
        spool_dir=os.getenv("CODEX_DASHBOARD_AGENT_SPOOL_DIR", str(state_dir / "codex-dashboard-agent")),
    )
