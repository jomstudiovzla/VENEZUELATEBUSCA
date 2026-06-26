"""Motor async — compatible SQLite y PostgreSQL."""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.models import Base

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


_SHELTER_MIGRATION_COLS: dict[str, str] = {
    "state": "VARCHAR(64)",
    "description": "TEXT",
    "services_offered": "JSON",
    "is_official": "BOOLEAN DEFAULT 0",
    "verification_status": "VARCHAR(16) DEFAULT 'verificado'",
    "submitted_by_name": "VARCHAR(255)",
    "submitted_by_contact": "VARCHAR(64)",
    "submitted_by_org": "VARCHAR(255)",
    "verification_notes": "TEXT",
    "verified_at": "DATETIME",
    "verified_by": "VARCHAR(255)",
    "rejection_reason": "TEXT",
}


async def _migrate_shelter_columns(conn) -> None:
    cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(shelters)"))).fetchall()}
    added_verification = False
    for col, col_type in _SHELTER_MIGRATION_COLS.items():
        if col not in cols:
            await conn.execute(text(f"ALTER TABLE shelters ADD COLUMN {col} {col_type}"))
            if col == "verification_status":
                added_verification = True
    if added_verification:
        await conn.execute(
            text(
                "UPDATE shelters SET verification_status = 'verificado' "
                "WHERE verification_status IS NULL OR verification_status = ''"
            )
        )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            await _migrate_shelter_columns(conn)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise