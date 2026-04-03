from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

from codex_dashboard.config import Settings
from codex_dashboard.models import Agent, Base, Session as ManagedSession, SessionLock, User, utcnow
from codex_dashboard.security import hash_password
from codex_dashboard.store import (
    acquire_lock,
    get_agent_sessions,
    get_recoverable_agent_sessions,
    ingest_session_event,
    list_agents_with_sessions,
    refresh_staleness,
    release_lock,
)


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


def test_refresh_staleness_stops_stale_detected_unmanaged_sessions() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-2",
        display_name="Agent Two",
        hostname="host",
        status="online",
        last_seen_at=(now - timedelta(seconds=5)).replace(tzinfo=None),
        labels=[],
        meta={},
    )
    session = ManagedSession(
        id="session-2",
        agent_id="agent-2",
        source="unmanaged",
        name="Unmanaged",
        state="detected",
        command="codex",
        cwd=".",
        last_heartbeat_at=(now - timedelta(seconds=120)).replace(tzinfo=None),
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

    db.refresh(session)
    assert session.state == "stopped"
    assert session.ended_at is not None


def test_dashboard_lists_hide_stopped_sessions() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-3",
        display_name="Agent Three",
        hostname="host",
        status="online",
        last_seen_at=now,
        labels=[],
        meta={},
    )
    sessions = [
        ManagedSession(
            id="managed-running",
            agent_id="agent-3",
            source="managed",
            name="Managed Running",
            state="running",
            command="codex --no-alt-screen",
            cwd=".",
            started_at=now - timedelta(minutes=1),
            last_heartbeat_at=now,
            meta={},
        ),
        ManagedSession(
            id="managed-stopped",
            agent_id="agent-3",
            source="managed",
            name="Managed Stopped",
            state="stopped",
            command="codex --no-alt-screen",
            cwd=".",
            started_at=now - timedelta(minutes=2),
            ended_at=now - timedelta(minutes=1),
            last_heartbeat_at=now - timedelta(minutes=1),
            meta={},
        ),
        ManagedSession(
            id="unmanaged-running",
            agent_id="agent-3",
            source="unmanaged",
            name="Unmanaged Running",
            state="detected",
            command="codex resume --last",
            cwd=".",
            started_at=now - timedelta(minutes=3),
            last_heartbeat_at=now,
            meta={},
        ),
        ManagedSession(
            id="unmanaged-stopped",
            agent_id="agent-3",
            source="unmanaged",
            name="Unmanaged Stopped",
            state="stopped",
            command="codex resume --last",
            cwd=".",
            started_at=now - timedelta(minutes=4),
            ended_at=now - timedelta(minutes=3),
            last_heartbeat_at=now - timedelta(minutes=3),
            meta={},
        ),
    ]
    db.add(agent)
    db.add_all(sessions)
    db.commit()

    agent_sessions = get_agent_sessions(db, "agent-3")
    assert [session.id for session in agent_sessions] == [
        "managed-running",
        "unmanaged-running",
    ]

    overview = list_agents_with_sessions(db)
    assert len(overview) == 1
    assert [session.id for session in overview[0]["sessions"]] == [
        "managed-running",
        "unmanaged-running",
    ]


def test_ingest_session_event_preserves_running_for_current_pane_tmux_sessions() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-4",
        display_name="Agent Four",
        hostname="host",
        status="online",
        last_seen_at=now,
        labels=[],
        meta={},
    )
    session = ManagedSession(
        id="session-4",
        agent_id="agent-4",
        source="managed",
        name="Current Pane",
        state="running",
        command="codex --no-alt-screen",
        cwd=".",
        pid=4321,
        started_at=now - timedelta(minutes=1),
        last_heartbeat_at=now,
        meta={"transport": "tmux_terminal", "tmux_launch_mode": "current_pane"},
    )
    db.add(agent)
    db.add(session)
    db.commit()

    ingest_session_event(
        db,
        "agent-4",
        {
            "session_id": "session-4",
            "event_type": "output",
            "state": "launching",
            "pid": 4321,
            "text": "hello",
            "meta": {
                "transport": "tmux_terminal",
                "tmux_launch_mode": "current_pane",
                "pane_current_command": "uv",
            },
        },
    )
    ingest_session_event(
        db,
        "agent-4",
        {
            "session_id": "session-4",
            "event_type": "heartbeat",
            "state": "launching",
            "pid": 4321,
            "meta": {
                "transport": "tmux_terminal",
                "tmux_launch_mode": "current_pane",
                "pane_current_command": "uv",
            },
        },
    )

    db.refresh(session)
    assert session.state == "running"
    assert "hello" in (session.last_output_excerpt or "")


