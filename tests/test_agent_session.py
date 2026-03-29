import asyncio

from codex_dashboard.agent.session import ManagedPtySession


def test_managed_pty_session_emits_output() -> None:
    async def run_case() -> None:
        events: list[dict] = []

        async def emit(payload: dict) -> None:
            events.append(payload)

        session = ManagedPtySession(
            session_id="test-session",
            name="Test",
            command="python3 -c \"print('hello from codex-dashboard')\"",
            cwd=".",
            emit=emit,
        )
        await session.start()

        for _ in range(50):
            if any(event["event_type"] in {"stopped", "failed"} for event in events if event["type"] == "session_event"):
                break
            await asyncio.sleep(0.05)

        assert any(event["event_type"] == "started" for event in events)
        assert any(
            event["event_type"] == "output" and "hello from codex-dashboard" in event.get("text", "")
            for event in events
        )
        assert any(event["event_type"] in {"stopped", "failed"} for event in events)

    asyncio.run(run_case())
