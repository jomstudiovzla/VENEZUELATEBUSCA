"""Modelos y configuración de base de datos para el sistema SAR/DVI Venezuela te Busca."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import AsyncGenerator, Optional

from pydantic_settings import BaseSettings
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./ojo_de_dios.db"
    source_api_url: str = "https://desaparecidos-terremoto-api.theempire.tech/api"
    source_website_url: str = "https://desaparecidosterremotovenezuela.com/"
    yolov7_weights: str = "weights/yolov7-oa.pt"
    tattoo_match_threshold: float = 0.72
    height_tolerance_cm: float = 8.0
    frame_skip: int = 5
    sync_page_size: int = 100
    sync_interval_minutes: int = 15
    process_photo_embeddings: bool = True
    scraper_poll_interval: int = 20
    scraper_max_pages_incremental: int = 5
    photo_batch_size: int = 8
    photo_pause_seconds: float = 1.5
    terremoto_supabase_url: str = "https://jckifxsdlnsvbztxydes.supabase.co"
    terremoto_api_key: str = "sb_publishable_i7iEDrCVZcSt0k3RGFrY4g_WrtZBB4w"
    terremoto_poll_interval: int = 20
    building_photo_batch_size: int = 6
    building_photo_pause_seconds: float = 2.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()


class Base(DeclarativeBase):
    pass


class MissingStatus(str, enum.Enum):
    """Estados vitales para triaje y rescate."""

    DESAPARECIDO = "desaparecido"
    LOCALIZADO = "localizado"
    FALLECIDO = "fallecido"


# Compatibilidad con módulos legacy
VictimStatus = MissingStatus


class FeedStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    OFFLINE = "offline"


class MissingPerson(Base):
    """Modelo MissingPerson — tabla física missing_victims (alias missing_persons)."""

    __tablename__ = "missing_victims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    nombre_completo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    edad: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sexo: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    descripcion_fisica: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    height_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estatura_estimada_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    clasificacion_tatuajes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    skin_tone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hair_description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    clothing_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    distinguishing_marks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tattoo_descriptions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tattoo_embeddings: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    reference_photo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    last_known_location: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_seen_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    reporter_contact: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_estado: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_updated_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[MissingStatus] = mapped_column(
        Enum(MissingStatus, name="missing_status", values_callable=lambda x: [e.value for e in x]),
        default=MissingStatus.DESAPARECIDO,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BuildingReport(Base):
    """Reportes comunitarios de edificios dañados (con foto obligatoria)."""

    __tablename__ = "building_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    zone: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    damage_level: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    photo_path: Mapped[str] = mapped_column(String(512), nullable=False)
    reporter_contact: Mapped[str] = mapped_column(String(255), nullable=False)
    reporter_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="reporte_comunidad", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuthorizedRescueFeed(Base):
    __tablename__ = "authorized_rescue_feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    disaster_zone: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    authorized_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    camera_matrix: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    distortion_coeffs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    is_calibrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[FeedStatus] = mapped_column(
        Enum(FeedStatus, name="feed_status"),
        default=FeedStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RescueAlert(Base):
    __tablename__ = "rescue_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    victim_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    feed_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    tattoo_similarity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    height_delta_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    frame_snapshot_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bbox: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    photos_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=connect_args,
)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            for stmt in STATUS_MIGRATION_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(text(stmt))
            await _migrate_forensic_columns(conn)


MissingVictim = MissingPerson

FORENSIC_COLUMN_DDL = [
    "ALTER TABLE missing_victims ADD COLUMN nombre_completo VARCHAR(255)",
    "ALTER TABLE missing_victims ADD COLUMN edad INTEGER",
    "ALTER TABLE missing_victims ADD COLUMN sexo VARCHAR(32)",
    "ALTER TABLE missing_victims ADD COLUMN descripcion_fisica TEXT",
    "ALTER TABLE missing_victims ADD COLUMN estatura_estimada_cm FLOAT",
    "ALTER TABLE missing_victims ADD COLUMN clasificacion_tatuajes JSON",
]

FORENSIC_DATA_MIGRATION = """
UPDATE missing_victims SET nombre_completo = full_name WHERE nombre_completo IS NULL;
UPDATE missing_victims SET edad = age WHERE edad IS NULL AND age IS NOT NULL;
UPDATE missing_victims SET sexo = gender WHERE sexo IS NULL AND gender IS NOT NULL;
UPDATE missing_victims SET estatura_estimada_cm = height_cm WHERE estatura_estimada_cm IS NULL AND height_cm IS NOT NULL;
UPDATE missing_victims SET descripcion_fisica = distinguishing_marks WHERE descripcion_fisica IS NULL AND distinguishing_marks IS NOT NULL;
"""

STATUS_MIGRATION_SQL = """
UPDATE missing_victims SET status='desaparecido' WHERE status IN ('missing','MISSING','desaparecido');
UPDATE missing_victims SET status='localizado' WHERE status IN ('located','rescued','LOCATED','RESCUED','localizado');
UPDATE missing_victims SET status='fallecido' WHERE status IN ('deceased','DECEASED','fallecido');
"""


async def _migrate_forensic_columns(conn) -> None:
    for ddl in FORENSIC_COLUMN_DDL:
        try:
            await conn.execute(text(ddl))
        except Exception:
            pass
    for stmt in FORENSIC_DATA_MIGRATION.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass


async def commit_with_retry(session: AsyncSession, retries: int = 12) -> None:
    import asyncio
    from sqlalchemy.exc import OperationalError, PendingRollbackError

    for attempt in range(retries):
        try:
            await session.commit()
            return
        except (OperationalError, PendingRollbackError) as exc:
            await session.rollback()
            err = str(exc).lower()
            if ("locked" in err or "rollback" in err) and attempt < retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise