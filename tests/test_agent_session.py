import asyncio
from pathlib import Path
import stat
import textwrap

from codex_dashboard.agent.session import AppServerSession


FAKE_APP_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys

    thread_id = "thread-1"
    turn_id = None

    def emit(obj):
        print(json.dumps(obj), flush=True)

    def thread_payload():
        return {
            "id": thread_id,
            "preview": "",
            "ephemeral": False,
            "modelProvider": "openai",
            "createdAt": 0,
            "updatedAt": 0,
            "status": {"type": "idle"},
            "path": "/tmp/fake-thread.jsonl",
            "cwd": ".",
            "cliVersion": "0.0.0",
            "source": "app-server",
            "agentNickname": None,
            "agentRole": None,
            "gitInfo": None,
            "name": None,
            "turns": [],
        }

    for raw in sys.stdin:
        msg = json.loads(raw)
        method = msg.get("method")

        if method == "initialize":
            emit({"id": msg["id"], "result": {"userAgent": "fake", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "linux"}})
            continue

        if method == "initialized":
            continue

        if method == "thread/start":
            emit({
                "id": msg["id"],
                "result": {
                    "thread": thread_payload(),
                    "model": "gpt-5.4",
                    "modelProvider": "openai",
                    "cwd": ".",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": {"type": "workspaceWrite"},
                },
            })
            emit({"method": "thread/started", "params": {"thread": thread_payload()}})
            continue

        if method in {"turn/start", "turn/steer"}:
            turn_id = f"turn-{msg['id']}"
            turn = {"id": turn_id, "items": [], "status": "inProgress", "error": None}
            emit({"id": msg["id"], "result": {"turn": turn}})
            emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
            emit({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id, "item": {"type": "userMessage"}}})
            text = msg["params"]["input"][0]["text"]
            if "approval" in text:
                emit({
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "cmd-1",
                        "command": "ls",
                        "cwd": ".",
                        "reason": "test approval",
                    },
                })
            else:
                emit({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "agent-1", "delta": "hello from app server"}})
                emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "completed", "error": None}}})
            continue

        if method == "turn/interrupt":
            emit({"id": msg["id"], "result": {}})
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": msg["params"]["turnId"], "items": [], "status": "interrupted", "error": None}}})
            continue

        if "id" in msg and method is None:
            decision = "unknown"
            if isinstance(msg.get("result"), dict):
                decision = msg["result"].get("decision", decision)
            emit({"method": "serverRequest/resolved", "params": {"requestId": msg["id"], "threadId": thread_id}})
            emit({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "agent-2", "delta": f"approval {decision}"}})
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "completed", "error": None}}})
    """
)


def make_fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_APP_SERVER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_app_server_session_emits_native_output(tmp_path: Path) -> None:
    async def run_case() -> None:
        events: list[dict] = []
        fake_codex = make_fake_codex(tmp_path)

        async def emit(payload: dict) -> None:
            events.append(payload)

        session = AppServerSession(
            session_id="test-session",
            name="Test",
            cwd=".",
            initial_prompt="say hello",
            codex_bin=str(fake_codex),
            emit=emit,
        )
        await session.start()

        for _ in range(50):
            if any(event.get("text") == "hello from app server" for event in events):
                break
            await asyncio.sleep(0.05)

        await session.stop()
        for _ in range(50):
            if any(event["event_type"] in {"stopped", "failed"} for event in events if event["type"] == "session_event"):
                break
            await asyncio.sleep(0.05)

        assert any(event["event_type"] == "started" for event in events if event["type"] == "session_event")
        assert any(
            event["event_type"] == "output" and "hello from app server" in event.get("text", "")
            for event in events
            if event["type"] == "session_event"
        )

    asyncio.run(run_case())


def test_app_server_session_resolves_approval(tmp_path: Path) -> None:
    async def run_case() -> None:
        events: list[dict] = []
        fake_codex = make_fake_codex(tmp_path)

        async def emit(payload: dict) -> None:
            events.append(payload)

        session = AppServerSession(
            session_id="approval-session",
            name="Approval Test",
            cwd=".",
            initial_prompt="",
            codex_bin=str(fake_codex),
            emit=emit,
        )
        await session.start()
        await session.send_input("needs approval")

        approval_id = None
        for _ in range(50):
            for event in events:
                if event.get("event_type") == "approval_requested":
                    approval_id = event["approval_id"]
                    break
            if approval_id:
                break
            await asyncio.sleep(0.05)

        assert approval_id == "900"
        await session.resolve_approval(approved=True, approval_id=approval_id)

        for _ in range(50):
            if any("approval accept" in event.get("text", "") for event in events if event.get("event_type") == "output"):
                break
            await asyncio.sleep(0.05)

        await session.stop()
        assert any(event.get("event_type") == "approval_resolved" for event in events)
        assert any("approval accept" in event.get("text", "") for event in events if event.get("event_type") == "output")

    asyncio.run(run_case())
