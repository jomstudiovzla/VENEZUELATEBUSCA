"""Utilidades DVI: perfil forense y clasificación ANSI/NIST ITL de tatuajes."""

from __future__ import annotations

import re
from typing import Any, Optional

from data_ingestor import extract_cedula
from database import MissingPerson, MissingStatus

# Categorías simplificadas alineadas con ANSI/NIST-ITL 1-2011 (Type-10 metadata)
NIST_TATTOO_CLASSES = (
    "ANIMAL",
    "SYMBOL",
    "PLANT",
    "TEXT",
    "HUMAN_FIGURE",
    "OBJECT",
    "ABSTRACT",
    "UNKNOWN",
)

_CLASS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ANIMAL", re.compile(r"\b(perro|gato|león|leon|tigre|águila|aguila|serpiente|mariposas?|lobo|caballo|animal)\b", re.I)),
    ("PLANT", re.compile(r"\b(flores?|rosa|margarita|planta|hoja|árbol|arbol|tribal vegetal)\b", re.I)),
    ("SYMBOL", re.compile(r"\b(cruz|estrella|infinito|símbolo|simbolo|ankh|yin.?yang|trébol|trebol|corazón|corazon)\b", re.I)),
    ("TEXT", re.compile(r"\b(letras?|nombre|frase|texto|números?|numeros?|iniciales)\b", re.I)),
    ("HUMAN_FIGURE", re.compile(r"\b(cara|rostro|calaca|virgen|santa|ángel|angel|silueta humana|persona)\b", re.I)),
    ("OBJECT", re.compile(r"\b(arma|reloj|llave|ancla|cadena|corona|dados|objeto)\b", re.I)),
    ("ABSTRACT", re.compile(r"\b(geométrico|geometrico|tribal|mandala|patrón|patron|abstracto|líneas|lineas)\b", re.I)),
]


def classify_tattoo_text(description: Optional[str]) -> dict[str, Any]:
    if not description or not description.strip():
        return {
            "nist_standard": "ANSI/NIST-ITL 1-2011",
            "primary_class": "UNKNOWN",
            "secondary_classes": [],
            "confidence": 0.0,
            "raw_description": "",
        }

    text = description.strip()
    matches: list[str] = []
    for label, pattern in _CLASS_PATTERNS:
        if pattern.search(text):
            matches.append(label)

    primary = matches[0] if matches else "UNKNOWN"
    return {
        "nist_standard": "ANSI/NIST-ITL 1-2011",
        "primary_class": primary,
        "secondary_classes": matches[1:4],
        "confidence": min(0.95, 0.45 + 0.15 * len(matches)),
        "raw_description": text,
    }


def build_clasificacion_tatuajes(person: MissingPerson) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    descriptions = person.tattoo_descriptions or []
    if not descriptions and person.distinguishing_marks and "tatuaj" in person.distinguishing_marks.lower():
        descriptions = [person.distinguishing_marks]

    for idx, desc in enumerate(descriptions):
        classified = classify_tattoo_text(desc)
        classified["index"] = idx
        entries.append(classified)

    if not entries and person.descripcion_fisica and "tatuaj" in person.descripcion_fisica.lower():
        entries.append({**classify_tattoo_text(person.descripcion_fisica), "index": 0})

    return entries


def build_descripcion_fisica(person: MissingPerson) -> str:
    if person.descripcion_fisica:
        return person.descripcion_fisica

    parts: list[str] = []
    if person.distinguishing_marks:
        parts.append(person.distinguishing_marks)
    if person.hair_description:
        parts.append(f"Pelo: {person.hair_description}")
    if person.skin_tone:
        parts.append(f"Piel: {person.skin_tone}")
    if person.clothing_description:
        parts.append(f"Vestimenta: {person.clothing_description}")
    return " · ".join(parts) if parts else "Sin descripción física registrada."


def sync_forensic_fields(person: MissingPerson) -> None:
    """Sincroniza columnas forenses DVI desde datos de ingesta."""
    if not person.nombre_completo:
        person.nombre_completo = person.full_name
    if person.edad is None and person.age is not None:
        person.edad = person.age
    if not person.sexo and person.gender:
        person.sexo = person.gender
    person.descripcion_fisica = build_descripcion_fisica(person)
    if person.estatura_estimada_cm is None and person.height_cm is not None:
        person.estatura_estimada_cm = person.height_cm
    person.clasificacion_tatuajes = build_clasificacion_tatuajes(person)


def person_to_forensic_dict(person: MissingPerson) -> dict[str, Any]:
    sync_forensic_fields(person)
    photo = person.photo_url
    if person.reference_photo_path:
        fname = person.reference_photo_path.split("/")[-1]
        photo = f"/photos/{fname}"
    elif not photo and person.reference_photo_path:
        photo = f"/{person.reference_photo_path}"

    cedula = (
        extract_cedula(person.last_known_location)
        or extract_cedula(person.distinguishing_marks)
        or extract_cedula(person.reporter_contact)
    )
    return {
        "id": person.id,
        "external_id": person.external_id,
        "cedula": cedula,
        "nombre_completo": person.nombre_completo or person.full_name,
        "edad": person.edad if person.edad is not None else person.age,
        "sexo": person.sexo or person.gender,
        "descripcion_fisica": person.descripcion_fisica,
        "estatura_estimada_cm": person.estatura_estimada_cm or person.height_cm,
        "clasificacion_tatuajes": person.clasificacion_tatuajes or [],
        "estado": person.status.value.upper(),
        "estado_raw": person.status.value,
        "photo_url": photo,
        "last_known_location": person.last_known_location,
        "last_seen_date": person.last_seen_date,
        "reporter_contact": person.reporter_contact,
        "tattoo_descriptions": person.tattoo_descriptions or [],
    }