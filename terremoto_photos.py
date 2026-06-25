"""Fotos de edificios — proxy terremotovenezuela.com + caché local."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

BUILDING_PHOTOS_DIR = Path("building_photos")
BUILDING_PHOTOS_DIR.mkdir(exist_ok=True)

TERREMOTO_MEDIA_PROXY = "https://terremotovenezuela.com/api/public/media"
DAMAGE_MEDIA_PATTERN = re.compile(r"/damage-media/(.+?)(?:\?.*)?$")


def extract_media_path(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = DAMAGE_MEDIA_PATTERN.search(url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    if parsed.path.startswith("/api/public/media/"):
        return parsed.path.removeprefix("/api/public/media/")
    return None


def resolve_building_photo_url(url: Optional[str]) -> Optional[str]:
    """Convierte URL Supabase rota al proxy público que sí funciona."""
    if not url:
        return None
    media_path = extract_media_path(url)
    if media_path:
        return f"{TERREMOTO_MEDIA_PROXY}/{media_path}"
    if url.startswith("/api/public/media/"):
        return f"https://terremotovenezuela.com{url}"
    return url


def pick_building_photo_url(building: dict[str, Any]) -> Optional[str]:
    main = building.get("main_photo_url")
    if main:
        return main
    media_urls = building.get("media_urls") or []
    if media_urls:
        return media_urls[0]
    return None


def local_photo_path(building_id: str, source_url: Optional[str] = None) -> Path:
    ext = ".jpg"
    if source_url:
        suffix = Path(urlparse(source_url).path).suffix
        if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = suffix.lower()
    return BUILDING_PHOTOS_DIR / f"{building_id}{ext}"


def local_photo_url_for(building_id: str, source_url: Optional[str] = None) -> Optional[str]:
    path = local_photo_path(building_id, source_url)
    if path.exists() and path.stat().st_size > 0:
        return f"/building-photos/{path.name}"
    for candidate in BUILDING_PHOTOS_DIR.glob(f"{building_id}.*"):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return f"/building-photos/{candidate.name}"
    return None


def enrich_building(building: dict[str, Any]) -> dict[str, Any]:
    source_url = pick_building_photo_url(building)
    building_id = building.get("id", "")
    display_url = resolve_building_photo_url(source_url)
    local_url = local_photo_url_for(building_id, source_url) if building_id else None
    enriched = dict(building)
    enriched["source_photo_url"] = source_url
    enriched["display_photo_url"] = display_url
    enriched["local_photo_url"] = local_url
    enriched["main_photo_url"] = local_url or display_url
    enriched["has_local_photo"] = bool(local_url)
    return enriched


async def download_building_photo(
    building_id: str,
    source_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Path]:
    if not building_id:
        return None

    existing = local_photo_path(building_id, source_url)
    if existing.exists() and existing.stat().st_size > 0:
        return existing

    for candidate in BUILDING_PHOTOS_DIR.glob(f"{building_id}.*"):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    download_url = resolve_building_photo_url(source_url)
    if not download_url:
        return None

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=90.0, follow_redirects=True)

    dest = local_photo_path(building_id, download_url)
    try:
        response = await client.get(download_url)
        response.raise_for_status()
        dest.write_bytes(response.content)
        logger.info("Foto edificio descargada | %s | %d bytes", building_id, len(response.content))
        return dest
    except httpx.HTTPError:
        logger.warning("No se pudo descargar foto edificio %s desde %s", building_id, download_url)
        return None
    finally:
        if owns_client and client:
            await client.aclose()


async def get_building_photo_stats() -> dict[str, Any]:
    local_files = [p for p in BUILDING_PHOTOS_DIR.iterdir() if p.is_file()]
    return {
        "carpeta": str(BUILDING_PHOTOS_DIR.resolve()),
        "descargadas": len(local_files),
        "bytes_total": sum(p.stat().st_size for p in local_files),
    }