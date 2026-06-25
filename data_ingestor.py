"""Ingesta de desaparecidos desde desaparecidosterremotovenezuela.com."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from tattoo_analyzer import TattooAnalyzer

import cv2
import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from database import MissingPerson, MissingStatus, MissingVictim, SyncLog, settings

logger = logging.getLogger(__name__)

PHOTOS_DIR = Path("reference_photos")
PHOTOS_DIR.mkdir(exist_ok=True)

HEIGHT_PATTERNS = [
    re.compile(r"estatura\s*[:\-]?\s*(\d+[.,]\d+)\s*(?:m|metros?)?", re.I),
    re.compile(r"(\d+[.,]\d+)\s*(?:m|metros?)\s*de\s*estatura", re.I),
    re.compile(r"mide\s*(\d+[.,]\d+)", re.I),
    re.compile(r"(\d{3})\s*cm", re.I),
    re.compile(r"(\d)\s*['′]\s*(\d{1,2})", re.I),
]

TATTOO_PATTERN = re.compile(r"tatuaj[eé]", re.I)
HAIR_PATTERN = re.compile(
    r"pelo\s+([^.,;]+)|cabello\s+([^.,;]+)|peluqu[^.,;]+", re.I
)
SKIN_PATTERN = re.compile(r"(blanc[ao]|moren[ao]|trigueñ[ao]|piel\s+[^.,;]+)", re.I)


@dataclass
class PersonaRecord:
    external_id: str
    nombre: str
    edad: Optional[int]
    ubicacion: Optional[str]
    fecha: Optional[str]
    descripcion: Optional[str]
    contacto: Optional[str]
    foto: Optional[str]
    estado: str
    updated_at: Optional[int]


def parse_height_cm(description: Optional[str]) -> Optional[float]:
    if not description:
        return None

    text = description.strip()
    for pattern in HEIGHT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if len(match.groups()) == 2:
            feet = float(match.group(1))
            inches = float(match.group(2))
            return round((feet * 12 + inches) * 2.54, 1)
        raw = match.group(1).replace(",", ".")
        value = float(raw)
        if value < 3:
            return round(value * 100, 1)
        if value < 10:
            return round(value * 100, 1)
        return round(value, 1)

    if re.search(r"baja de estatura", text, re.I):
        return 155.0
    if re.search(r"alta de estatura", text, re.I):
        return 180.0
    return None


def parse_physical_traits(description: Optional[str]) -> dict[str, Optional[str]]:
    if not description:
        return {"hair": None, "skin_tone": None, "marks": None, "tattoos": []}

    hair = None
    hair_match = HAIR_PATTERN.search(description)
    if hair_match:
        hair = next((g.strip() for g in hair_match.groups() if g), None)

    skin = None
    skin_match = SKIN_PATTERN.search(description)
    if skin_match:
        skin = skin_match.group(1).strip()

    tattoos: list[str] = []
    if TATTOO_PATTERN.search(description):
        tattoos.append(description.strip())

    return {
        "hair": hair,
        "skin_tone": skin,
        "marks": description.strip() if description else None,
        "tattoos": tattoos,
    }


def map_source_status(estado: str) -> MissingStatus:
    if estado == "localizado":
        return MissingStatus.LOCALIZADO
    return MissingStatus.DESAPARECIDO


CEDULA_PATTERN = re.compile(
    r"(?:c[eé]dula|ci|v[\-\.]?|e[\-\.]?)[\s.\-]*(\d[\d.\s\-]{5,12}\d)",
    re.I,
)


def extract_cedula(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = CEDULA_PATTERN.search(text)
    if not match:
        digits = re.sub(r"\D", "", text)
        if 6 <= len(digits) <= 9 and text.strip().replace(".", "").replace("-", "").isdigit():
            return digits
        return None
    return re.sub(r"\D", "", match.group(1))


def format_cedula_ve(digits: str) -> Optional[str]:
    if len(digits) == 7:
        return f"{digits[0]}.{digits[1:4]}.{digits[4:]}"
    if len(digits) == 8:
        return f"{digits[0:2]}.{digits[2:5]}.{digits[5:]}"
    if len(digits) == 9:
        return f"{digits[0]}.{digits[1:4]}.{digits[4:7]}.{digits[7:]}"
    return None


def normalize_search_query(q: str) -> list[str]:
    cleaned = q.strip()
    if not cleaned:
        return []
    variants = [cleaned]
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) >= 6:
        variants.append(digits)
        formatted = format_cedula_ve(digits)
        if formatted:
            variants.append(formatted)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in variants:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def source_estado_for_status(status: Optional[MissingStatus]) -> Optional[str]:
    if status == MissingStatus.DESAPARECIDO:
        return "sin-contacto"
    if status == MissingStatus.LOCALIZADO:
        return "localizado"
    return None


class DesaparecidosIngestor:
    """Cliente para la API pública del sitio desaparecidosterremotovenezuela.com."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        tattoo_analyzer: Optional["TattooAnalyzer"] = None,
    ):
        self.api_base = (api_base or settings.source_api_url).rstrip("/")
        self.tattoo_analyzer = tattoo_analyzer
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "DesaparecidosIngestor":
        self._client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Usa el ingestor como context manager async")
        return self._client

    async def fetch_page(
        self,
        page: int = 1,
        page_size: Optional[int] = None,
        estado: Optional[str] = None,
        query: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "pageSize": page_size or settings.sync_page_size,
        }
        if estado:
            params["estado"] = estado
        if query:
            params["q"] = query

        response = await self.client.get(f"{self.api_base}/personas", params=params)
        response.raise_for_status()
        return response.json()

    async def fetch_persona(self, external_id: str) -> PersonaRecord:
        response = await self.client.get(f"{self.api_base}/personas/{external_id}")
        response.raise_for_status()
        return self._to_record(response.json())

    def _to_record(self, item: dict[str, Any]) -> PersonaRecord:
        return PersonaRecord(
            external_id=item["id"],
            nombre=item.get("nombre", "").strip(),
            edad=item.get("edad"),
            ubicacion=item.get("ubicacion"),
            fecha=item.get("fecha"),
            descripcion=item.get("descripcion") or "",
            contacto=item.get("contacto"),
            foto=item.get("foto") or "",
            estado=item.get("estado", "sin-contacto"),
            updated_at=item.get("updatedAt"),
        )

    async def fetch_all_personas(
        self,
        estado: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> list[PersonaRecord]:
        first = await self.fetch_page(page=1, estado=estado)
        total_pages = first.get("totalPages", 1)
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)

        records = [self._to_record(item) for item in first.get("items", [])]

        for page in range(2, total_pages + 1):
            payload = await self.fetch_page(page=page, estado=estado)
            records.extend(self._to_record(item) for item in payload.get("items", []))
            if page % 25 == 0:
                logger.info("Descargadas %d/%d páginas (%d registros)", page, total_pages, len(records))

        return records

    async def download_photo(self, record: PersonaRecord) -> Optional[Path]:
        if not record.foto:
            return None

        suffix = Path(record.foto).suffix or ".jpg"
        dest = PHOTOS_DIR / f"{record.external_id}{suffix}"
        if dest.exists():
            return dest

        try:
            response = await self.client.get(record.foto)
            response.raise_for_status()
            dest.write_bytes(response.content)
            return dest
        except httpx.HTTPError:
            logger.warning("No se pudo descargar foto de %s", record.external_id)
            return None

    def extract_embeddings_from_photo(self, photo_path: Path) -> list[list[float]]:
        if self.tattoo_analyzer is None:
            return []

        image = cv2.imread(str(photo_path))
        if image is None:
            return []

        regions = self.tattoo_analyzer.extract_from_crop(image)
        if regions:
            return [region.embedding.tolist() for region in regions]

        embedding = self.tattoo_analyzer._cnn_embedding(image)
        return [embedding.tolist()]

    async def upsert_victim(
        self,
        session: AsyncSession,
        record: PersonaRecord,
        process_photo: bool = True,
    ) -> tuple[MissingVictim, bool]:
        result = await session.execute(
            select(MissingVictim).where(MissingVictim.external_id == record.external_id)
        )
        existing = result.scalar_one_or_none()
        traits = parse_physical_traits(record.descripcion)
        height_cm = parse_height_cm(record.descripcion)
        is_new = existing is None

        photo_path: Optional[str] = existing.reference_photo_path if existing else None
        embeddings: Optional[list] = existing.tattoo_embeddings if existing else None

        if process_photo and record.foto:
            local_photo = await self.download_photo(record)
            if local_photo:
                photo_path = str(local_photo)
                if settings.process_photo_embeddings and self.tattoo_analyzer:
                    embeddings = self.extract_embeddings_from_photo(local_photo)

        values = dict(
            external_id=record.external_id,
            full_name=record.nombre,
            age=record.edad,
            height_cm=height_cm,
            skin_tone=traits["skin_tone"],
            hair_description=traits["hair"],
            distinguishing_marks=traits["marks"],
            tattoo_descriptions=traits["tattoos"] or None,
            tattoo_embeddings=embeddings,
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
        else:
            victim = MissingVictim(**values)
            session.add(victim)

        return victim, is_new

    async def sync_database(
        self,
        session: AsyncSession,
        estado: Optional[str] = None,
        max_pages: Optional[int] = None,
        download_photos: bool = True,
        extract_embeddings: bool = False,
    ) -> SyncLog:
        log = SyncLog(
            source=settings.source_website_url,
            started_at=datetime.now(timezone.utc),
        )
        session.add(log)
        await session.flush()

        created = 0
        updated = 0
        photos = 0

        try:
            records = await self.fetch_all_personas(estado=estado, max_pages=max_pages)
            log.records_fetched = len(records)

            for idx, record in enumerate(records, start=1):
                victim, is_new = await self.upsert_victim(
                    session,
                    record,
                    process_photo=download_photos,
                )
                if extract_embeddings and victim.reference_photo_path and self.tattoo_analyzer:
                    path = Path(victim.reference_photo_path)
                    victim.tattoo_embeddings = self.extract_embeddings_from_photo(path)
                if is_new:
                    created += 1
                else:
                    updated += 1
                if victim.reference_photo_path:
                    photos += 1

                if idx % 200 == 0:
                    await session.commit()
                    logger.info("Sincronizados %d/%d registros", idx, len(records))

            log.records_created = created
            log.records_updated = updated
            log.photos_processed = photos
            log.completed = True
            log.finished_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(
                "Sync completado: %d obtenidos, %d nuevos, %d actualizados, %d fotos",
                log.records_fetched,
                created,
                updated,
                photos,
            )
        except Exception as exc:
            log.error_message = str(exc)
            log.finished_at = datetime.now(timezone.utc)
            await session.commit()
            logger.exception("Error durante sincronización")
            raise

        return log

    async def get_source_stats(self) -> dict[str, Any]:
        payload = await self.fetch_page(page=1, page_size=1)
        return {
            "source": settings.source_website_url,
            "api": self.api_base,
            "total": payload.get("total", 0),
            "counts": payload.get("counts", {}),
            "total_pages": payload.get("totalPages", 0),
        }


    async def _commit_with_retry(self, session: AsyncSession, retries: int = 12) -> None:
        for attempt in range(retries):
            try:
                await session.commit()
                return
            except (OperationalError, PendingRollbackError) as exc:
                await session.rollback()
                err = str(exc).lower()
                if ("locked" in err or "rollback" in err) and attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise

    async def reconcile_disk_photos(self, session: AsyncSession) -> int:
        linked = 0
        for photo_path in PHOTOS_DIR.glob("*"):
            if not photo_path.is_file():
                continue
            external_id = photo_path.stem
            result = await session.execute(
                select(MissingVictim).where(MissingVictim.external_id == external_id)
            )
            victim = result.scalar_one_or_none()
            if victim and not victim.reference_photo_path:
                victim.reference_photo_path = str(photo_path)
                linked += 1
        if linked:
            await self._commit_with_retry(session)
        return linked

    async def download_pending_photos(
        self,
        session: AsyncSession,
        batch_size: int = 25,
        limit: Optional[int] = None,
    ) -> dict[str, int]:
        query = select(MissingVictim).where(
            MissingVictim.photo_url.isnot(None),
            MissingVictim.photo_url != "",
            MissingVictim.reference_photo_path.is_(None),
        )
        if limit:
            query = query.limit(limit)

        linked = await self.reconcile_disk_photos(session)
        if linked:
            logger.info("Reconciliadas %d fotos ya descargadas en disco", linked)

        result = await session.execute(query)
        victims = list(result.scalars().all())
        downloaded = 0
        failed = 0
        skipped = 0

        for idx, victim in enumerate(victims, start=1):
            if victim.external_id:
                existing = PHOTOS_DIR / f"{victim.external_id}.jpg"
                if not existing.exists():
                    for candidate in PHOTOS_DIR.glob(f"{victim.external_id}.*"):
                        existing = candidate
                        break
                if existing.exists():
                    victim.reference_photo_path = str(existing)
                    skipped += 1
                    if idx % batch_size == 0:
                        await self._commit_with_retry(session)
                    continue

            record = PersonaRecord(
                external_id=victim.external_id or str(victim.id),
                nombre=victim.full_name,
                edad=victim.age,
                ubicacion=victim.last_known_location,
                fecha=victim.last_seen_date,
                descripcion=victim.distinguishing_marks,
                contacto=victim.reporter_contact,
                foto=victim.photo_url or "",
                estado=victim.source_estado or "sin-contacto",
                updated_at=victim.source_updated_at,
            )
            path = await self.download_photo(record)
            if path:
                victim.reference_photo_path = str(path)
                downloaded += 1
            else:
                failed += 1

            if idx % batch_size == 0:
                await self._commit_with_retry(session)
                logger.info(
                    "Progreso fotos: %d/%d (nuevas=%d, en disco=%d, fallidas=%d)",
                    idx,
                    len(victims),
                    downloaded,
                    skipped,
                    failed,
                )

        await self._commit_with_retry(session)
        return {
            "pending": len(victims),
            "downloaded": downloaded,
            "skipped_existing": skipped,
            "failed": failed,
            "reconciled": linked,
        }


async def run_sync(
    estado: Optional[str] = None,
    max_pages: Optional[int] = None,
    download_photos: bool = True,
    extract_embeddings: bool = False,
) -> SyncLog:
    from database import async_session_factory, init_db

    await init_db()

    analyzer = None
    if extract_embeddings:
        from tattoo_analyzer import TattooAnalyzer

        analyzer = TattooAnalyzer()
    async with DesaparecidosIngestor(tattoo_analyzer=analyzer) as ingestor:
        async with async_session_factory() as session:
            return await ingestor.sync_database(
                session,
                estado=estado,
                max_pages=max_pages,
                download_photos=download_photos,
                extract_embeddings=extract_embeddings,
            )


async def run_photo_download(limit: Optional[int] = None) -> dict[str, int]:
    from database import async_session_factory, init_db

    await init_db()
    async with DesaparecidosIngestor() as ingestor:
        async with async_session_factory() as session:
            return await ingestor.download_pending_photos(session, limit=limit)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "photos":
        asyncio.run(run_photo_download())
    else:
        asyncio.run(run_sync(download_photos=False))