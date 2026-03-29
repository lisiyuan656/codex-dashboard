from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from .config import get_settings
from .db import SessionLocal, engine, get_db
from .hub import ConnectionHub
from .models import Base
from .security import get_websocket_user, require_user
from .store import (
    acquire_lock,
    authenticate_user,
    create_pending_session,
    ensure_default_admin,
    get_agent_detail,
    get_agent_sessions,
    get_session_detail,
    ingest_session_event,
    list_agents_with_sessions,
    mark_agent_offline,
    record_heartbeat,
    refresh_staleness,
    release_lock,
    resolve_latest_pending_approval,
    session_summary,
    upsert_agent,
    verify_agent_token,
)


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
hub = ConnectionHub()


def _is_authenticated(request: Request, db: Session) -> bool:
    return get_websocket_user({"session": request.session}, db) is not None


def _transport_for(session: Any) -> str:
    meta = session.meta or {}
    if session.source == "unmanaged":
        return "unmanaged"
    return meta.get("transport", "app_server")


def create_app() -> FastAPI:
    app = FastAPI(title="Codex Dashboard")
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie=settings.session_cookie_name)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.on_event("startup")
    def startup() -> None:
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            ensure_default_admin(db, settings)
            refresh_staleness(db, settings)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {})

    @app.post("/api/login")
    async def login(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
        payload = await request.json()
        user = authenticate_user(db, payload.get("username", ""), payload.get("password", ""))
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        request.session["user_id"] = user.id
        return JSONResponse({"ok": True})

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db), user=Depends(require_user)) -> HTMLResponse:
        del user
        refresh_staleness(db, settings)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "agents": list_agents_with_sessions(db),
                "hub": hub,
            },
        )

    @app.get("/agents/{agent_id}", response_class=HTMLResponse)
    def agent_page(
        agent_id: str,
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(require_user),
    ) -> HTMLResponse:
        del user
        refresh_staleness(db, settings)
        agent = get_agent_detail(db, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        sessions = get_agent_sessions(db, agent_id)
        active_sessions = [item for item in sessions if item.state not in {"finished", "failed", "stopped"}]
        return templates.TemplateResponse(
            request,
            "agent.html",
            {
                "agent": agent,
                "active_sessions": active_sessions,
                "sessions": sessions,
                "connected": hub.is_agent_connected(agent_id),
            },
        )

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    def session_page(
        session_id: str,
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(require_user),
    ) -> HTMLResponse:
        detail = get_session_detail(db, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return templates.TemplateResponse(
            request,
            "session.html",
            {
                "detail": detail,
                "user": user,
            },
        )

    @app.post("/api/agents/{agent_id}/sessions")
    async def launch_session(
        agent_id: str,
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(require_user),
    ) -> JSONResponse:
        if get_agent_detail(db, agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        payload = await request.json()
        initial_prompt = payload.get("initial_prompt", "").strip()
        if not initial_prompt:
            initial_prompt = payload.get("command", "").strip()
        cwd = payload.get("cwd", "").strip() or "."
        launch_mode = payload.get("launch_mode", "terminal").strip() or "terminal"
        if launch_mode not in {"terminal", "app_server"}:
            raise HTTPException(status_code=400, detail="Unsupported launch mode")
        argv = [str(item) for item in payload.get("argv", [])]
        command = "codex app-server" if launch_mode == "app_server" else " ".join(["codex", "--no-alt-screen", *argv]).strip()
        session = create_pending_session(
            db,
            agent_id=agent_id,
            user_id=user.id,
            source="managed",
            name=payload.get("name", "Managed Codex Session").strip() or "Managed Codex Session",
            command=command or "codex --no-alt-screen",
            cwd=cwd,
            meta={"transport": "app_server" if launch_mode == "app_server" else "tmux_terminal", "initial_prompt": initial_prompt, "argv": argv},
        )
        try:
            await hub.send_action(
                agent_id,
                {
                    "type": "launch_session",
                    "session_id": session.id,
                    "cwd": cwd,
                    "name": session.name,
                    "prompt": initial_prompt,
                    "launch_mode": launch_mode,
                    "argv": argv,
                },
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "session_id": session.id})

    @app.post("/api/sessions/{session_id}/lock")
    async def session_lock(
        session_id: str,
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(require_user),
    ) -> JSONResponse:
        payload = await request.json()
        action = payload.get("action", "acquire")
        if action == "release":
            ok, message = release_lock(db, session_id, user.id)
        else:
            ok, message = acquire_lock(db, session_id, user.id, force=bool(payload.get("force")))
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        await hub.broadcast_session(session_id, {"type": "lock_changed", "message": message, "session_id": session_id})
        return JSONResponse({"ok": True, "message": message})

    @app.post("/api/sessions/{session_id}/actions")
    async def session_action(
        session_id: str,
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(require_user),
    ) -> JSONResponse:
        detail = get_session_detail(db, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session = detail["session"]
        if session.source != "managed":
            raise HTTPException(status_code=409, detail="Unmanaged sessions are read-only")
        lock = detail["lock"]
        if lock is None or lock.user_id != user.id:
            raise HTTPException(status_code=409, detail="Acquire the session lock before sending actions")

        payload = await request.json()
        action_type = payload.get("type")
        transport = _transport_for(session)
        supported_actions = {"send_input", "stop_session"}
        if transport == "tmux_terminal":
            supported_actions |= {"send_enter", "interrupt_session"}
        elif transport == "app_server":
            supported_actions |= {"approve", "deny"}
        if action_type not in supported_actions:
            raise HTTPException(status_code=400, detail="Unsupported action")

        action_payload: dict[str, Any] = {"type": action_type, "session_id": session_id}
        if action_type == "send_input":
            text = payload.get("input", "")
            if not text:
                raise HTTPException(status_code=400, detail="input is required")
            action_payload["input"] = text
        elif action_type in {"approve", "deny"}:
            approval = resolve_latest_pending_approval(db, session_id, user.id, approved=action_type == "approve")
            if approval is None:
                raise HTTPException(status_code=409, detail="No pending approval")
            action_payload["approval_id"] = approval.id

        try:
            await hub.send_action(session.agent_id, action_payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        agent_id = websocket.query_params.get("agent_id", "").strip()
        token = websocket.query_params.get("token", "")
        if not agent_id or not token:
            await websocket.close(code=1008)
            return

        with SessionLocal() as db:
            if not verify_agent_token(db, settings, agent_id, token):
                await websocket.close(code=1008)
                return

        await hub.register_agent(agent_id, websocket)
        try:
            while True:
                payload = await websocket.receive_json()
                message_type = payload.get("type")
                with SessionLocal() as db:
                    if message_type == "hello":
                        upsert_agent(
                            db,
                            agent_id,
                            hostname=payload.get("hostname", agent_id),
                            display_name=payload.get("display_name", agent_id),
                            labels=payload.get("labels", []),
                            meta=payload.get("meta", {}),
                            token=None,
                        )
                    elif message_type == "heartbeat":
                        record_heartbeat(db, agent_id, payload.get("meta", {}))
                    elif message_type == "session_event":
                        session = ingest_session_event(db, agent_id, payload)
                        if session is not None:
                            await hub.broadcast_session(
                                session.id,
                                {
                                    "type": "session_event",
                                    "session_id": session.id,
                                    "event_type": payload["event_type"],
                                    "payload": payload,
                                    "summary": session_summary(session),
                                },
                            )
                    else:
                        await websocket.send_json({"type": "error", "message": f"Unknown message type: {message_type}"})
        except WebSocketDisconnect:
            with SessionLocal() as db:
                mark_agent_offline(db, agent_id)
        finally:
            await hub.unregister_agent(agent_id)

    @app.websocket("/ws/sessions/{session_id}")
    async def session_socket(websocket: WebSocket, session_id: str) -> None:
        with SessionLocal() as db:
            user = get_websocket_user(websocket.scope, db)
            if user is None:
                await websocket.close(code=1008)
                return
            detail = get_session_detail(db, session_id)
            if detail is None:
                await websocket.close(code=1008)
                return
            initial_events = [
                {
                    "type": "history",
                    "session_id": session_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                for event in detail["events"][-100:]
            ]

        await websocket.accept()
        await hub.register_viewer(session_id, websocket)
        try:
            await websocket.send_json({"type": "history_batch", "items": initial_events})
            while True:
                # Keep the socket alive. The browser uses HTTP endpoints for actions.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister_viewer(session_id, websocket)

    @app.exception_handler(HTTPException)
    async def api_errors(request: Request, exc: HTTPException) -> JSONResponse | RedirectResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()


def run() -> None:
    uvicorn.run("codex_dashboard.main:app", host="0.0.0.0", port=8000, reload=False)
