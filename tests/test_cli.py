import json
from typing import Any

from codex_dashboard.cli import launch_local_terminal_session, launch_managed_session


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class FakeOpener:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def open(self, request: Any) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8"))
        self.requests.append((request.full_url, payload))
        if request.full_url.endswith("/api/login"):
            return FakeResponse({"ok": True})
        return FakeResponse({"ok": True, "session_id": "session-123"})


def test_launch_managed_session_logs_in_then_launches_terminal_mode(monkeypatch) -> None:
    opener = FakeOpener()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args, **kwargs: opener)

    result = launch_managed_session(
        server_url="http://127.0.0.1:8899/",
        username="admin",
        password="secret",
        agent_id="workstation-omarchy",
        cwd="/repo",
        name="CLI Session",
        initial_prompt="hello",
        launch_mode="terminal",
        argv=["resume", "--last"],
    )

    assert result["session_id"] == "session-123"
    assert opener.requests == [
        (
            "http://127.0.0.1:8899/api/login",
            {"username": "admin", "password": "secret"},
        ),
        (
            "http://127.0.0.1:8899/api/agents/workstation-omarchy/sessions",
            {
                "name": "CLI Session",
                "cwd": "/repo",
                "initial_prompt": "hello",
                "launch_mode": "terminal",
                "argv": ["resume", "--last"],
            },
        ),
    ]


def test_launch_local_terminal_session_uses_unix_socket(monkeypatch) -> None:
    received: dict[str, Any] = {}

    class FakeSocket:
        def connect(self, path: str) -> None:
            received["path"] = path

        def sendall(self, data: bytes) -> None:
            received["payload"] = json.loads(data.decode("utf-8").strip())

        def recv(self, _size: int) -> bytes:
            if received.get("closed"):
                return b""
            received["closed"] = True
            return (
                json.dumps(
                    {
                        "ok": True,
                        "session_id": "local-session",
                        "meta": {
                            "tmux_session": "codexdash-local",
                            "attach_command": "tmux attach-session -t codexdash-local",
                        },
                    }
                ).encode("utf-8")
                + b"\n"
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: FakeSocket())
    result = launch_local_terminal_session(
        socket_path="/tmp/agent.sock",
        cwd="/repo",
        name="TTY Session",
        initial_prompt="",
        argv=["resume", "--last"],
    )

    assert result["session_id"] == "local-session"
    assert received == {
        "path": "/tmp/agent.sock",
        "payload": {
            "type": "launch_terminal",
            "cwd": "/repo",
            "name": "TTY Session",
            "initial_prompt": "",
            "argv": ["resume", "--last"],
        },
        "closed": True,
    }
