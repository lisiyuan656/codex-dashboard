import json
from pathlib import Path
import sqlite3

from codex_dashboard.agent.transcript import CodexRolloutTranscriptReader, RolloutFollowState


def make_codex_home(tmp_path: Path, *, session_id: str, rollout_path: Path) -> Path:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    db_path = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            model_provider text not null,
            cwd text not null,
            title text not null,
            sandbox_policy text not null,
            approval_mode text not null
        )
        """
    )
    connection.execute(
        """
        insert into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode)
        values (?, ?, 0, 0, 'cli', 'openai', '/repo', 'title', 'workspace-write', 'on-request')
        """,
        (session_id, str(rollout_path)),
    )
    connection.commit()
    connection.close()
    return codex_home


def test_resolve_rollout_path_reads_threads_table(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("", encoding="utf-8")
    codex_home = make_codex_home(tmp_path, session_id="codex-session", rollout_path=rollout)

    reader = CodexRolloutTranscriptReader(codex_home)

    assert reader.resolve_rollout_path("codex-session") == rollout


def test_read_update_normalizes_readable_transcript_items(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    lines = [
        {
            "timestamp": "2026-04-07T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Inspect the session output"},
        },
        {
            "timestamp": "2026-04-07T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Checking the tmux pane.", "phase": "commentary"},
        },
        {
            "timestamp": "2026-04-07T00:00:02Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "**Planning parser work**"}]},
        },
        {
            "timestamp": "2026-04-07T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "exec_command_end",
                "command": ["/usr/bin/zsh", "-lc", "rg -n Working spool.log"],
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "Working\n",
            },
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    codex_home = make_codex_home(tmp_path, session_id="codex-session", rollout_path=rollout)

    reader = CodexRolloutTranscriptReader(codex_home)
    result = reader.read_update(RolloutFollowState(codex_session_id="codex-session"), restart=True)

    assert [item.kind for item in result.items] == [
        "user_message",
        "commentary",
        "reasoning_summary",
        "tool_activity",
    ]
    assert result.items[0].text == "Inspect the session output"
    assert result.items[1].text == "Checking the tmux pane."
    assert result.items[2].text == "**Planning parser work**"
    assert "Ran /usr/bin/zsh -lc 'rg -n Working spool.log'" in result.items[3].text
    assert result.items[3].meta["details_text"] == "Working\n"


def test_read_update_is_incremental(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "First"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    codex_home = make_codex_home(tmp_path, session_id="codex-session", rollout_path=rollout)
    reader = CodexRolloutTranscriptReader(codex_home)

    first = reader.read_update(RolloutFollowState(codex_session_id="codex-session"), restart=True)
    assert [item.text for item in first.items] == ["First"]

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-04-07T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "Second", "phase": "final_answer"},
                }
            )
            + "\n"
        )

    second = reader.read_update(first.state)
    assert [item.text for item in second.items] == ["Second"]
    assert all(item.source_key.startswith("0000000002") for item in second.items)
