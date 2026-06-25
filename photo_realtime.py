"""Descarga continua de fotos con emisión WebSocket en tiempo real."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from connection_manager import victim_room_manager
from data_ingestor import DesaparecidosIngestor, PersonaRecord, PHOTOS_DIR
from database import MissingPerson, async_session_factory, commit_with_retry, init_db, settings
from event_bus import missing_updates_bus
from forensic_utils import person_to_forensic_dict, sync_forensic_fields

logger = logging.getLogger(__name__)


@dataclass
class PhotoWorkerStats:
    downloaded: int = 0
    failed: int = 0
    linked: int = 0
    cycles: int = 0
    last_batch: int = 0


class PhotoRealtimeWorker:
    """Descarga fotos en lotes pequeños sin bloquear el scraper ni los PATCH."""

    def __init__(
        self,
        batch_size: int = 8,
        pause_seconds: float = 2.0,
        idle_seconds: float = 15.0,
    ):
        self.batch_size = batch_size
        self.pause_seconds = pause_seconds
        self.idle_seconds = idle_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.stats = PhotoWorkerStats()

    def local_photo_url(self, external_id: str, path: Path) -> str:
        return f"/photos/{path.name}"

    async def _emit_photo_ready(self, person: MissingPerson, local_url: str) -> None:
        sync_forensic_fields(person)
        payload = {
            "id": person.id,
            "external_id": person.external_id,
            "full_name": person.full_name,
            "nombre_completo": person.nombre_completo or person.full_name,
            "photo_url": person.photo_url,
            "local_photo_url": local_url,
            "status": person.status.value,
            "nuevo_estado": person.status.value,
            "last_known_location": person.last_known_location,
        }
        await missing_updates_bus.publish("photo_ready", payload)
        await victim_room_manager.broadcast("photo_ready", payload)
        await victim_room_manager.broadcast_to_room(str(person.id), "photo_ready", payload)

    async def _fetch_pending_ids(self, limit: int) -> list[int]:
        async with async_session_factory() as session:
            result = await session.execute(
                select(MissingPerson.id)
                .where(
                    MissingPerson.photo_url.isnot(None),
                    MissingPerson.photo_url != "",
                    MissingPerson.reference_photo_path.is_(None),
                )
                .order_by(MissingPerson.updated_at.desc())
                .limit(limit)
            )
            return [row[0] for row in result.all()]

    async def _process_one(self, ingestor: DesaparecidosIngestor, person_id: int) -> bool:
        async with async_session_factory() as session:
            person = await session.get(MissingPerson, person_id)
            if person is None or not person.photo_url:
                return False

            if person.external_id:
                for candidate in PHOTOS_DIR.glob(f"{person.external_id}.*"):
                    person.reference_photo_path = str(candidate)
                    await session.flush()
                    await commit_with_retry(session)
                    local_url = self.local_photo_url(person.external_id, candidate)
                    await self._emit_photo_ready(person, local_url)
                    self.stats.linked += 1
                    return True

            record = PersonaRecord(
                external_id=person.external_id or str(person.id),
                nombre=person.full_name,
                edad=person.age,
                ubicacion=person.last_known_location,
                fecha=person.last_seen_date,
                descripcion=person.distinguishing_marks,
                contacto=person.reporter_contact,
                foto=person.photo_url,
                estado=person.source_estado or "sin-contacto",
                updated_at=person.source_updated_at,
            )
            path = await ingestor.download_photo(record)
            if not path:
                self.stats.failed += 1
                return False

            person.reference_photo_path = str(path)
            await session.flush()
            await commit_with_retry(session)
            local_url = self.local_photo_url(person.external_id or str(person.id), path)
            await self._emit_photo_ready(person, local_url)
            self.stats.downloaded += 1
            return True

    async def run_cycle(self) -> int:
        pending_ids = await self._fetch_pending_ids(self.batch_size)
        if not pending_ids:
            return 0

        processed = 0
        async with DesaparecidosIngestor() as ingestor:
            for person_id in pending_ids:
                try:
                    if await self._process_one(ingestor, person_id):
                        processed += 1
                except Exception:
                    logger.exception("Error descargando foto id=%d", person_id)
                    self.stats.failed += 1
                await asyncio.sleep(0.15)

        self.stats.last_batch = processed
        self.stats.cycles += 1
        if processed:
            logger.info(
                "Lote fotos | procesadas=%d | total_descargadas=%d | fallidas=%d",
                processed,
                self.stats.downloaded,
                self.stats.failed,
            )
        return processed

    async def reconcile_disk_batch(self) -> int:
        linked = 0
        async with async_session_factory() as session:
            for photo_path in list(PHOTOS_DIR.glob("*"))[:50]:
                if not photo_path.is_file():
                    continue
                external_id = photo_path.stem
                result = await session.execute(
                    select(MissingPerson).where(MissingPerson.external_id == external_id)
                )
                person = result.scalar_one_or_none()
                if person and not person.reference_photo_path:
                    person.reference_photo_path = str(photo_path)
                    linked += 1
            if linked:
                await commit_with_retry(session)
                self.stats.linked += linked
                logger.info("Reconciliadas %d fotos en disco", linked)
        return linked

    async def run_forever(self) -> None:
        await init_db()
        await self.reconcile_disk_batch()
        self._running = True
        logger.info("PhotoRealtimeWorker activo | lote=%d", self.batch_size)

        while self._running:
            try:
                count = await self.run_cycle()
                delay = self.idle_seconds if count == 0 else self.pause_seconds
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error en ciclo de fotos")
                await asyncio.sleep(self.pause_seconds * 2)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


async def get_photo_stats() -> dict:
    async with async_session_factory() as session:
        from sqlalchemy import func

        total = await session.scalar(select(func.count()).select_from(MissingPerson))
        with_url = await session.scalar(
            select(func.count()).where(
                MissingPerson.photo_url.isnot(None), MissingPerson.photo_url != ""
            )
        )
        with_local = await session.scalar(
            select(func.count()).where(MissingPerson.reference_photo_path.isnot(None))
        )
    pending = (with_url or 0) - (with_local or 0)
    return {
        "total": total or 0,
        "con_url": with_url or 0,
        "descargadas": with_local or 0,
        "pendientes": max(0, pending),
    }