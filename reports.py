"""Reportes comunitarios — personas y edificios con foto obligatoria."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from connection_manager import victim_room_manager
from database import BuildingReport, MissingStatus, MissingVictim
from event_bus import missing_updates_bus
from scraper_realtime import victim_to_event
from terremoto_photos import BUILDING_PHOTOS_DIR

PHOTOS_DIR = Path("reference_photos")
PHOTOS_DIR.mkdir(exist_ok=True)
BUILDING_PHOTOS_DIR.mkdir(exist_ok=True)

MAX_PHOTO_BYTES = 10 * 1024 * 1024
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

CEDULA_RE = re.compile(r"[\d.\-]+")


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise HTTPException(400, "La edad debe ser un número válido") from exc
    if parsed < 0 or parsed > 120:
        raise HTTPException(400, "La edad debe estar entre 0 y 120")
    return parsed


def _parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise HTTPException(400, "Coordenada inválida") from exc


async def _read_photo(upload: UploadFile) -> tuple[bytes, str]:
    if not upload or not upload.filename:
        raise HTTPException(400, "La fotografía es obligatoria")
    content_type = (upload.content_type or "").lower()
    ext = ALLOWED_MIME.get(content_type)
    if not ext:
        suffix = Path(upload.filename).suffix.lower()
        if suffix in ALLOWED_EXT:
            ext = ".jpg" if suffix == ".jpeg" else suffix
    if not ext:
        raise HTTPException(400, "Formato de imagen no permitido (use JPG, PNG o WebP)")
    data = await upload.read()
    if not data:
        raise HTTPException(400, "La fotografía está vacía")
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "La fotografía supera el límite de 10 MB")
    return data, ext


def _validate_person_fields(
    *,
    full_name: str,
    last_known_location: str,
    reporter_contact: str,
    reporter_name: Optional[str],
    cedula: Optional[str],
    gender: Optional[str],
) -> dict[str, Any]:
    name = _clean(full_name)
    location = _clean(last_known_location)
    contact = _clean(reporter_contact)
    if len(name) < 3:
        raise HTTPException(400, "Indique el nombre completo de la persona")
    if len(location) < 5:
        raise HTTPException(400, "Indique dónde fue vista por última vez")
    if len(contact) < 6:
        raise HTTPException(400, "Indique un teléfono o correo de contacto del reportante")
    clean_cedula = _clean(cedula)
    if clean_cedula and not CEDULA_RE.fullmatch(clean_cedula.replace(" ", "")):
        raise HTTPException(400, "Formato de cédula inválido")
    clean_gender = _clean(gender).lower()
    if clean_gender and clean_gender not in {"masculino", "femenino", "otro"}:
        raise HTTPException(400, "Género inválido")
    return {
        "full_name": name,
        "last_known_location": location,
        "reporter_contact": contact,
        "reporter_name": _clean(reporter_name) or None,
        "cedula": clean_cedula or None,
        "gender": clean_gender or None,
    }


def _validate_building_fields(
    *,
    name: str,
    address: str,
    city: str,
    damage_level: str,
    reporter_contact: str,
    reporter_name: Optional[str],
    zone: Optional[str],
    notes: Optional[str],
) -> dict[str, Any]:
    clean_name = _clean(name)
    clean_address = _clean(address)
    clean_city = _clean(city)
    clean_contact = _clean(reporter_contact)
    level = _clean(damage_level).lower()
    if len(clean_name) < 2:
        raise HTTPException(400, "Indique el nombre del edificio o conjunto")
    if len(clean_address) < 5:
        raise HTTPException(400, "Indique la dirección del edificio")
    if len(clean_city) < 2:
        raise HTTPException(400, "Indique la ciudad")
    if level not in {"parcial", "severo", "total"}:
        raise HTTPException(400, "Seleccione el nivel de daño")
    if len(clean_contact) < 6:
        raise HTTPException(400, "Indique un teléfono o correo de contacto del reportante")
    return {
        "name": clean_name,
        "address": clean_address,
        "city": clean_city,
        "zone": _clean(zone) or None,
        "damage_level": level,
        "notes": _clean(notes) or None,
        "reporter_contact": clean_contact,
        "reporter_name": _clean(reporter_name) or None,
    }


async def create_person_report(
    session: AsyncSession,
    *,
    full_name: str,
    last_known_location: str,
    reporter_contact: str,
    photo: UploadFile,
    cedula: Optional[str] = None,
    age: Optional[str] = None,
    gender: Optional[str] = None,
    last_seen_date: Optional[str] = None,
    distinguishing_marks: Optional[str] = None,
    reporter_name: Optional[str] = None,
) -> dict[str, Any]:
    fields = _validate_person_fields(
        full_name=full_name,
        last_known_location=last_known_location,
        reporter_contact=reporter_contact,
        reporter_name=reporter_name,
        cedula=cedula,
        gender=gender,
    )
    photo_data, ext = await _read_photo(photo)
    parsed_age = _parse_optional_int(age)
    external_id = f"report-{uuid.uuid4().hex[:12]}"
    photo_path = PHOTOS_DIR / f"{external_id}{ext}"
    photo_path.write_bytes(photo_data)

    marks_parts = []
    if fields["cedula"]:
        marks_parts.append(f"Cédula: {fields['cedula']}")
    if _clean(distinguishing_marks):
        marks_parts.append(_clean(distinguishing_marks))
    if fields["reporter_name"]:
        marks_parts.append(f"Reporta: {fields['reporter_name']}")

    victim = MissingVictim(
        external_id=external_id,
        full_name=fields["full_name"],
        nombre_completo=fields["full_name"],
        age=parsed_age,
        edad=parsed_age,
        gender=fields["gender"],
        sexo=fields["gender"],
        last_known_location=fields["last_known_location"],
        last_seen_date=_clean(last_seen_date) or None,
        distinguishing_marks=" · ".join(marks_parts) if marks_parts else None,
        descripcion_fisica=_clean(distinguishing_marks) or None,
        reporter_contact=fields["reporter_contact"],
        reference_photo_path=str(photo_path),
        photo_url=None,
        source_estado="reporte-comunidad",
        status=MissingStatus.DESAPARECIDO,
    )
    session.add(victim)
    await session.flush()

    event_payload = victim_to_event(victim)
    event_payload["local_photo_url"] = f"/photos/{photo_path.name}"
    event_payload["cedula"] = fields["cedula"]
    event_payload["is_local_report"] = True

    await missing_updates_bus.publish("new_missing", event_payload)
    await victim_room_manager.broadcast("new_missing", event_payload)

    return {
        "id": victim.id,
        "external_id": external_id,
        "full_name": victim.full_name,
        "status": victim.status.value,
        "local_photo_url": event_payload["local_photo_url"],
        "message": "Reporte registrado. La comunidad SAR revisará el caso.",
    }


async def create_building_report(
    session: AsyncSession,
    *,
    name: str,
    address: str,
    city: str,
    damage_level: str,
    reporter_contact: str,
    photo: UploadFile,
    zone: Optional[str] = None,
    notes: Optional[str] = None,
    reporter_name: Optional[str] = None,
    lat: Optional[str] = None,
    lng: Optional[str] = None,
) -> dict[str, Any]:
    fields = _validate_building_fields(
        name=name,
        address=address,
        city=city,
        damage_level=damage_level,
        reporter_contact=reporter_contact,
        reporter_name=reporter_name,
        zone=zone,
        notes=notes,
    )
    photo_data, ext = await _read_photo(photo)
    building_id = str(uuid.uuid4())
    photo_path = BUILDING_PHOTOS_DIR / f"{building_id}{ext}"
    photo_path.write_bytes(photo_data)
    now = datetime.now(timezone.utc)

    report = BuildingReport(
        id=building_id,
        name=fields["name"],
        address=fields["address"],
        city=fields["city"],
        zone=fields["zone"],
        damage_level=fields["damage_level"],
        notes=fields["notes"],
        lat=_parse_optional_float(lat),
        lng=_parse_optional_float(lng),
        photo_path=str(photo_path),
        reporter_contact=fields["reporter_contact"],
        reporter_name=fields["reporter_name"],
        status="reporte_comunidad",
    )
    session.add(report)
    await session.flush()

    payload = serialize_building_report(report)
    await missing_updates_bus.publish("terremoto_building", payload)
    await victim_room_manager.broadcast("terremoto_building", payload)

    return {
        "id": building_id,
        "name": report.name,
        "damage_level": report.damage_level,
        "local_photo_url": payload["local_photo_url"],
        "message": "Edificio reportado. Aparecerá en el mapa y listado local.",
    }


def serialize_building_report(report: BuildingReport) -> dict[str, Any]:
    photo_name = Path(report.photo_path).name
    local_url = f"/building-photos/{photo_name}"
    updated = report.created_at.isoformat() if report.created_at else datetime.now(timezone.utc).isoformat()
    return {
        "id": report.id,
        "name": report.name,
        "address": report.address,
        "city": report.city,
        "zone": report.zone,
        "lat": report.lat,
        "lng": report.lng,
        "damage_level": report.damage_level,
        "status": report.status,
        "main_photo_url": local_url,
        "local_photo_url": local_url,
        "display_photo_url": local_url,
        "has_local_photo": True,
        "notes": report.notes,
        "reporter_contact": report.reporter_contact,
        "reporter_name": report.reporter_name,
        "is_local_report": True,
        "last_updated_at": updated,
        "created_at": updated,
    }


async def list_building_reports(session: AsyncSession, limit: int = 200) -> list[dict[str, Any]]:
    result = await session.execute(
        select(BuildingReport).order_by(BuildingReport.created_at.desc()).limit(limit)
    )
    return [serialize_building_report(row) for row in result.scalars().all()]