"""Bus Pub/Sub en memoria para difusión de eventos en tiempo real."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class EventBus:
    """Publisher-Subscriber asyncio para notificaciones de desaparecidos."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def publish(self, event_type: str, data: dict[str, Any]) -> int:
        payload = {
            "event": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        delivered = 0
        dead: list[asyncio.Queue[dict[str, Any]]] = []

        async with self._lock:
            targets = list(self._subscribers)

        for queue in targets:
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                dead.append(queue)
                logger.warning("Suscriptor lento descartado (cola llena)")

        if dead:
            async with self._lock:
                for queue in dead:
                    self._subscribers.discard(queue)

        return delivered

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


missing_updates_bus = EventBus()