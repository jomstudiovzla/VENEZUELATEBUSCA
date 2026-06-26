"""WebSocket — Tablero de Esperanza en tiempo real (+ Redis opcional)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis_client: Optional[Any] = None
_REDIS_CHANNEL = "red_esperanza:events"


async def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis_client.ping()
        logger.info("Redis conectado para broadcast WS")
        return _redis_client
    except Exception:
        logger.debug("Redis no disponible — solo broadcast local")
        return None


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
        r = await _get_redis()
        if r:
            try:
                await r.publish(_REDIS_CHANNEL, json.dumps(payload, default=str))
            except Exception:
                logger.debug("Redis publish falló")
        return await self._send_local(payload)

    async def _send_local(self, payload: dict[str, Any]) -> int:
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

    async def relay_from_redis(self, raw: str) -> int:
        try:
            payload = json.loads(raw)
            return await self._send_local(payload)
        except Exception:
            return 0


dashboard_ws = DashboardConnectionManager()


async def start_redis_listener() -> None:
    r = await _get_redis()
    if not r:
        return

    async def _loop():
        pubsub = r.pubsub()
        await pubsub.subscribe(_REDIS_CHANNEL)
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                await dashboard_ws.relay_from_redis(msg["data"])

    asyncio.create_task(_loop())