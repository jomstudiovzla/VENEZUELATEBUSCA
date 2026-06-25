"""Rastreador en tiempo real — desaparecidosterremotovenezuela.com."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from data_ingestor import (
    DesaparecidosIngestor,
    PersonaRecord,
    parse_height_cm,
    parse_physical_traits,
    map_source_status,
)
from connection_manager import victim_room_manager
from database import MissingStatus, MissingVictim, async_session_factory, init_db, is_sync_running, settings
from event_bus import missing_updates_bus
from stats_dashboard import broadcast_dashboard_stats

logger = logging.getLogger(__name__)

GENDER_PATTERN = re.compile(r"\b(hombre|mujer|masculino|femenino|varón|varon)\b", re.I)


@dataclass
class ScrapeStats:
    source_total: int = 0
    sin_contacto: int = 0
    localizado: int = 0
    cycles: int = 0
    last_new: int = 0
    last_updated: int = 0


def compute_record_hash(record: PersonaRecord) -> str:
    raw = "|".join(
        [
            record.external_id,
            record.nombre.strip().lower(),
            str(record.edad or ""),
            (record.descripcion or "").strip().lower(),
            (record.foto or "").strip(),
            record.estado,
            str(record.updated_at or ""),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_gender(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    match = GENDER_PATTERN.search(description)
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"hombre", "masculino", "varón", "varon"}:
        return "masculino"
    return "femenino"


def victim_to_event(victim: MissingVictim) -> dict[str, Any]:
    return {
        "id": victim.id,
        "external_id": victim.external_id,
        "full_name": victim.full_name,
        "age": victim.age,
        "gender": victim.gender,
        "photo_url": victim.photo_url,
        "height_cm": victim.height_cm,
        "hair_description": victim.hair_description,
        "skin_tone": victim.skin_tone,
        "distinguishing_marks": victim.distinguishing_marks,
        "tattoo_descriptions": victim.tattoo_descriptions,
        "last_known_location": victim.last_known_location,
        "last_seen_date": victim.last_seen_date,
        "reporter_contact": victim.reporter_contact,
        "status": victim.status.value,
        "source_estado": victim.source_estado,
        "has_photo": bool(victim.reference_photo_path or victim.photo_url),
    }


class RealtimeScraper:
    """
    Monitor continuo de la plataforma pública.
    El sitio es Next.js; los datos viven en su API REST (descubierta en el bundle JS).
    BeautifulSoup valida el DOM; httpx extrae los registros.
    """

    def __init__(
        self,
        poll_interval: Optional[float] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ):
        self.website_url = settings.source_website_url.rstrip("/")
        self.api_base = settings.source_api_url.rstrip("/")
        self.poll_interval = poll_interval or float(settings.scraper_poll_interval)
        self.max_incremental_pages = settings.scraper_max_pages_incremental
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._known_hashes: dict[str, str] = {}
        self._last_source_total = 0
        self._catchup_page = 1
        self.stats = ScrapeStats()
        self._on_event = on_event

    async def _commit_with_retry(self, session: AsyncSession, retries: int = 6) -> None:
        for attempt in range(retries):
            try:
                await session.commit()
                return
            except OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < retries - 1:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise

    async def scrape_dom_stats(self, client: httpx.AsyncClient) -> dict[str, int]:
        """Lee contadores del HTML público como señal de cambio."""
        try:
            response = await client.get(self.website_url, timeout=20.0)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            values = [
                int(el.get_text(strip=True))
                for el in soup.select("[class*='statValue']")
                if el.get_text(strip=True).isdigit()
            ]
            if len(values) >= 3:
                return {
                    "total": values[0],
                    "sin_contacto": values[1],
                    "localizado": values[2],
                }
        except Exception:
            logger.debug("No se pudieron leer stats del DOM; usando API", exc_info=True)
        return {}

    async def upsert_record(
        self,
        session: AsyncSession,
        record: PersonaRecord,
        ingestor: DesaparecidosIngestor,
    ) -> tuple[Optional[MissingVictim], str]:
        """
        Upsert por external_id + hash de contenido.
        Retorna (victim, action) donde action ∈ created|updated|unchanged.
        """
        record_hash = compute_record_hash(record)
        cached = self._known_hashes.get(record.external_id)
        if cached == record_hash:
            return None, "unchanged"

        result = await session.execute(
            select(MissingVictim).where(MissingVictim.external_id == record.external_id)
        )
        existing = result.scalar_one_or_none()
        traits = parse_physical_traits(record.descripcion)
        height_cm = parse_height_cm(record.descripcion)
        gender = parse_gender(record.descripcion)
        is_new = existing is None
        previous_status = existing.status if existing else None

        photo_path = existing.reference_photo_path if existing else None

        values = dict(
            external_id=record.external_id,
            full_name=record.nombre,
            age=record.edad,
            gender=gender,
            height_cm=height_cm,
            skin_tone=traits["skin_tone"],
            hair_description=traits["hair"],
            distinguishing_marks=traits["marks"],
            tattoo_descriptions=traits["tattoos"] or None,
            reference_photo_path=photo_path,
            photo_url=record.foto or None,
            last_known_location=record.ubicacion,
            last_seen_date=record.fecha,
            reporter_contact=record.contacto,
            source_estado=record.estado,
            source_updated_at=record.updated_at,
            status=map_source_status(record.estado),
        )

        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            victim = existing
            action = "updated" if (cached != record_hash or previous_status != victim.status) else "unchanged"
        else:
            victim = MissingVictim(**values)
            session.add(victim)
            action = "created"

        await session.flush()
        self._known_hashes[record.external_id] = record_hash
        return victim, action

    async def _emit(self, event_type: str, victim: MissingVictim) -> None:
        payload = victim_to_event(victim)
        if self._on_event:
            result = self._on_event(event_type, payload)
            if asyncio.iscoroutine(result):
                await result
        await missing_updates_bus.publish(event_type, payload)

        if event_type in ("updated_missing", "new_missing"):
            await victim_room_manager.broadcast(event_type, payload)
            status_payload = {
                "id": victim.id,
                "external_id": victim.external_id,
                "full_name": victim.full_name,
                "nombre_completo": victim.nombre_completo or victim.full_name,
                "nuevo_estado": victim.status.value,
                "estado": victim.status.value.upper(),
                "last_known_location": victim.last_known_location,
                "photo_url": victim.photo_url,
            }
            await victim_room_manager.broadcast_status(victim.id, status_payload)

    async def _load_local_counts(self) -> None:
        async with async_session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
            desaparecido = (
                await session.scalar(
                    select(func.count()).where(MissingVictim.status == MissingStatus.DESAPARECIDO)
                )
                or 0
            )
            localizado = (
                await session.scalar(
                    select(func.count()).where(MissingVictim.status == MissingStatus.LOCALIZADO)
                )
                or 0
            )
        self.stats.source_total = total
        self.stats.sin_contacto = desaparecido
        self.stats.localizado = localizado

    async def poll_cycle(self) -> dict[str, int]:
        created = 0
        updated = 0
        unchanged = 0
        api_offline = False

        if is_sync_running():
            logger.debug("Scraper en pausa: sincronización completa activa")
            return {"created": 0, "updated": 0, "unchanged": 0}

        try:
            async with DesaparecidosIngestor() as ingestor:
                client = ingestor.client
                dom_stats = await self.scrape_dom_stats(client)
                first_page = await ingestor.fetch_page(page=1, page_size=settings.sync_page_size)
                api_counts = first_page.get("counts", {})
                source_total = api_counts.get("total", first_page.get("total", 0))

                self.stats.source_total = source_total
                self.stats.sin_contacto = api_counts.get("sinContacto", dom_stats.get("sin_contacto", 0))
                self.stats.localizado = api_counts.get("localizado", dom_stats.get("localizado", 0))

                async with async_session_factory() as session:
                    local_count = int(
                        await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
                    )

                total_pages = max(1, (source_total + settings.sync_page_size - 1) // settings.sync_page_size)
                page_numbers: list[int]

                if source_total > local_count + 50:
                    batch = min(25, max(self.max_incremental_pages * 5, 10))
                    page_numbers = [
                        ((self._catchup_page - 1 + i) % total_pages) + 1 for i in range(batch)
                    ]
                    self._catchup_page = (self._catchup_page + batch - 1) % total_pages + 1
                    logger.info(
                        "Catch-up activo | local=%d fuente=%d | páginas %s…",
                        local_count,
                        source_total,
                        page_numbers[:3],
                    )
                elif source_total > self._last_source_total:
                    delta_pages = max(
                        1,
                        (source_total - self._last_source_total) // settings.sync_page_size + 1,
                    )
                    pages_to_scan = min(delta_pages, self.max_incremental_pages)
                    page_numbers = list(range(1, pages_to_scan + 1))
                else:
                    page_numbers = [1]

                self._last_source_total = source_total

                for page in page_numbers:
                    payload = first_page if page == 1 else await ingestor.fetch_page(
                        page=page, page_size=settings.sync_page_size
                    )
                    async with async_session_factory() as session:
                        for item in payload.get("items", []):
                            record = ingestor._to_record(item)
                            victim, action = await self.upsert_record(session, record, ingestor)
                            if action == "created" and victim:
                                created += 1
                                await self._commit_with_retry(session)
                                await self._emit("new_missing", victim)
                            elif action == "updated" and victim:
                                updated += 1
                                await self._commit_with_retry(session)
                                await self._emit("updated_missing", victim)
                            else:
                                unchanged += 1
        except Exception:
            logger.warning("API desaparecidos no disponible en ciclo de rastreo; usando BD local", exc_info=True)
            api_offline = True
            await self._load_local_counts()

        self.stats.cycles += 1
        self.stats.last_new = created
        self.stats.last_updated = updated
        await broadcast_dashboard_stats(
            scraper_stats={
                "cycles": self.stats.cycles,
                "source_total": self.stats.source_total,
                "sin_contacto": self.stats.sin_contacto,
                "localizado": self.stats.localizado,
                "last_new": created,
                "last_updated": updated,
                "api_offline": api_offline,
            }
        )
        logger.info(
            "Ciclo #%d | total_fuente=%d | nuevos=%d | actualizados=%d | sin_cambio=%d",
            self.stats.cycles,
            self.stats.source_total,
            created,
            updated,
            unchanged,
        )
        return {"created": created, "updated": updated, "unchanged": unchanged}

    async def preload_hashes(self) -> None:
        async with async_session_factory() as session:
            result = await session.execute(
                select(MissingVictim).order_by(MissingVictim.updated_at.desc()).limit(5000)
            )
            for victim in result.scalars().all():
                if not victim.external_id:
                    continue
                record = PersonaRecord(
                    external_id=victim.external_id,
                    nombre=victim.full_name,
                    edad=victim.age,
                    ubicacion=victim.last_known_location,
                    fecha=victim.last_seen_date,
                    descripcion=victim.distinguishing_marks,
                    contacto=victim.reporter_contact,
                    foto=victim.photo_url,
                    estado=victim.source_estado or "sin-contacto",
                    updated_at=victim.source_updated_at,
                )
                self._known_hashes[victim.external_id] = compute_record_hash(record)

            total = await session.scalar(select(func.count()).select_from(MissingVictim))
            logger.info("Precargados %d hashes | registros locales=%d", len(self._known_hashes), total or 0)

    async def run_forever(self) -> None:
        self._running = True
        await init_db()
        await self.preload_hashes()
        logger.info(
            "Rastreador activo | origen=%s | intervalo=%.0fs",
            self.website_url,
            self.poll_interval,
        )

        while self._running:
            try:
                await self.poll_cycle()
            except Exception:
                logger.exception("Error en ciclo de rastreo; reintentando")
            await asyncio.sleep(self.poll_interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


