from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import sqlite3
from typing import Any


DEFAULT_CODEX_HOME = Path.home() / ".codex"


@dataclass
class TranscriptItem:
    source_key: str
    kind: str
    role: str | None
    text: str
    meta: dict[str, Any]
    created_at: str


@dataclass
class RolloutFollowState:
    codex_session_id: str
    rollout_path: str | None = None
    offset: int = 0
    line_no: int = 0
    carryover: str = ""


@dataclass
class RolloutReadResult:
    items: list[TranscriptItem]
    state: RolloutFollowState
    error: str | None = None


def _state_db_candidates(codex_home: Path) -> list[Path]:
    candidates = [path for path in codex_home.glob("state*.sqlite") if path.is_file()]
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def _parse_timestamp(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def _emit_item(
    *,
    line_no: int,
    item_no: int,
    timestamp: str | None,
    kind: str,
    text: str,
    role: str | None = None,
    meta: dict[str, Any] | None = None,
) -> TranscriptItem | None:
    cleaned = text.rstrip()
    if not cleaned:
        return None
    return TranscriptItem(
        source_key=f"{line_no:010d}:{item_no:04d}",
        kind=kind,
        role=role,
        text=cleaned,
        meta=meta or {},
        created_at=_parse_timestamp(timestamp),
    )


def _truncate_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _json_text(value: Any, *, limit: int = 4000) -> str:
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        rendered = str(value)
    return _truncate_text(rendered, limit=limit)


def _extract_mcp_result_text(value: Any) -> str:
    if not isinstance(value, dict):
        return _json_text(value)
    ok_payload = value.get("Ok")
    if not isinstance(ok_payload, dict):
        return _json_text(value)
    content = ok_payload.get("content")
    if not isinstance(content, list):
        return _json_text(ok_payload)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.rstrip())
    if parts:
        return _truncate_text("\n\n".join(parts))
    return _json_text(ok_payload)


def _format_duration(duration: dict[str, Any] | None) -> str | None:
    if not isinstance(duration, dict):
        return None
    secs = duration.get("secs")
    nanos = duration.get("nanos", 0)
    if not isinstance(secs, int) or not isinstance(nanos, int):
        return None
    total_ms = (secs * 1000) + (nanos // 1_000_000)
    if total_ms < 1000:
        return f"{total_ms} ms"
    return f"{total_ms / 1000:.1f} s"


def _parse_event_msg(record: dict[str, Any], line_no: int) -> list[TranscriptItem]:
    timestamp = record.get("timestamp")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    event_type = payload.get("type")
    items: list[TranscriptItem] = []

    if event_type == "user_message":
        message = payload.get("message")
        if isinstance(message, str):
            item = _emit_item(
                line_no=line_no,
                item_no=0,
                timestamp=timestamp,
                kind="user_message",
                role="user",
                text=message,
            )
            if item is not None:
                items.append(item)
        return items

    if event_type == "agent_message":
        message = payload.get("message")
        if isinstance(message, str):
            phase = payload.get("phase")
            kind = "commentary" if phase == "commentary" else "assistant_message"
            item = _emit_item(
                line_no=line_no,
                item_no=0,
                timestamp=timestamp,
                kind=kind,
                role="assistant",
                text=message,
                meta={"phase": phase} if phase else {},
            )
            if item is not None:
                items.append(item)
        return items

    if event_type == "task_started":
        item = _emit_item(
            line_no=line_no,
            item_no=0,
            timestamp=timestamp,
            kind="task_status",
            text="Turn started",
            meta={
                "turn_id": payload.get("turn_id"),
                "collaboration_mode_kind": payload.get("collaboration_mode_kind"),
            },
        )
        if item is not None:
            items.append(item)
        return items

    if event_type == "task_complete":
        item = _emit_item(
            line_no=line_no,
            item_no=0,
            timestamp=timestamp,
            kind="task_status",
            text="Turn complete",
            meta={"turn_id": payload.get("turn_id")},
        )
        if item is not None:
            items.append(item)
        return items

    if event_type == "exec_command_end":
        command = payload.get("command")
        command_text = ""
        if isinstance(command, list):
            command_text = shlex.join(str(part) for part in command)
        elif command is not None:
            command_text = str(command)
        suffix = []
        if payload.get("status"):
            suffix.append(str(payload["status"]))
        if payload.get("exit_code") is not None:
            suffix.append(f"exit {payload['exit_code']}")
        duration = _format_duration(payload.get("duration"))
        if duration:
            suffix.append(duration)
        summary = f"Ran {command_text}" if command_text else "Ran command"
        if suffix:
            summary = f"{summary} ({', '.join(suffix)})"
        details_text = payload.get("aggregated_output")
        if not isinstance(details_text, str) or not details_text.strip():
            details_text = payload.get("formatted_output")
        if not isinstance(details_text, str):
            details_text = ""
        item = _emit_item(
            line_no=line_no,
            item_no=0,
            timestamp=timestamp,
            kind="tool_activity",
            text=summary,
            meta={
                "tool_name": "exec_command",
                "command": command_text,
                "cwd": payload.get("cwd"),
                "status": payload.get("status"),
                "exit_code": payload.get("exit_code"),
                "details_text": _truncate_text(details_text) if details_text else "",
            },
        )
        if item is not None:
            items.append(item)
        return items

    if event_type == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        invocation = invocation if isinstance(invocation, dict) else {}
        server = invocation.get("server")
        tool = invocation.get("tool")
        label = ".".join(part for part in [str(server) if server else "", str(tool) if tool else ""] if part)
        result = payload.get("result")
        result_text = _extract_mcp_result_text(result)
        ok_payload = result.get("Ok") if isinstance(result, dict) else None
        is_error = isinstance(ok_payload, dict) and bool(ok_payload.get("isError"))
        summary = f"MCP {label}" if label else "MCP tool"
        if is_error:
            summary = f"{summary} failed"
        duration = _format_duration(payload.get("duration"))
        if duration:
            summary = f"{summary} ({duration})"
        item = _emit_item(
            line_no=line_no,
            item_no=0,
            timestamp=timestamp,
            kind="tool_activity",
            text=summary,
            meta={
                "tool_name": label,
                "arguments": invocation.get("arguments"),
                "is_error": is_error,
                "details_text": result_text,
            },
        )
        if item is not None:
            items.append(item)
        return items

    if event_type == "web_search_end":
        query = payload.get("query")
        action = payload.get("action")
        if isinstance(query, str) and query:
            summary = f"Web search: {query}"
        else:
            summary = "Web search"
        item = _emit_item(
            line_no=line_no,
            item_no=0,
            timestamp=timestamp,
            kind="tool_activity",
            text=summary,
            meta={
                "tool_name": "web_search",
                "action": action,
            },
        )
        if item is not None:
            items.append(item)
        return items

    return []


def _parse_reasoning(record: dict[str, Any], line_no: int) -> list[TranscriptItem]:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "reasoning":
        return []
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return []
    parts: list[str] = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.rstrip())
    if not parts:
        return []
    transcript_item = _emit_item(
        line_no=line_no,
        item_no=0,
        timestamp=record.get("timestamp"),
        kind="reasoning_summary",
        role="assistant",
        text="\n".join(parts),
    )
    return [transcript_item] if transcript_item is not None else []


