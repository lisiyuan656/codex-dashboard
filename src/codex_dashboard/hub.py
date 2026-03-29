from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionHub:
    def __init__(self) -> None:
        self._agents: dict[str, WebSocket] = {}
        self._watchers: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register_agent(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._agents[agent_id] = websocket

    async def unregister_agent(self, agent_id: str) -> None:
        async with self._lock:
            self._agents.pop(agent_id, None)

    async def register_viewer(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._watchers[session_id].add(websocket)

    async def unregister_viewer(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            watchers = self._watchers.get(session_id)
            if watchers is None:
                return
            watchers.discard(websocket)
            if not watchers:
                self._watchers.pop(session_id, None)

    async def broadcast_session(self, session_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            watchers = list(self._watchers.get(session_id, set()))
        stale = []
        for websocket in watchers:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.unregister_viewer(session_id, websocket)

    async def send_action(self, agent_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            websocket = self._agents.get(agent_id)
        if websocket is None:
            raise RuntimeError(f"Agent {agent_id} is not connected")
        await websocket.send_json(payload)

    def is_agent_connected(self, agent_id: str) -> bool:
        return agent_id in self._agents
