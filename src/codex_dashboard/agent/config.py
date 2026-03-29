from __future__ import annotations

from dataclasses import dataclass
import os
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


def load_config() -> AgentConfig:
    hostname = socket.gethostname()
    agent_id = os.getenv("CODEX_DASHBOARD_AGENT_ID", hostname)
    labels_value = os.getenv("CODEX_DASHBOARD_AGENT_LABELS", "")
    labels = [item.strip() for item in labels_value.split(",") if item.strip()]
    return AgentConfig(
        ws_url=os.getenv("CODEX_DASHBOARD_AGENT_WS_URL", "ws://127.0.0.1:8000/ws/agent"),
        agent_id=agent_id,
        token=os.getenv("CODEX_DASHBOARD_AGENT_TOKEN", os.getenv("CODEX_DASHBOARD_AGENT_SHARED_SECRET", "change-me")),
        display_name=os.getenv("CODEX_DASHBOARD_AGENT_DISPLAY_NAME", hostname),
        labels=labels,
        heartbeat_seconds=int(os.getenv("CODEX_DASHBOARD_AGENT_HEARTBEAT_SECONDS", "15")),
        watch_seconds=int(os.getenv("CODEX_DASHBOARD_AGENT_WATCH_SECONDS", "15")),
        codex_bin=os.getenv("CODEX_DASHBOARD_CODEX_BIN", "codex"),
    )
