from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    Agent,
    ApprovalRequest,
    Session as ManagedSession,
    SessionEvent,
    SessionLock,
    SessionTranscriptItem,
    User,
    utcnow,
)
from .security import hash_password, verify_password
from .terminal import sanitize_terminal_output


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return utcnow()
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return utcnow()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _show_in_dashboard(session: ManagedSession) -> bool:
    if session.state == "stopped":
        return False
    return not (session.source == "unmanaged" and session.state in {"finished", "failed"})


def _normalized_runtime_state(session: ManagedSession, payload_state: str | None) -> str | None:
    if payload_state != "launching":
        return payload_state
    meta = session.meta or {}
    if (
        session.source == "managed"
        and meta.get("transport") == "tmux_terminal"
        and meta.get("tmux_launch_mode") == "current_pane"
        and session.pid is not None
    ):
        return "running"
    return payload_state


def _output_text_for(session: ManagedSession, payload: dict[str, Any]) -> str:
    text = payload.get("text", "")
    meta = session.meta or {}
    if meta.get("transport") == "tmux_terminal":
        return sanitize_terminal_output(text)
    return text


def ensure_default_admin(db: Session, settings: Settings) -> None:
    user = db.scalar(select(User).where(User.username == settings.admin_username))
    if user is not None:
        return
    db.add(User(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
    db.commit()


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def refresh_staleness(db: Session, settings: Settings) -> None:
    now = utcnow()
    agent_cutoff = now - timedelta(seconds=settings.offline_agent_seconds)
    session_cutoff = now - timedelta(seconds=settings.stale_session_seconds)

    for agent in db.scalars(select(Agent)).all():
        last_seen_at = _as_utc(agent.last_seen_at)
        agent.status = "online" if last_seen_at and last_seen_at >= agent_cutoff else "offline"

    active_states = {"launching", "running", "idle", "awaiting_approval", "stale", "detected"}
    for session in db.scalars(select(ManagedSession).where(ManagedSession.state.in_(active_states))).all():
        if session.state in {"finished", "failed", "stopped"}:
            continue
        last_heartbeat_at = _as_utc(session.last_heartbeat_at)
        if last_heartbeat_at and last_heartbeat_at >= session_cutoff:
            if session.state == "stale":
                session.state = "running"
        elif session.source == "managed":
            session.state = "stale"
        elif session.source == "unmanaged":
            session.state = "stopped"
            if session.ended_at is None:
                session.ended_at = now
    db.commit()


def upsert_agent(
    db: Session,
    agent_id: str,
    *,
    hostname: str,
    display_name: str,
    labels: list[str],
    meta: dict[str, Any],
    token: str | None = None,
) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        agent = Agent(id=agent_id, hostname=hostname, display_name=display_name, labels=labels, meta=meta, token=token)
        db.add(agent)
    else:
        agent.hostname = hostname
        agent.display_name = display_name
        agent.labels = labels
        agent.meta = meta
        if token:
            agent.token = token
    agent.last_seen_at = utcnow()
    agent.status = "online"
    db.commit()
    db.refresh(agent)
    return agent


def mark_agent_offline(db: Session, agent_id: str) -> None:
    agent = db.get(Agent, agent_id)
    if agent is None:
        return
    agent.status = "offline"
    db.commit()


def verify_agent_token(db: Session, settings: Settings, agent_id: str, token: str) -> bool:
    if token == settings.agent_shared_secret:
        return True
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.token:
        return False
    return token == agent.token


def record_heartbeat(db: Session, agent_id: str, meta: dict[str, Any]) -> None:
    agent = db.get(Agent, agent_id)
    if agent is None:
        return
    agent.last_seen_at = utcnow()
    agent.meta = {**agent.meta, **meta}
    agent.status = "online"
    db.commit()


def list_agents_with_sessions(db: Session) -> list[dict[str, Any]]:
    agents = db.scalars(select(Agent).order_by(Agent.display_name)).all()
    sessions = [
        session
        for session in db.scalars(select(ManagedSession).order_by(ManagedSession.started_at.desc())).all()
        if _show_in_dashboard(session)
    ]
    by_agent: dict[str, list[ManagedSession]] = defaultdict(list)
    for session in sessions:
        by_agent[session.agent_id].append(session)

    result = []
    for agent in agents:
        result.append(
            {
                "id": agent.id,
                "display_name": agent.display_name,
                "hostname": agent.hostname,
                "status": agent.status,
                "labels": agent.labels,
                "meta": agent.meta,
                "last_seen_at": agent.last_seen_at,
                "sessions": by_agent.get(agent.id, [])[:8],
            }
        )
    return result


def get_agent_detail(db: Session, agent_id: str) -> Agent | None:
    return db.get(Agent, agent_id)


def get_agent_sessions(db: Session, agent_id: str) -> list[ManagedSession]:
    return [
        session
        for session in db.scalars(
            select(ManagedSession).where(ManagedSession.agent_id == agent_id).order_by(ManagedSession.started_at.desc())
        ).all()
        if _show_in_dashboard(session)
    ]


def get_recoverable_agent_sessions(db: Session, agent_id: str) -> list[dict[str, Any]]:
    recoverable = []
    for session in db.scalars(
        select(ManagedSession).where(ManagedSession.agent_id == agent_id).order_by(ManagedSession.started_at.desc())
    ).all():
        if session.source != "managed" or session.state in {"finished", "failed", "stopped"}:
            continue
        meta = session.meta or {}
        if meta.get("transport") != "tmux_terminal":
            continue
        recoverable.append(
            {
                "session_id": session.id,
                "name": session.name,
                "cwd": session.cwd,
                "argv": list(meta.get("argv", [])),
                "initial_prompt": meta.get("initial_prompt", ""),
                "tmux_pane": meta.get("tmux_pane"),
                "tmux_session": meta.get("tmux_session"),
                "tmux_launch_mode": meta.get("tmux_launch_mode", "detached_session"),
                "repo_path": session.repo_path,
                "git_branch": session.git_branch,
                "pid": session.pid,
                "state": session.state,
                "pane_tty": meta.get("pane_tty"),
                "attached_clients": meta.get("attached_clients", 0),
                "pane_current_command": meta.get("pane_current_command"),
                "codex_session_id": meta.get("codex_session_id"),
                "status_detail": meta.get("status_detail"),
                "last_hook_event": meta.get("last_hook_event"),
            }
        )
    return recoverable


def create_pending_session(
    db: Session,
    *,
    agent_id: str,
    user_id: int,
    source: str,
    name: str,
    command: str,
    cwd: str,
    meta: dict[str, Any],
) -> ManagedSession:
    session = ManagedSession(
        id=uuid.uuid4().hex,
        agent_id=agent_id,
        launched_by_user_id=user_id,
        source=source,
        name=name,
        state="launching" if source == "managed" else "detected",
        command=command,
        cwd=cwd,
        meta=meta,
        last_heartbeat_at=utcnow(),
    )
    db.add(session)
    db.add(SessionEvent(session_id=session.id, event_type="requested", payload={"command": command, "cwd": cwd}))
    db.commit()
    db.refresh(session)
    return session


def ingest_session_event(db: Session, agent_id: str, payload: dict[str, Any]) -> ManagedSession | None:
    session_id = payload["session_id"]
    session = db.get(ManagedSession, session_id)
    if session is None:
        session = ManagedSession(
            id=session_id,
            agent_id=agent_id,
            source=payload.get("source", "managed"),
            name=payload.get("name", "Codex Session"),
            state=payload.get("state", "running"),
            command=payload.get("command", "unknown"),
            cwd=payload.get("cwd", "."),
            meta=payload.get("meta", {}),
            last_heartbeat_at=utcnow(),
        )
        db.add(session)
    event_type = payload["event_type"]
    session.agent_id = agent_id
    session.name = payload.get("name", session.name)
    session.command = payload.get("command", session.command)
    session.cwd = payload.get("cwd", session.cwd)
    session.repo_path = payload.get("repo_path", session.repo_path)
    session.git_branch = payload.get("git_branch", session.git_branch)
    session.pid = payload.get("pid", session.pid)
    session.last_heartbeat_at = utcnow()
    if payload.get("meta"):
        session.meta = {**(session.meta or {}), **payload["meta"]}

    if event_type == "started":
        session.state = "running"
        session.started_at = payload.get("started_at", session.started_at)
    elif event_type == "output":
        session.state = _normalized_runtime_state(session, payload.get("state")) or session.state or "running"
        chunk = _output_text_for(session, payload)
        session.last_output_excerpt = ((session.last_output_excerpt or "") + chunk)[-4000:]
    elif event_type == "heartbeat":
        if session.state not in {"awaiting_approval", "stopped", "finished", "failed"}:
            session.state = _normalized_runtime_state(session, payload.get("state", "running")) or "running"
    elif event_type == "hook_status":
        if session.state not in {"stopped", "finished", "failed"}:
            session.state = payload.get("state", session.state) or session.state or "running"
    elif event_type == "approval_requested":
        session.state = "awaiting_approval"
        db.add(
            ApprovalRequest(
                id=payload.get("approval_id", uuid.uuid4().hex),
                session_id=session.id,
                prompt=payload.get("prompt", "Approval requested"),
                status="pending",
            )
        )
    elif event_type == "approval_resolved":
        approval = db.get(ApprovalRequest, payload["approval_id"])
        if approval is not None:
            approval.status = payload.get("status", "approved")
            approval.resolved_at = utcnow()
            approval.resolved_by_user_id = payload.get("resolved_by_user_id")
        if session.state == "awaiting_approval":
            session.state = "running"
    elif event_type == "stopped":
        session.state = payload.get("state", "finished")
        session.exit_code = payload.get("exit_code")
        session.ended_at = utcnow()
    elif event_type == "detected":
        session.source = "unmanaged"
        session.state = payload.get("state", "detected")
    elif event_type == "failed":
        session.state = "failed"
        session.exit_code = payload.get("exit_code")
        session.ended_at = utcnow()

    db.add(SessionEvent(session_id=session.id, event_type=event_type, payload=payload))
    db.commit()
    db.refresh(session)
    return session


def ingest_session_transcript(db: Session, agent_id: str, payload: dict[str, Any]) -> ManagedSession | None:
    session_id = payload["session_id"]
    session = db.get(ManagedSession, session_id)
    if session is None:
        return None

    session.agent_id = agent_id
    session.last_heartbeat_at = utcnow()
    meta = dict(session.meta or {})
    meta["transcript_source"] = "codex_archive"
    meta["transcript_complete"] = bool(payload.get("complete"))
    meta["transcript_status"] = payload.get("status") or ("ready" if payload.get("items") or payload.get("complete") else "pending")
    meta["transcript_error"] = payload.get("error")
    meta["transcript_last_loaded_at"] = utcnow().isoformat()
    session.meta = meta

    items_payload = payload.get("items") or []
    source_keys = [str(item.get("source_key")) for item in items_payload if item.get("source_key")]
    existing_source_keys: set[str] = set()
    if source_keys:
        existing_source_keys = set(
            db.scalars(
                select(SessionTranscriptItem.source_key).where(
                    SessionTranscriptItem.session_id == session_id,
                    SessionTranscriptItem.source_key.in_(source_keys),
                )
            ).all()
        )

    for item in items_payload:
        source_key = str(item.get("source_key") or "").strip()
        if not source_key or source_key in existing_source_keys:
            continue
        db.add(
            SessionTranscriptItem(
                session_id=session_id,
                source_key=source_key,
                kind=str(item.get("kind") or "commentary"),
                role=str(item["role"]) if item.get("role") is not None else None,
                text=str(item.get("text") or ""),
                meta=item.get("meta") or {},
                created_at=_parse_datetime(item.get("created_at")),
            )
        )
        existing_source_keys.add(source_key)

    db.commit()
    db.refresh(session)
    return session


def session_summary(session: ManagedSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "agent_id": session.agent_id,
        "source": session.source,
        "name": session.name,
        "state": session.state,
        "command": session.command,
        "cwd": session.cwd,
        "repo_path": session.repo_path,
        "git_branch": session.git_branch,
        "pid": session.pid,
        "exit_code": session.exit_code,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "last_heartbeat_at": session.last_heartbeat_at.isoformat() if session.last_heartbeat_at else None,
        "last_output_excerpt": session.last_output_excerpt or "",
        "meta": session.meta or {},
    }


def session_transcript_item_summary(item: SessionTranscriptItem) -> dict[str, Any]:
    return {
        "source_key": item.source_key,
        "kind": item.kind,
        "role": item.role,
        "text": item.text,
        "meta": item.meta or {},
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def get_session_detail(db: Session, session_id: str) -> dict[str, Any] | None:
    session = db.get(ManagedSession, session_id)
    if session is None:
        return None

    events = db.scalars(
        select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.created_at.asc())
    ).all()
    approvals = db.scalars(
        select(ApprovalRequest).where(ApprovalRequest.session_id == session_id).order_by(ApprovalRequest.created_at.desc())
    ).all()
    transcript_items = db.scalars(
        select(SessionTranscriptItem)
        .where(SessionTranscriptItem.session_id == session_id)
        .order_by(SessionTranscriptItem.created_at.asc(), SessionTranscriptItem.source_key.asc(), SessionTranscriptItem.id.asc())
    ).all()
    lock = db.get(SessionLock, session_id)
    return {
        "session": session,
        "events": events,
        "approvals": approvals,
        "transcript_items": transcript_items,
        "lock": lock,
    }


def acquire_lock(db: Session, session_id: str, user_id: int, *, force: bool = False) -> tuple[bool, str]:
    lock = db.get(SessionLock, session_id)
    if lock is None:
        db.add(SessionLock(session_id=session_id, user_id=user_id))
        db.commit()
        return True, "Lock acquired"
    if lock.user_id == user_id:
        return True, "Already locked"
    if force:
        lock.user_id = user_id
        lock.acquired_at = utcnow()
        db.commit()
        return True, "Lock taken over"
    return False, "Session is locked by another user"


def release_lock(db: Session, session_id: str, user_id: int) -> tuple[bool, str]:
    lock = db.get(SessionLock, session_id)
    if lock is None:
        return True, "No lock held"
    if lock.user_id != user_id:
        return False, "You do not hold the lock"
    db.delete(lock)
    db.commit()
    return True, "Lock released"


def resolve_latest_pending_approval(db: Session, session_id: str, user_id: int, *, approved: bool) -> ApprovalRequest | None:
    approval = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.session_id == session_id, ApprovalRequest.status == "pending")
        .order_by(ApprovalRequest.created_at.desc())
    )
    if approval is None:
        return None
    approval.status = "approved" if approved else "denied"
    approval.resolved_by_user_id = user_id
    approval.resolved_at = utcnow()
    db.commit()
    db.refresh(approval)
    return approval
