from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os


@dataclass(slots=True)
class Settings:
    database_url: str
    secret_key: str
    session_cookie_name: str
    admin_username: str
    admin_password: str
    agent_shared_secret: str
    offline_agent_seconds: int
    stale_session_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("CODEX_DASHBOARD_DATABASE_URL", "sqlite:///./codex_dashboard.db"),
        secret_key=os.getenv("CODEX_DASHBOARD_SECRET_KEY", "change-me"),
        session_cookie_name=os.getenv("CODEX_DASHBOARD_SESSION_COOKIE", "codex_dashboard_session"),
        admin_username=os.getenv("CODEX_DASHBOARD_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("CODEX_DASHBOARD_ADMIN_PASSWORD", "change-me"),
        agent_shared_secret=os.getenv("CODEX_DASHBOARD_AGENT_SHARED_SECRET", "change-me"),
        offline_agent_seconds=int(os.getenv("CODEX_DASHBOARD_OFFLINE_AGENT_SECONDS", "60")),
        stale_session_seconds=int(os.getenv("CODEX_DASHBOARD_STALE_SESSION_SECONDS", "45")),
    )
