"""Conexión async a la BD de desaparecidos (ojo_de_dios.db)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)

_CEDULA_RE = re.compile(
    r"(?:cedula|c[eé]dula|cedula\s*/\s*id)\s*:?\s*([\d][\d.\-\s]{5,12}\d)",
    re.IGNORECASE,
)

_MIGRATION_COLS: dict[str, str] = {
    "cedula": "VARCHAR(32)",
    "ingreso_shelter_id": "VARCHAR(36)",
    "ingreso_shelter_name": "VARCHAR(255)",
    "ingreso_at": "DATETIME",
    "ingreso_notas": "TEXT",
    "estado_encontrado": "VARCHAR(64)",
    "ubicacion_encontrado": "VARCHAR(512)",
    "descripcion_atencion": "TEXT",
    "confirmacion_postmortem": "BOOLEAN DEFAULT 0",
    "acta_defuncion_hash": "VARCHAR(128)",
    "biometric_embedding": "JSON",
    "candado_forense": "BOOLEAN DEFAULT 0",
}

_backfill_task: asyncio.Task | None = None
_backfill_done = False

connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if settings.victims_database_url.startswith("sqlite")
    else {}
)
victims_engine = create_async_engine(
    settings.victims_database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=connect_args,
)
victims_session_factory = async_sessionmaker(victims_engine, class_=AsyncSession, expire_on_commit=False)


def normalize_cedula(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def extract_cedula_from_text(*texts: str | None) -> str | None:
    for raw in texts:
        if not raw:
            continue
        for part in re.split(r"[\t|]+", raw):
            digits = normalize_cedula(part)
            if 6 <= len(digits) <= 10:
                return digits
        match = _CEDULA_RE.search(raw)
        if match:
            digits = normalize_cedula(match.group(1))
            if len(digits) >= 6:
                return digits
        for token in re.findall(r"\b\d[\d.\-]{5,12}\d\b", raw):
            digits = normalize_cedula(token)
            if 6 <= len(digits) <= 10:
                return digits
    return None


async def _migrate_columns(conn) -> None:
    cols = {
        row[1]
        for row in (await conn.execute(text("PRAGMA table_info(missing_victims)"))).fetchall()
    }
    for col, col_type in _MIGRATION_COLS.items():
        if col not in cols:
            await conn.execute(text(f"ALTER TABLE missing_victims ADD COLUMN {col} {col_type}"))
            logger.info("Columna %s añadida a missing_victims", col)

    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_missing_victims_cedula ON missing_victims (cedula)")
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_missing_victims_nombre_completo "
            "ON missing_victims (nombre_completo)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_missing_victims_ingreso_shelter "
            "ON missing_victims (ingreso_shelter_id)"
        )
    )


async def backfill_cedulas_batch(batch_size: int | None = None) -> tuple[int, int]:
    """Extrae cédulas de un lote. Retorna (indexadas, filas_escaneadas)."""
    size = batch_size or settings.victims_cedula_batch_size
    indexed = 0
    scanned = 0
    async with victims_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, full_name, nombre_completo, descripcion_fisica, distinguishing_marks "
                    "FROM missing_victims "
                    "WHERE cedula IS NULL "
                    "ORDER BY id "
                    "LIMIT :limit"
                ),
                {"limit": size},
            )
        ).fetchall()
        scanned = len(rows)
        for row_id, full_name, nombre, desc, marks in rows:
            cedula = extract_cedula_from_text(nombre, full_name, desc, marks) or ""
            await conn.execute(
                text("UPDATE missing_victims SET cedula = :cedula WHERE id = :id"),
                {"id": row_id, "cedula": cedula},
            )
            if cedula:
                indexed += 1
    return indexed, scanned


async def cedula_backfill_loop() -> None:
    """Backfill completo de cédulas en segundo plano hasta agotar registros."""
    global _backfill_done
    total = 0
    while not _backfill_done:
        try:
            indexed, scanned = await backfill_cedulas_batch()
            total += indexed
            if scanned == 0:
                _backfill_done = True
                logger.info("Backfill de cédulas completado (%d indexadas en esta sesión)", total)
                break
            if indexed:
                logger.info("Backfill cédulas: +%d en lote de %d (acumulado: %d)", indexed, scanned, total)
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en backfill de cédulas")
            await asyncio.sleep(5)


def start_cedula_backfill() -> asyncio.Task:
    global _backfill_task
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.create_task(cedula_backfill_loop())
    return _backfill_task


async def init_victims_db() -> None:
    async with victims_engine.begin() as conn:
        if settings.victims_database_url.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
        await _migrate_columns(conn)

    async with victims_engine.connect() as conn:
        pending = await conn.scalar(
            text("SELECT COUNT(*) FROM missing_victims WHERE cedula IS NULL")
        )
    if pending:
        logger.info("Pendientes de indexar cédula: %d — iniciando backfill en background", pending)
        start_cedula_backfill()


async def get_victims_session() -> AsyncGenerator[AsyncSession, None]:
    async with victims_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise