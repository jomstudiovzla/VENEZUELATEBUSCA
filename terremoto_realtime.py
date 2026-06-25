"""Poller en tiempo real de terremotovenezuela.com."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from connection_manager import victim_room_manager
from event_bus import missing_updates_bus
from terremoto_ingestor import TerremotoVenezuelaClient, fetch_live_unified_stats
from terremoto_photos import enrich_building

logger = logging.getLogger(__name__)


class TerremotoRealtimeWorker:
    def __init__(self, poll_interval: float = 45.0):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.last_stats: dict[str, Any] = {}
        self.last_building_ids: set[str] = set()
        self.cycles = 0

    async def poll_and_broadcast(self) -> dict[str, Any]:
        stats = await fetch_live_unified_stats()
        payload = stats.to_dict()
        self.cycles += 1

        await missing_updates_bus.publish("live_stats", payload)
        await victim_room_manager.broadcast("live_stats", payload)

        async with TerremotoVenezuelaClient() as client:
            buildings = await client.fetch_buildings(limit=20)

        new_buildings = []
        for b in buildings:
            bid = b.get("id", "")
            if bid and bid not in self.last_building_ids:
                new_buildings.append(b)
                self.last_building_ids.add(bid)

        if len(self.last_building_ids) > 500:
            self.last_building_ids = set(list(self.last_building_ids)[-200:])

        if new_buildings and self.cycles > 1:
            for building in new_buildings[:5]:
                payload = enrich_building(building)
                await missing_updates_bus.publish("terremoto_building", payload)
                await victim_room_manager.broadcast("terremoto_building", payload)

        self.last_stats = payload
        logger.info(
            "Stats en vivo | desap=%s | edificios=%s",
            payload["desaparecidos"].get("total"),
            payload["terremoto"].get("total_edificios"),
        )
        return payload

    async def run_forever(self) -> None:
        self._running = True
        logger.info("TerremotoRealtimeWorker activo | intervalo=%.0fs", self.poll_interval)
        while self._running:
            try:
                await self.poll_and_broadcast()
            except Exception:
                logger.exception("Error polling terremotovenezuela.com")
            await asyncio.sleep(self.poll_interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()