def test_ingest_session_event_strips_tmux_escape_sequences_from_excerpt() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-4b",
        display_name="Agent Four B",
        hostname="host",
        status="online",
        last_seen_at=now,
        labels=[],
        meta={},
    )
    session = ManagedSession(
        id="session-4b",
        agent_id="agent-4b",
        source="managed",
        name="Tmux Session",
        state="running",
        command="codex --no-alt-screen",
        cwd=".",
        pid=4321,
        started_at=now - timedelta(minutes=1),
        last_heartbeat_at=now,
        meta={"transport": "tmux_terminal", "tmux_launch_mode": "current_pane"},
    )
    db.add(agent)
    db.add(session)
    db.commit()

    ingest_session_event(
        db,
        "agent-4b",
        {
            "session_id": "session-4b",
            "event_type": "output",
            "state": "running",
            "pid": 4321,
            "text": "\u001b]10;?\u001b\\\u001b[1;2H\u001b[0mToken usage: total=12\r\nrun codex resume abc-123\u001b[39m",
            "meta": {
                "transport": "tmux_terminal",
                "tmux_launch_mode": "current_pane",
                "pane_current_command": "uv",
            },
        },
    )

    db.refresh(session)
    assert session.last_output_excerpt == "Token usage: total=12\nrun codex resume abc-123"


def test_get_recoverable_agent_sessions_returns_only_live_tmux_sessions() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-5",
        display_name="Agent Five",
        hostname="host",
        status="online",
        last_seen_at=now,
        labels=[],
        meta={},
    )
    db.add(agent)
    db.add_all(
        [
            ManagedSession(
                id="recover-tmux",
                agent_id="agent-5",
                source="managed",
                name="Recover Tmux",
                state="running",
                command="codex --no-alt-screen",
                cwd="/repo",
                pid=111,
                started_at=now,
                last_heartbeat_at=now,
                meta={
                    "transport": "tmux_terminal",
                    "tmux_launch_mode": "current_pane",
                    "tmux_pane": "%7",
                    "tmux_session": "work",
                    "argv": ["resume", "--last"],
                    "codex_session_id": "codex-session",
                    "status_detail": "Waiting for input",
                    "last_hook_event": "Stop",
                },
            ),
            ManagedSession(
                id="skip-app-server",
                agent_id="agent-5",
                source="managed",
                name="App Server",
                state="running",
                command="codex app-server",
                cwd="/repo",
                started_at=now,
                last_heartbeat_at=now,
                meta={"transport": "app_server"},
            ),
            ManagedSession(
                id="skip-stopped",
                agent_id="agent-5",
                source="managed",
                name="Stopped Tmux",
                state="stopped",
                command="codex --no-alt-screen",
                cwd="/repo",
                started_at=now,
                last_heartbeat_at=now,
                meta={"transport": "tmux_terminal", "tmux_pane": "%8"},
            ),
        ]
    )
    db.commit()

    recoverable = get_recoverable_agent_sessions(db, "agent-5")
    assert recoverable == [
        {
            "session_id": "recover-tmux",
            "name": "Recover Tmux",
            "cwd": "/repo",
            "argv": ["resume", "--last"],
            "initial_prompt": "",
            "tmux_pane": "%7",
            "tmux_session": "work",
            "tmux_launch_mode": "current_pane",
            "repo_path": None,
            "git_branch": None,
            "pid": 111,
            "state": "running",
            "pane_tty": None,
            "attached_clients": 0,
            "pane_current_command": None,
            "codex_session_id": "codex-session",
            "status_detail": "Waiting for input",
            "last_hook_event": "Stop",
        }
    ]


def test_ingest_hook_status_updates_unmanaged_cli_session_state() -> None:
    db = make_db()
    now = utcnow()
    agent = Agent(
        id="agent-6",
        display_name="Agent Six",
        hostname="host",
        status="online",
        last_seen_at=now,
        labels=[],
        meta={},
    )
    session = ManagedSession(
        id="unmanaged-agent-6-4321",
        agent_id="agent-6",
        source="unmanaged",
        name="CLI Codex 4321",
        state="detected",
        command="codex --no-alt-screen",
        cwd="/repo",
        pid=4321,
        started_at=now - timedelta(minutes=1),
        last_heartbeat_at=now,
        meta={"transport": "cli_terminal"},
    )
    db.add(agent)
    db.add(session)
    db.commit()

    ingest_session_event(
        db,
        "agent-6",
        {
            "session_id": "unmanaged-agent-6-4321",
            "event_type": "hook_status",
            "state": "idle",
            "pid": 4321,
            "source": "unmanaged",
            "meta": {
                "transport": "cli_terminal",
                "codex_session_id": "codex-session",
                "status_detail": "Waiting for input",
                "last_hook_event": "Stop",
                "tty": "pts/8",
            },
        },
    )

    db.refresh(session)
    assert session.state == "idle"
    assert session.meta["codex_session_id"] == "codex-session"
    assert session.meta["status_detail"] == "Waiting for input"
