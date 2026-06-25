"""Modelos SQLAlchemy — 5 vías de entrada consensuadas + base de desaparecidos."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MissingStatus(str, enum.Enum):
    DESAPARECIDO = "desaparecido"
    LOCALIZADO = "localizado"
    FALLECIDO = "fallecido"


class SosVitalStatus(str, enum.Enum):
    ATRAPADO = "Atrapado"
    A_SALVO = "A salvo"


class EvidenceStatus(str, enum.Enum):
    PENDIENTE = "pendiente"
    PROCESANDO = "procesando"
    PROCESADO = "procesado"
    ERROR = "error"


class FeedStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    OFFLINE = "offline"


class MissingPerson(Base):
    """Base externa de desaparecidos (solo lectura local, sin scraping automático)."""

    __tablename__ = "missing_victims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    nombre_completo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    edad: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sexo: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    height_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estatura_estimada_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    clasificacion_tatuajes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    distinguishing_marks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tattoo_descriptions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tattoo_embeddings: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    reference_photo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    last_known_location: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[MissingStatus] = mapped_column(
        Enum(MissingStatus, name="missing_status", values_callable=lambda x: [e.value for e in x]),
        default=MissingStatus.DESAPARECIDO,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


MissingVictim = MissingPerson


class MedicalTriage(Base):
    """Pacientes 'John Doe' ingresados inconscientes — triaje hospitalario oficial."""

    __tablename__ = "medical_triage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hospital_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ward: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    photo_path: Mapped[str] = mapped_column(String(512), nullable=False)
    estatura_estimada_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tatuajes_clasificados: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tattoo_embeddings: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    clinical_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    matched_victim_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    match_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoluntaryCamera(Base):
    """Cámaras voluntarias 'Protege tu Barrio' — protocolo ECU911 con consentimiento."""

    __tablename__ = "voluntary_cameras"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    condominium_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    zone: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    terms_version: Mapped[str] = mapped_column(String(32), default="2026-06-01", nullable=False)
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[FeedStatus] = mapped_column(
        Enum(FeedStatus, name="voluntary_feed_status"),
        default=FeedStatus.ACTIVE,
        nullable=False,
    )
    last_detection_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrowdsourcedEvidence(Base):
    """Videos subidos voluntariamente por ciudadanos con avistamientos."""

    __tablename__ = "crowdsourced_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    uploader_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    video_path: Mapped[str] = mapped_column(String(512), nullable=False)
    location_description: Mapped[str] = mapped_column(String(512), nullable=False)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    processing_status: Mapped[EvidenceStatus] = mapped_column(
        Enum(EvidenceStatus, name="evidence_status"),
        default=EvidenceStatus.PENDIENTE,
        nullable=False,
    )
    detections_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DroneTelemetry(Base):
    """Flujos de fotogrametría aérea 3D autorizada sobre zonas colapsadas."""

    __tablename__ = "drone_telemetry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operator_name: Mapped[str] = mapped_column(String(255), nullable=False)
    authorization_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    stream_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    zone: Mapped[str] = mapped_column(String(255), nullable=False)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    altitude_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    photogrammetry_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[FeedStatus] = mapped_column(
        Enum(FeedStatus, name="drone_feed_status"),
        default=FeedStatus.ACTIVE,
        nullable=False,
    )
    last_frame_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    detections_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SosSignal(Base):
    """Botón de pánico P2P — coordenadas GPS voluntarias desde la PWA."""

    __tablename__ = "sos_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    device_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    vital_status: Mapped[SosVitalStatus] = mapped_column(
        Enum(SosVitalStatus, name="sos_vital_status"),
        nullable=False,
        index=True,
    )
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )