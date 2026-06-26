"""Motor de Búsqueda de Triaje Rápido — paramédicos y hospitales de campaña."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.connection_manager import dashboard_ws
from app.core.database import get_session
from app.core.forensic_auth import FORENSIC_ROLE, is_forensic_admin
from app.core.victims_database import get_victims_session, normalize_cedula
from app.models.models import MissingVictim, Shelter, VictimStatus
from app.services.victims_sync import fetch_api_stats

logger = logging.getLogger(__name__)
router = APIRouter(tags=["triaje"])

_STATUS_LABELS = {
    VictimStatus.DESAPARECIDO.value: "DESAPARECIDO",
    VictimStatus.LOCALIZADO.value: "LOCALIZADO",
    VictimStatus.FALLECIDO.value: "FALLECIDO",
}

_REVERSIBLE_FROM_FALLECIDO = {
    VictimStatus.DESAPARECIDO.value,
    VictimStatus.LOCALIZADO.value,
}


_ESTADOS_ENCONTRADO = {
    "estable",
    "herido_leve",
    "herido_grave",
    "critico",
    "inconsciente",
    "quemaduras",
    "trauma",
    "desorientado",
    "fallecido_en_sitio",
}


class VictimStatusUpdate(BaseModel):
    status: str = Field(..., description="localizado | desaparecido | fallecido")
    shelter_id: Optional[str] = Field(None, description="Hospital/refugio donde ingresa el paciente")
    estado_encontrado: Optional[str] = Field(
        None,
        description="Condición al ser encontrado: estable, herido_leve, herido_grave, critico, inconsciente…",
    )
    ubicacion_encontrado: Optional[str] = Field(None, description="Lugar exacto donde fue localizado")
    descripcion_atencion: Optional[str] = Field(
        None,
        description="Descripción para atención posterior, tratamientos o ayuda requerida",
    )
    notas: Optional[str] = Field(None, description="Notas clínicas adicionales de ingreso")
    confirmacion_postmortem: bool = Field(
        False,
        description="True solo con rol ADMIN_FORENSE para alterar un registro FALLECIDO",
    )
    acta_defuncion_hash: Optional[str] = Field(None, description="Hash del acta física / formulario rosa DVI")


def _resolve_photo(victim: MissingVictim) -> Optional[str]:
    if victim.reference_photo_path:
        name = Path(victim.reference_photo_path).name
        local = Path(__file__).resolve().parents[2] / "reference_photos" / name
        if local.is_file():
            return f"/reference_photos/{name}"
    if victim.photo_url:
        return victim.photo_url
    return None


def _tattoos(victim: MissingVictim) -> list[str]:
    raw = victim.clasificacion_tatuajes or victim.tattoo_descriptions
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, dict):
        return [str(v) for v in raw.values() if v]
    return [str(raw)]


def _estatura(victim: MissingVictim) -> Optional[str]:
    cm = victim.estatura_estimada_cm or victim.height_cm
    if cm is None:
        return None
    if cm < 3:
        return f"{cm:.2f} m"
    return f"{cm:.0f} cm"


def _is_locked(victim: MissingVictim) -> bool:
    return bool(getattr(victim, "candado_forense", False)) or victim.status.lower() == VictimStatus.FALLECIDO.value


def _victim_card(victim: MissingVictim) -> dict[str, Any]:
    nombre = victim.nombre_completo or victim.full_name
    edad = victim.edad if victim.edad is not None else victim.age
    locked = _is_locked(victim)
    return {
        "id": victim.id,
        "external_id": victim.external_id,
        "nombre_completo": nombre,
        "cedula": victim.cedula,
        "edad": edad,
        "sexo": victim.sexo or victim.gender,
        "fotografia": _resolve_photo(victim),
        "tatuajes": _tattoos(victim),
        "estatura": _estatura(victim),
        "descripcion_fisica": victim.descripcion_fisica or victim.distinguishing_marks,
        "ultima_ubicacion": victim.last_known_location,
        "estado": _STATUS_LABELS.get(victim.status.lower(), victim.status.upper()),
        "estado_raw": victim.status.lower(),
        "is_locked": locked,
        "confirmacion_postmortem": victim.confirmacion_postmortem,
        "acta_defuncion_hash": victim.acta_defuncion_hash,
        "ingreso_shelter_id": victim.ingreso_shelter_id,
        "ingreso_shelter_name": victim.ingreso_shelter_name,
        "ingreso_at": victim.ingreso_at.isoformat() if victim.ingreso_at else None,
        "ingreso_notas": victim.ingreso_notas,
        "estado_encontrado": victim.estado_encontrado,
        "ubicacion_encontrado": victim.ubicacion_encontrado,
        "descripcion_atencion": victim.descripcion_atencion,
    }


def _parse_estado_encontrado(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "estable": "estable",
        "leve": "herido_leve",
        "herido_leve": "herido_leve",
        "grave": "herido_grave",
        "herido_grave": "herido_grave",
        "critico": "critico",
        "crítico": "critico",
        "inconsciente": "inconsciente",
        "quemaduras": "quemaduras",
        "trauma": "trauma",
        "desorientado": "desorientado",
        "fallecido_en_sitio": "fallecido_en_sitio",
    }
    if key not in aliases and key not in _ESTADOS_ENCONTRADO:
        raise HTTPException(
            400,
            "Estado al encontrar inválido. Use: estable, herido_leve, herido_grave, critico, "
            "inconsciente, quemaduras, trauma, desorientado",
        )
    return aliases.get(key, key)


def _parse_status(value: str) -> str:
    key = value.strip().lower()
    aliases = {
        "desaparecido": VictimStatus.DESAPARECIDO.value,
        "localizado": VictimStatus.LOCALIZADO.value,
        "ingresado": VictimStatus.LOCALIZADO.value,
        "encontrado": VictimStatus.LOCALIZADO.value,
        "fallecido": VictimStatus.FALLECIDO.value,
    }
    if key not in aliases:
        raise HTTPException(400, "Estado inválido. Use: desaparecido, localizado o fallecido")
    return aliases[key]


def _enforce_forensic_lock(
    victim: MissingVictim,
    new_status: str,
    forensic_role: str | None,
    confirmacion: bool,
) -> None:
    """Candado forense: FALLECIDO es inmutable sin ADMIN_FORENSE + confirmación."""
    current = victim.status.lower()
    if current != VictimStatus.FALLECIDO.value:
        return
    if new_status == current:
        return
    if new_status not in _REVERSIBLE_FROM_FALLECIDO:
        return
    if is_forensic_admin(forensic_role) and confirmacion:
        return
    raise HTTPException(
        403,
        "Registro bloqueado: expediente FALLECIDO. Requiere rol ADMIN_FORENSE "
        "y confirmacion_postmortem=true (acta forense oficial).",
    )


def _build_acta_hash(victim_id: int, provided: str | None) -> str:
    if provided and provided.strip():
        return provided.strip()
    stamp = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(f"DVI-ACTA:{victim_id}:{stamp}".encode()).hexdigest()
    return digest[:64]


async def _apply_status_change(
    victim: MissingVictim,
    payload: VictimStatusUpdate,
    new_status: str,
    forensic_role: str | None,
    logistics_session: AsyncSession,
) -> tuple[dict[str, Any], str | None]:
    _enforce_forensic_lock(victim, new_status, forensic_role, payload.confirmacion_postmortem)

    shelter_name = None
    if new_status in (VictimStatus.LOCALIZADO.value, VictimStatus.FALLECIDO.value):
        if not payload.shelter_id:
            raise HTTPException(400, "Debe indicar el hospital/refugio (shelter_id) para registrar el ingreso")
        if new_status == VictimStatus.LOCALIZADO.value and not payload.estado_encontrado:
            raise HTTPException(400, "Debe indicar el estado en que fue encontrado (estado_encontrado)")
        shelter = await logistics_session.get(Shelter, payload.shelter_id)
        if not shelter:
            raise HTTPException(404, "Hospital/refugio no encontrado")
        shelter_name = shelter.name
        victim.ingreso_shelter_id = payload.shelter_id
        victim.ingreso_shelter_name = shelter_name
        victim.ingreso_at = datetime.now(timezone.utc)
        victim.ingreso_notas = payload.notas
        victim.estado_encontrado = _parse_estado_encontrado(payload.estado_encontrado)
        victim.ubicacion_encontrado = (payload.ubicacion_encontrado or "").strip() or None
        victim.descripcion_atencion = (payload.descripcion_atencion or "").strip() or None

    if new_status == VictimStatus.FALLECIDO.value:
        victim.confirmacion_postmortem = False
        victim.acta_defuncion_hash = None
        victim.candado_forense = True
    elif new_status == VictimStatus.LOCALIZADO.value:
        victim.candado_forense = False
    elif (
        victim.status.lower() == VictimStatus.FALLECIDO.value
        and new_status in _REVERSIBLE_FROM_FALLECIDO
        and is_forensic_admin(forensic_role)
        and payload.confirmacion_postmortem
    ):
        victim.confirmacion_postmortem = True
        victim.acta_defuncion_hash = _build_acta_hash(victim.id, payload.acta_defuncion_hash)

    victim.status = new_status
    victim.updated_at = datetime.now(timezone.utc)

    card = _victim_card(victim)
    from app.services.firebase_bridge import push_victim_update_to_firebase
    await push_victim_update_to_firebase(card)

    await dashboard_ws.broadcast(
        "victim_status_updated",
        {
            "id": victim.id,
            "nombre_completo": card["nombre_completo"],
            "cedula": card["cedula"],
            "estado": card["estado"],
            "estado_raw": new_status,
            "fotografia": card["fotografia"],
            "is_locked": card["is_locked"],
            "confirmacion_postmortem": card["confirmacion_postmortem"],
            "acta_defuncion_hash": card["acta_defuncion_hash"],
            "ingreso_shelter_name": shelter_name,
            "ingreso_notas": payload.notas,
            "estado_encontrado": victim.estado_encontrado,
            "ubicacion_encontrado": victim.ubicacion_encontrado,
            "descripcion_atencion": victim.descripcion_atencion,
        },
    )
    return card, shelter_name


@router.get("/api/victims/stats")
async def victims_stats(session: AsyncSession = Depends(get_victims_session)):
    """Estadísticas de la BD local y la fuente externa."""
    total = await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
    desaparecidos = await session.scalar(
        select(func.count()).where(MissingVictim.status == VictimStatus.DESAPARECIDO.value)
    ) or 0
    localizados = await session.scalar(
        select(func.count()).where(MissingVictim.status == VictimStatus.LOCALIZADO.value)
    ) or 0
    fallecidos = await session.scalar(
        select(func.count()).where(MissingVictim.status == VictimStatus.FALLECIDO.value)
    ) or 0
    con_cedula = await session.scalar(
        select(func.count()).where(MissingVictim.cedula.is_not(None), MissingVictim.cedula != "")
    ) or 0
    ingresos_hoy = await session.scalar(
        select(func.count()).where(
            MissingVictim.ingreso_at.is_not(None),
            func.date(MissingVictim.ingreso_at) == func.date("now"),
        )
    ) or 0
    bloqueados = await session.scalar(
        select(func.count()).where(
            MissingVictim.status == VictimStatus.FALLECIDO.value,
            MissingVictim.confirmacion_postmortem == False,  # noqa: E712
        )
    ) or 0

    api = await fetch_api_stats()
    return {
        "local_db": {
            "total": total,
            "desaparecidos": desaparecidos,
            "localizados": localizados,
            "fallecidos": fallecidos,
            "bloqueados_forenses": bloqueados,
            "con_cedula": con_cedula,
            "ingresos_hoy": ingresos_hoy,
        },
        "fuente_externa": api,
        "forensic_role_required": FORENSIC_ROLE,
    }


@router.get("/api/victims/search")
async def search_victims(
    q: str = Query(..., min_length=1, description="Cédula exacta o fragmento de nombre"),
    limit: int = Query(25, ge=1, le=50),
    hospital_id: Optional[str] = Query(None, description="Filtrar solo pacientes ingresados en este hospital"),
    solo_ingresados: bool = Query(False, description="Mostrar solo pacientes ya ingresados en algún hospital"),
    session: AsyncSession = Depends(get_victims_session),
):
    """Búsqueda instantánea por cédula o nombre para triaje médico."""
    term = q.strip()
    if len(term) < 1:
        return {"items": [], "count": 0, "query": term}
    if len(term) < 2 and not term.isdigit():
        return {"items": [], "count": 0, "query": term}

    name_col = func.coalesce(MissingVictim.nombre_completo, MissingVictim.full_name)
    cedula_norm = normalize_cedula(term)
    clauses = [name_col.ilike(f"%{term}%")]

    if cedula_norm:
        clauses.append(MissingVictim.cedula == cedula_norm)
        if len(cedula_norm) >= 6:
            clauses.append(MissingVictim.descripcion_fisica.ilike(f"%{cedula_norm}%"))
            clauses.append(MissingVictim.distinguishing_marks.ilike(f"%{cedula_norm}%"))

    stmt = select(MissingVictim).where(or_(*clauses))

    if hospital_id:
        stmt = stmt.where(MissingVictim.ingreso_shelter_id == hospital_id)
    elif solo_ingresados:
        stmt = stmt.where(MissingVictim.ingreso_shelter_id.is_not(None))

    if cedula_norm:
        stmt = stmt.order_by((MissingVictim.cedula == cedula_norm).desc(), MissingVictim.id.desc())
    else:
        stmt = stmt.order_by(MissingVictim.id.desc())

    rows = (await session.execute(stmt.limit(limit))).scalars().all()
    items = [_victim_card(v) for v in rows]
    return {"items": items, "count": len(items), "query": term}


@router.get("/api/victims/{victim_id}")
async def get_victim(victim_id: int, session: AsyncSession = Depends(get_victims_session)):
    victim = await session.get(MissingVictim, victim_id)
    if not victim:
        raise HTTPException(404, "Persona no encontrada")
    return _victim_card(victim)


@router.patch("/api/victims/{victim_id}/status")
async def update_victim_status_forensic(
    victim_id: int,
    payload: VictimStatusUpdate,
    x_forensic_role: str | None = Header(None, alias="X-Forensic-Role"),
    victims_session: AsyncSession = Depends(get_victims_session),
    logistics_session: AsyncSession = Depends(get_session),
):
    """Candado forense DVI — actualización de estado con validación post-mortem."""
    victim = await victims_session.get(MissingVictim, victim_id)
    if not victim:
        raise HTTPException(404, "Persona no encontrada en la base de datos")

    new_status = _parse_status(payload.status)
    card, _ = await _apply_status_change(victim, payload, new_status, x_forensic_role, logistics_session)
    await victims_session.flush()
    return card


@router.patch("/api/victims/{victim_id}")
async def update_victim_status(
    victim_id: int,
    payload: VictimStatusUpdate,
    x_forensic_role: str | None = Header(None, alias="X-Forensic-Role"),
    victims_session: AsyncSession = Depends(get_victims_session),
    logistics_session: AsyncSession = Depends(get_session),
):
    """Alias retrocompatible — misma lógica de candado forense."""
    return await update_victim_status_forensic(
        victim_id, payload, x_forensic_role, victims_session, logistics_session
    )


@router.post("/api/victims/sync", status_code=202)
async def trigger_victims_sync():
    """Dispara sincronización manual con la API externa."""
    from app.services.victims_sync import sync_victims_incremental

    stats = await sync_victims_incremental()
    return {"status": "synced", **stats}