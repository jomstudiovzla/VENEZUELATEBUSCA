"""Modelos logísticos — Red de Esperanza."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
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
    max_capacity: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    current_occupancy: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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