def normalize_rollout_record(record: dict[str, Any], line_no: int) -> list[TranscriptItem]:
    record_type = record.get("type")
    if record_type == "event_msg":
        return _parse_event_msg(record, line_no)
    if record_type == "response_item":
        return _parse_reasoning(record, line_no)
    return []


class CodexRolloutTranscriptReader:
    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = codex_home or DEFAULT_CODEX_HOME

    def resolve_rollout_path(self, codex_session_id: str) -> Path | None:
        for db_path in _state_db_candidates(self.codex_home):
            try:
                connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                row = connection.execute("select rollout_path from threads where id = ?", (codex_session_id,)).fetchone()
            except sqlite3.Error:
                row = None
            finally:
                connection.close()
            if row and row[0]:
                rollout_path = Path(str(row[0]))
                if not rollout_path.is_absolute():
                    rollout_path = self.codex_home / rollout_path
                return rollout_path
        return None

    def read_update(self, state: RolloutFollowState, *, restart: bool = False) -> RolloutReadResult:
        rollout_path = self.resolve_rollout_path(state.codex_session_id)
        if rollout_path is None:
            return RolloutReadResult(items=[], state=state, error="Rollout file could not be resolved")
        if not rollout_path.exists():
            return RolloutReadResult(items=[], state=state, error="Rollout file is missing")

        next_state = RolloutFollowState(
            codex_session_id=state.codex_session_id,
            rollout_path=str(rollout_path),
            offset=0 if restart or state.rollout_path != str(rollout_path) else state.offset,
            line_no=0 if restart or state.rollout_path != str(rollout_path) else state.line_no,
            carryover="" if restart or state.rollout_path != str(rollout_path) else state.carryover,
        )

        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(next_state.offset)
            chunk = handle.read()
            next_state.offset = handle.tell()

        combined = next_state.carryover + chunk
        if not combined:
            return RolloutReadResult(items=[], state=next_state)

        last_newline = combined.rfind("\n")
        if last_newline == -1:
            next_state.carryover = combined
            return RolloutReadResult(items=[], state=next_state)

        complete_text = combined[: last_newline + 1]
        next_state.carryover = combined[last_newline + 1 :]

        items: list[TranscriptItem] = []
        for raw_line in complete_text.splitlines():
            if not raw_line.strip():
                continue
            next_state.line_no += 1
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            items.extend(normalize_rollout_record(record, next_state.line_no))

        return RolloutReadResult(items=items, state=next_state)
