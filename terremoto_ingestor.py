"""Cliente en vivo para terremotovenezuela.com (Supabase público)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from database import settings

logger = logging.getLogger(__name__)

TERREMOTO_SITE = "https://terremotovenezuela.com/"
DESAPARECIDOS_SITE = settings.source_website_url


@dataclass
class LiveStats:
    desaparecidos: dict[str, Any] = field(default_factory=dict)
    terremoto: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "desaparecidos": self.desaparecidos,
            "terremoto": self.terremoto,
            "fuentes": {
                "desaparecidos": DESAPARECIDOS_SITE,
                "terremoto": TERREMOTO_SITE,
            },
        }


class TerremotoVenezuelaClient:
    def __init__(
        self,
        supabase_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base = (supabase_url or settings.terremoto_supabase_url).rstrip("/")
        self.api_key = api_key or settings.terremoto_api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "TerremotoVenezuelaClient":
        self._client = httpx.AsyncClient(timeout=45.0)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Usa el cliente como context manager")
        return self._client

    def _headers(self, count: bool = False) -> dict[str, str]:
        h = {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        if count:
            h["Prefer"] = "count=exact"
        return h

    async def fetch_buildings(
        self,
        limit: int = 50,
        damage_level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "select": "id,name,address,city,zone,lat,lng,damage_level,status,main_photo_url,media_urls,notes,last_updated_at,created_at",
            "order": "last_updated_at.desc",
            "limit": limit,
        }
        if damage_level:
            params["damage_level"] = f"eq.{damage_level}"
        if search:
            params["or"] = f"(name.ilike.%{search}%,address.ilike.%{search}%,city.ilike.%{search}%,zone.ilike.%{search}%)"

        response = await self.client.get(
            f"{self.base}/rest/v1/buildings",
            headers=self._headers(),
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def fetch_building_count(self) -> int:
        response = await self.client.head(
            f"{self.base}/rest/v1/buildings",
            headers=self._headers(count=True),
            params={"select": "id"},
        )
        response.raise_for_status()
        content_range = response.headers.get("content-range", "")
        if "/" in content_range:
            return int(content_range.split("/")[-1])
        return 0

    async def _count_by_filter(self, params: dict[str, str]) -> int:
        response = await self.client.head(
            f"{self.base}/rest/v1/buildings",
            headers=self._headers(count=True),
            params={"select": "id", **params},
        )
        response.raise_for_status()
        content_range = response.headers.get("content-range", "")
        if "/" in content_range:
            return int(content_range.split("/")[-1])
        return 0

    async def fetch_damage_breakdown(self) -> dict[str, int]:
        parcial, severo, dano_total = await asyncio.gather(
            self._count_by_filter({"damage_level": "eq.parcial"}),
            self._count_by_filter({"damage_level": "eq.severo"}),
            self._count_by_filter({"damage_level": "eq.total"}),
        )
        total = await self.fetch_building_count()
        return {
            "total": total,
            "parcial": parcial,
            "severo": severo,
            "dano_total": dano_total,
        }

    async def get_terremoto_stats(self) -> dict[str, Any]:
        total = await self.fetch_building_count()
        breakdown = await self.fetch_damage_breakdown()
        recent = await self.fetch_buildings(limit=5)
        from terremoto_photos import enrich_building

        return {
            "fuente": TERREMOTO_SITE,
            "total_edificios": total,
            "dano_parcial": breakdown.get("parcial", 0),
            "dano_severo": breakdown.get("severo", 0),
            "dano_total": breakdown.get("dano_total", 0),
            "verificados": total,
            "ultimos_reportes": [enrich_building(b) for b in recent],
        }


async def fetch_desaparecidos_local() -> dict[str, Any]:
    from database import MissingStatus, MissingVictim, async_session_factory
    from sqlalchemy import func, select

    async with async_session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
        sin_contacto = (
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
    return {
        "fuente": DESAPARECIDOS_SITE,
        "total": total,
        "sin_contacto": sin_contacto,
        "localizado": localizado,
        "total_pages": 0,
        "source": "local_db",
    }


async def fetch_desaparecidos_live() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{settings.source_api_url.rstrip('/')}/personas",
                params={"page": 1, "pageSize": 1},
            )
            response.raise_for_status()
            data = response.json()
            counts = data.get("counts", {})
            return {
                "fuente": DESAPARECIDOS_SITE,
                "total": counts.get("total", data.get("total", 0)),
                "sin_contacto": counts.get("sinContacto", 0),
                "localizado": counts.get("localizado", 0),
                "total_pages": data.get("totalPages", 0),
                "source": "api",
            }
    except Exception:
        logger.warning("API desaparecidos no disponible; usando base local", exc_info=True)
        return await fetch_desaparecidos_local()


async def fetch_live_unified_stats() -> LiveStats:
    desap, trem = await asyncio.gather(
        fetch_desaparecidos_live(),
        _fetch_terremoto_stats_safe(),
    )
    return LiveStats(
        desaparecidos=desap,
        terremoto=trem,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def _fetch_terremoto_stats_safe() -> dict[str, Any]:
    try:
        async with TerremotoVenezuelaClient() as terremoto:
            return await terremoto.get_terremoto_stats()
    except Exception:
        logger.warning("API terremoto no disponible", exc_info=True)
        return {
            "fuente": TERREMOTO_SITE,
            "total_edificios": 0,
            "dano_parcial": 0,
            "dano_severo": 0,
            "dano_total": 0,
            "verificados": 0,
            "ultimos_reportes": [],
            "source": "unavailable",
        }