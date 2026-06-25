"""WebSocket — Tablero de Esperanza en tiempo real."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class DashboardConnectionManager:
    def __init__(self) -> None:
        self._clients: dict[int, WebSocket] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    @property
    def active_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> int:
        await websocket.accept()
        async with self._lock:
            self._counter += 1
            cid = self._counter
            self._clients[cid] = websocket
        return cid

    async def disconnect(self, conn_id: int) -> None:
        async with self._lock:
            self._clients.pop(conn_id, None)

    async def broadcast(self, event: str, data: dict[str, Any]) -> int:
        payload = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            targets = list(self._clients.items())
        dead: list[int] = []
        sent = 0
        for cid, ws in targets:
            try:
                await ws.send_json(payload)
                sent += 1
            except Exception:
                dead.append(cid)
        for cid in dead:
            await self.disconnect(cid)
        return sent


dashboard_ws = DashboardConnectionManager()