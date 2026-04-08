from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import codex_dashboard.db as db_module
import codex_dashboard.main as main_module
from codex_dashboard.hub import ConnectionHub
from codex_dashboard.models import Agent, Base, Session as ManagedSession, utcnow
from codex_dashboard.store import ingest_session_event


def make_app(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dashboard.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_local)
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "SessionLocal", session_local)
    monkeypatch.setattr(main_module, "hub", ConnectionHub())

    Base.metadata.create_all(engine)
    return main_module.create_app(), session_local


def add_session(session_local, *, session_id: str = "session-1") -> None:
    now = utcnow()
    with session_local() as db:
        db.add(
            Agent(
                id="agent-1",
                display_name="Agent One",
                hostname="host",
                status="online",
                last_seen_at=now,
                labels=[],
                meta={},
            )
        )
        db.add(
            ManagedSession(
                id=session_id,
                agent_id="agent-1",
                source="managed",
                name="Managed Session",
                state="running",
                command="codex --no-alt-screen",
                cwd="/repo",
                started_at=now,
                last_heartbeat_at=now,
                meta={"transport": "tmux_terminal"},
            )
        )
        db.commit()


def login(client: TestClient) -> None:
    response = client.post(
        "/api/login",
        json={
            "username": main_module.settings.admin_username,
            "password": main_module.settings.admin_password,
        },
    )
    assert response.status_code == 200


def test_session_socket_history_batch_uses_compact_timeline_items(tmp_path, monkeypatch) -> None:
    app, session_local = make_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        add_session(session_local)
        with session_local() as db:
            ingest_session_event(
                db,
                "agent-1",
                {
                    "session_id": "session-1",
                    "event_type": "heartbeat",
                    "state": "running",
                    "pid": 4321,
                    "repo_path": "/repo",
                    "git_branch": "main",
                    "meta": {
                        "transport": "tmux_terminal",
                        "attached_clients": 0,
                        "pane_current_command": "codex",
                        "status_detail": "",
                        "last_hook_event": None,
                        "codex_session_id": "codex-session",
                    },
                },
            )
            ingest_session_event(
                db,
                "agent-1",
                {
                    "session_id": "session-1",
                    "event_type": "heartbeat",
                    "state": "running",
                    "pid": 4321,
                    "repo_path": "/repo",
                    "git_branch": "main",
                    "meta": {
                        "transport": "tmux_terminal",
                        "attached_clients": 0,
                        "pane_current_command": "codex",
                        "status_detail": "",
                        "last_hook_event": None,
                        "codex_session_id": "codex-session",
                    },
                },
            )
            ingest_session_event(
                db,
                "agent-1",
                {
                    "session_id": "session-1",
                    "event_type": "output",
                    "state": "running",
                    "pid": 4321,
                    "text": "working",
                    "meta": {"transport": "tmux_terminal"},
                },
            )
            ingest_session_event(
                db,
                "agent-1",
                {
                    "session_id": "session-1",
                    "event_type": "stopped",
                    "state": "finished",
                    "exit_code": 0,
                    "meta": {"transport": "tmux_terminal"},
                },
            )

        login(client)
        page = client.get("/sessions/session-1")
        assert page.status_code == 200
        assert "Timeline" in page.text
        assert "Last heartbeat" in page.text
        with client.websocket_connect("/ws/sessions/session-1") as websocket:
            payload = websocket.receive_json()

        assert payload["type"] == "history_batch"
        assert [item["event_type"] for item in payload["items"]] == ["stopped", "heartbeat"]
        assert payload["items"][1]["kind"] == "heartbeat_rollup"
        assert payload["items"][1]["count"] == 2


def test_agent_session_event_broadcast_includes_timeline_item(tmp_path, monkeypatch) -> None:
    app, session_local = make_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        add_session(session_local)
        login(client)

        with client.websocket_connect("/ws/sessions/session-1") as viewer_socket:
            initial = viewer_socket.receive_json()
            assert initial["type"] == "history_batch"

            with client.websocket_connect(
                f"/ws/agent?agent_id=agent-1&token={main_module.settings.agent_shared_secret}"
            ) as agent_socket:
                agent_socket.send_json(
                    {
                        "type": "session_event",
                        "session_id": "session-1",
                        "event_type": "heartbeat",
                        "state": "running",
                        "pid": 4321,
                        "repo_path": "/repo",
                        "git_branch": "main",
                        "meta": {
                            "transport": "tmux_terminal",
                            "attached_clients": 0,
                            "pane_current_command": "codex",
                            "status_detail": "",
                            "last_hook_event": None,
                            "codex_session_id": "codex-session",
                        },
                    }
                )
                payload = viewer_socket.receive_json()

        assert payload["type"] == "session_event"
        assert payload["event_type"] == "heartbeat"
        assert payload["timeline_item"]["kind"] == "heartbeat_rollup"
        assert payload["timeline_item"]["summary"] == "running"
        assert payload["timeline_item"]["group_key"]
