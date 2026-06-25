"""Worker que descarga fotos de edificios a building_photos/."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from connection_manager import victim_room_manager
from event_bus import missing_updates_bus
from terremoto_ingestor import TerremotoVenezuelaClient
from terremoto_photos import (
    enrich_building,
    download_building_photo,
    local_photo_url_for,
    pick_building_photo_url,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildingPhotoStats:
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    cycles: int = 0
    last_batch: int = 0


class BuildingPhotoWorker:
    def __init__(
        self,
        batch_size: int = 6,
        pause_seconds: float = 2.0,
        idle_seconds: float = 30.0,
    ):
        self.batch_size = batch_size
        self.pause_seconds = pause_seconds
        self.idle_seconds = idle_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.stats = BuildingPhotoStats()
        self._offset = 0

    async def _emit_photo_ready(self, building: dict[str, Any], local_url: str) -> None:
        payload = enrich_building(building)
        payload["local_photo_url"] = local_url
        payload["main_photo_url"] = local_url or payload.get("display_photo_url")
        payload["has_local_photo"] = True
        await missing_updates_bus.publish("building_photo_ready", payload)
        await victim_room_manager.broadcast("building_photo_ready", payload)

    async def _process_batch(self) -> int:
        async with TerremotoVenezuelaClient() as client:
            buildings = await client.fetch_buildings(limit=200)

        pending: list[dict[str, Any]] = []
        for building in buildings:
            building_id = building.get("id")
            source = pick_building_photo_url(building)
            if not building_id or not source:
                continue
            if local_photo_url_for(building_id, source):
                self.stats.skipped += 1
                continue
            pending.append(building)

        if not pending:
            return 0

        start = self._offset % max(len(pending), 1)
        batch = pending[start : start + self.batch_size]
        if len(batch) < self.batch_size and start > 0:
            batch = (pending[start:] + pending[: self.batch_size - len(batch)])[: self.batch_size]
        self._offset = (start + self.batch_size) % max(len(pending), 1)

        processed = 0
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as http:
            for building in batch:
                building_id = building["id"]
                source = pick_building_photo_url(building)
                path = await download_building_photo(building_id, source, client=http)
                if path:
                    self.stats.downloaded += 1
                    local_url = f"/building-photos/{path.name}"
                    await self._emit_photo_ready(building, local_url)
                    processed += 1
                else:
                    self.stats.failed += 1
                await asyncio.sleep(self.pause_seconds)

        self.stats.last_batch = processed
        return processed

    async def run_forever(self) -> None:
        self._running = True
        logger.info(
            "BuildingPhotoWorker activo | carpeta=building_photos | lote=%d",
            self.batch_size,
        )
        while self._running:
            try:
                self.stats.cycles += 1
                count = await self._process_batch()
                if count == 0:
                    await asyncio.sleep(self.idle_seconds)
            except Exception:
                logger.exception("Error en BuildingPhotoWorker")
                await asyncio.sleep(self.idle_seconds)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()