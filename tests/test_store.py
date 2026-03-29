from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

from codex_dashboard.config import Settings
from codex_dashboard.models import Agent, Base, Session as ManagedSession, SessionLock, User, utcnow
from codex_dashboard.security import hash_password
from codex_dashboard.store import acquire_lock, refresh_staleness, release_lock


def make_db() -> OrmSession:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return OrmSession(bind=engine)


def test_session_lock_lifecycle() -> None:
    db = make_db()
    user = User(username="admin", password_hash=hash_password("pw"))
    other = User(username="other", password_hash=hash_password("pw"))
    db.add_all([user, other])
    db.commit()
    db.refresh(user)
    db.refresh(other)

    ok, message = acquire_lock(db, "abc", user.id)
    assert ok
    assert "Lock" in message
    lock = db.get(SessionLock, "abc")
    assert lock is not None
    assert lock.user_id == user.id

    ok, _ = acquire_lock(db, "abc", other.id)
    assert not ok

    ok, _ = release_lock(db, "abc", other.id)
    assert not ok

    ok, _ = release_lock(db, "abc", user.id)
    assert ok
    assert db.get(SessionLock, "abc") is None


def test_refresh_staleness_handles_sqlite_naive_datetimes() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-1",
        display_name="Agent One",
        hostname="host",
        status="online",
        last_seen_at=(now - timedelta(seconds=5)).replace(tzinfo=None),
        labels=[],
        meta={},
    )
    session = ManagedSession(
        id="session-1",
        agent_id="agent-1",
        source="managed",
        name="Managed",
        state="running",
        command="codex app-server",
        cwd=".",
        last_heartbeat_at=(now - timedelta(seconds=5)).replace(tzinfo=None),
        meta={},
    )
    db.add(agent)
    db.add(session)
    db.commit()

    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="secret",
        session_cookie_name="cookie",
        admin_username="admin",
        admin_password="password",
        agent_shared_secret="token",
        offline_agent_seconds=60,
        stale_session_seconds=45,
    )

    refresh_staleness(db, settings)

    db.refresh(agent)
    db.refresh(session)
    assert agent.status == "online"
    assert session.state == "running"
