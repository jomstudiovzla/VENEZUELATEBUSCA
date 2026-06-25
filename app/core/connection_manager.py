"""WebSocket ConnectionManager para el Centro de Comando."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._global: dict[int, WebSocket] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    @property
    def active_count(self) -> int:
        return len(self._global)

    async def connect(self, websocket: WebSocket) -> int:
        await websocket.accept()
        async with self._lock:
            self._counter += 1
            conn_id = self._counter
            self._global[conn_id] = websocket
        return conn_id

    async def disconnect(self, conn_id: int) -> None:
        async with self._lock:
            self._global.pop(conn_id, None)

    async def broadcast(self, event: str, data: dict[str, Any]) -> int:
        payload = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            targets = list(self._global.items())
        dead: list[int] = []
        delivered = 0
        for conn_id, ws in targets:
            try:
                await ws.send_json(payload)
                delivered += 1
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            await self.disconnect(conn_id)
        return delivered


command_center_manager = ConnectionManager()