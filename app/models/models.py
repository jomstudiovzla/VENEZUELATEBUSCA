"""Modelos logísticos — Red de Esperanza."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ShelterType(str, enum.Enum):
    REFUGIO = "refugio"
    HOSPITAL = "hospital"
    ACOPIO = "acopio"


class InventoryStatus(str, enum.Enum):
    NECESITADO = "Necesitado"
    EXCEDENTE = "Excedente"


class MissionStatus(str, enum.Enum):
    ABIERTA = "abierta"
    ACEPTADA = "aceptada"
    EN_CURSO = "en_curso"
    COMPLETADA = "completada"


class ReportStatus(str, enum.Enum):
    ACTIVO = "activo"
    POSIBLE_MATCH = "posible_match"
    ENCONTRADO = "encontrado"
    CERRADO = "cerrado"


class VictimStatus(str, enum.Enum):
    DESAPARECIDO = "desaparecido"
    LOCALIZADO = "localizado"
    FALLECIDO = "fallecido"


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    VERIFICADOR_ACOPIO = "verificador_acopio"
    PARAMEDICO = "paramedico"
    VOLUNTARIO = "voluntario"
    FAMILIAR = "familiar"


class MapPointCategory(str, enum.Enum):
    ACOPIO = "acopio"
    HOSPITAL = "hospital"
    REFUGIO = "refugio"
    ENERGIA = "energia"
    SENAL = "senal"
    SUMINISTROS = "suministros"
    MEDICA = "medica"
    PELIGRO = "peligro"
    MOVILIDAD = "movilidad"
    OFRECEN = "ofrecen"
    SOLICITAN = "solicitan"
    PLAZA = "plaza"
    OTRO = "otro"


class DestinationType(str, enum.Enum):
    HOSPITAL = "hospital"
    ACOPIO = "acopio"
    REFUGIO = "refugio"
    PLAZA = "plaza"
    OTRO = "otro"


class VerificationStatus(str, enum.Enum):
    PENDIENTE = "pendiente"
    VERIFICADO = "verificado"
    RECHAZADO = "rechazado"
    SUSPENDIDO = "suspendido"


class TipoEstructura(str, enum.Enum):
    COLAPSADO = "colapsado"
    REFUGIO = "refugio"
    HOSPITAL = "hospital"
    CENTRO_ACOPIO = "centro_acopio"


class Shelter(Base):
    __tablename__ = "shelters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    shelter_type: Mapped[ShelterType] = mapped_column(
        Enum(ShelterType, name="shelter_type"), default=ShelterType.REFUGIO, nullable=False
    )
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    services_offered: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    max_capacity: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    current_occupancy: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), default=VerificationStatus.PENDIENTE.value, nullable=False, index=True
    )
    submitted_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    submitted_by_contact: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    submitted_by_org: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    inventory_items: Mapped[list["Inventory"]] = relationship(back_populates="shelter")
    survivors: Mapped[list["Survivor"]] = relationship(back_populates="shelter")


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    shelter_id: Mapped[str] = mapped_column(String(36), ForeignKey("shelters.id"), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="unidades", nullable=False)
    status: Mapped[InventoryStatus] = mapped_column(
        Enum(InventoryStatus, name="inventory_status"), nullable=False, index=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    shelter: Mapped["Shelter"] = relationship(back_populates="inventory_items")


class Survivor(Base):
    __tablename__ = "survivors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_sync_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True, index=True)
    shelter_id: Mapped[str] = mapped_column(String(36), ForeignKey("shelters.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), default="Desconocido", nullable=False)
    estado_medico: Mapped[str] = mapped_column(String(128), default="estable", nullable=False)
    caracteristicas_fisicas: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    matched_report_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    synced_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    shelter: Mapped["Shelter"] = relationship(back_populates="survivors")


class MissingVictim(Base):
    """Registros sincronizados desde desaparecidosterremotovenezuela.com."""

    __tablename__ = "missing_victims"
    __table_args__ = (
        Index("ix_missing_victims_nombre_completo", "nombre_completo"),
        Index("ix_missing_victims_cedula", "cedula"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    nombre_completo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    cedula: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    edad: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sexo: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    height_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estatura_estimada_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    distinguishing_marks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    descripcion_fisica: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tattoo_descriptions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    clasificacion_tatuajes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    reference_photo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    last_known_location: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_estado: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_updated_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reporter_contact: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ingreso_shelter_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    ingreso_shelter_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ingreso_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ingreso_notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estado_encontrado: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ubicacion_encontrado: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    descripcion_atencion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmacion_postmortem: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acta_defuncion_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    biometric_embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    candado_forense: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=VictimStatus.DESAPARECIDO.value, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Building(Base):
    """Registro de edificaciones afectadas — mapeo ciudadano en tiempo real."""

    __tablename__ = "buildings"
    __table_args__ = (Index("ix_buildings_coords", "latitud", "longitud"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nombre_edificio: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    tipo_estructura: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direccion_texto: Mapped[str] = mapped_column(String(512), nullable=False)
    latitud: Mapped[float] = mapped_column(Float, nullable=False)
    longitud: Mapped[float] = mapped_column(Float, nullable=False)
    necesidades_urgentes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estado_verificacion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    reportado_por: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contacto_reportante: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MissingReport(Base):
    __tablename__ = "missing_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seeker_name: Mapped[str] = mapped_column(String(255), nullable=False)
    seeker_contact: Mapped[str] = mapped_column(String(64), nullable=False)
    missing_person_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_location: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    physical_traits: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status"), default=ReportStatus.ACTIVO, nullable=False, index=True
    )
    matched_survivor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Mission(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    mission_type: Mapped[str] = mapped_column(String(64), default="rescate", nullable=False)
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    shelter_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("shelters.id"), nullable=True)
    status: Mapped[MissionStatus] = mapped_column(
        Enum(MissionStatus, name="mission_status"), default=MissionStatus.ABIERTA, nullable=False, index=True
    )
    volunteer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    volunteer_contact: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    parent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MapPoint(Base):
    """Puntos unificados del mapa (Punto de Apoyo + locales + refugios)."""

    __tablename__ = "map_points"
    __table_args__ = (Index("ix_map_points_source_ext", "source", "external_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    point_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contact: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VolunteerTestimony(Base):
    """Testimonio de voluntario: escuchó a alguien decir su nombre en zona de rescate."""

    __tablename__ = "volunteer_testimonies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    volunteer_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    volunteer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    heard_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    heard_cedula: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    location_text: Mapped[str] = mapped_column(String(512), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    gps_accuracy_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    destination_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    destination_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    destination_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    estado_persona: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    matched_victim_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    firebase_pushed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LivePosition(Base):
    """Posición GPS en tiempo real de voluntarios/paramédicos."""

    __tablename__ = "live_positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    heading: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mission_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())