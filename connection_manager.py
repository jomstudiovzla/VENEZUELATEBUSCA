"""ConnectionManager con soporte de rooms por víctima."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Gestiona conexiones WebSocket globales y por room (ID de víctima)."""

    def __init__(self) -> None:
        self._global: dict[int, WebSocket] = {}
        self._rooms: dict[str, dict[int, WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    @property
    def active_count(self) -> int:
        return len(self._global)

    def room_count(self, room: str) -> int:
        return len(self._rooms.get(room, {}))

    async def connect(self, websocket: WebSocket, room: str | None = None) -> int:
        await websocket.accept()
        async with self._lock:
            self._counter += 1
            conn_id = self._counter
            if room:
                self._rooms.setdefault(room, {})[conn_id] = websocket
            else:
                self._global[conn_id] = websocket
        scope = f"room:{room}" if room else "global"
        logger.info("WS conectado | %s | conn=%d", scope, conn_id)
        return conn_id

    async def disconnect(self, conn_id: int, room: str | None = None) -> None:
        async with self._lock:
            if room:
                bucket = self._rooms.get(room, {})
                bucket.pop(conn_id, None)
                if not bucket:
                    self._rooms.pop(room, None)
            else:
                self._global.pop(conn_id, None)

    async def send_personal(self, conn_id: int, message: dict[str, Any], room: str | None = None) -> None:
        async with self._lock:
            if room:
                ws = self._rooms.get(room, {}).get(conn_id)
            else:
                ws = self._global.get(conn_id)
        if ws:
            await ws.send_json(message)

    async def _deliver(self, targets: list[tuple[int, WebSocket]], payload: dict[str, Any], room: str | None) -> int:
        dead: list[int] = []
        delivered = 0
        for conn_id, websocket in targets:
            try:
                await websocket.send_json(payload)
                delivered += 1
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            await self.disconnect(conn_id, room=room)
        return delivered

    async def broadcast(self, event: str, data: dict[str, Any]) -> int:
        payload = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            targets = list(self._global.items())
        return await self._deliver(targets, payload, room=None)

    async def broadcast_to_room(self, room: str, event: str, data: dict[str, Any]) -> int:
        payload = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            targets = list(self._rooms.get(room, {}).items())
        return await self._deliver(targets, payload, room=room)

    async def broadcast_status(
        self,
        victim_id: int,
        data: dict[str, Any],
        *,
        global_event: str = "status_changed",
        room_event: str = "victim_status_changed",
    ) -> dict[str, int]:
        enriched = {**data, "id": victim_id}
        global_delivered = await self.broadcast(global_event, enriched)
        room_delivered = await self.broadcast_to_room(str(victim_id), room_event, enriched)
        return {"global": global_delivered, "room": room_delivered}


status_updates_manager = ConnectionManager()
victim_room_manager = status_updates_manager