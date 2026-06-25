"""Biometría blanda forense — Zhang + SIFT/ANSI-NIST para triaje hospitalario."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import MedicalTriage, MissingStatus, MissingVictim

logger = logging.getLogger(__name__)

ANSI_NIST_CATEGORIES = (
    "animal",
    "símbolo",
    "planta",
    "texto",
    "figura_humana",
    "objeto",
    "abstracto",
    "desconocido",
)


@dataclass
class BiometricAnalysis:
    estatura_estimada_cm: float
    estatura_confidence: float
    tatuajes_clasificados: list[dict[str, Any]]
    tattoo_embeddings: list[list[float]]


@dataclass
class VictimMatch:
    victim_id: int
    victim_name: str
    confidence: float
    tattoo_similarity: float
    height_delta_cm: float


class BiometricsService:
    """Analiza fotos de John Doe y cruza con la base de desaparecidos."""

    def __init__(self) -> None:
        self._height_estimator = None
        self._tattoo_analyzer = None

    def _height(self):
        if self._height_estimator is None:
            from height_estimator import HeightEstimator

            self._height_estimator = HeightEstimator()
        return self._height_estimator

    def _tattoos(self):
        if self._tattoo_analyzer is None:
            from tattoo_analyzer import TattooAnalyzer

            self._tattoo_analyzer = TattooAnalyzer()
        return self._tattoo_analyzer

    def _classify_tattoo_region(self, embedding: np.ndarray) -> str:
        norm = float(np.linalg.norm(embedding))
        if norm < 0.01:
            return "desconocido"
        bucket = int(abs(embedding[0] * 7)) % len(ANSI_NIST_CATEGORIES)
        return ANSI_NIST_CATEGORIES[bucket]

    def analyze_medical_photo(self, image_path: Path) -> BiometricAnalysis:
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"Imagen inválida: {image_path}")

        h, w = image.shape[:2]
        height_est = self._height().estimate_height(
            image,
            (int(w * 0.35), int(h * 0.1), int(w * 0.65), int(h * 0.95)),
        )
        tattoo_regions = self._tattoos().extract_from_crop(image)

        clasificados: list[dict[str, Any]] = []
        embeddings: list[list[float]] = []
        for idx, region in enumerate(tattoo_regions):
            category = self._classify_tattoo_region(region.embedding)
            clasificados.append(
                {
                    "id": idx + 1,
                    "categoria_ansi_nist": category,
                    "bbox": list(region.bbox),
                    "keypoints_sift": region.sift_keypoints,
                    "confianza": round(min(1.0, region.sift_keypoints / 50.0), 2),
                }
            )
            embeddings.append(region.embedding.tolist())

        return BiometricAnalysis(
            estatura_estimada_cm=round(height_est.height_cm, 1),
            estatura_confidence=round(height_est.confidence, 2),
            tatuajes_clasificados=clasificados,
            tattoo_embeddings=embeddings,
        )

    async def match_triage_against_missing(
        self,
        session: AsyncSession,
        triage: MedicalTriage,
    ) -> Optional[VictimMatch]:
        if not triage.estatura_estimada_cm and not triage.tattoo_embeddings:
            return None

        result = await session.execute(
            select(MissingVictim).where(MissingVictim.status == MissingStatus.DESAPARECIDO)
        )
        victims = list(result.scalars().all())
        if not victims:
            return None

        best: Optional[VictimMatch] = None
        triage_embeddings = triage.tattoo_embeddings or []

        for victim in victims:
            tattoo_sim = 0.0
            if triage_embeddings and victim.tattoo_embeddings:
                from tattoo_analyzer import TattooAnalyzer

                analyzer = TattooAnalyzer()
                for te in triage_embeddings[:3]:
                    for ve in (victim.tattoo_embeddings or [])[:3]:
                        a = np.array(te, dtype=np.float32)
                        b = np.array(ve, dtype=np.float32)
                        tattoo_sim = max(tattoo_sim, TattooAnalyzer.cosine_similarity(a, b))

            height_delta = 0.0
            height_score = 0.5
            registered = victim.estatura_estimada_cm or victim.height_cm
            if triage.estatura_estimada_cm and registered:
                height_delta = abs(triage.estatura_estimada_cm - registered)
                tol = settings.height_tolerance_cm
                height_score = max(0.0, 1.0 - height_delta / (tol * 2)) if height_delta <= tol * 2 else 0.1

            confidence = float(np.clip(0.55 * tattoo_sim + 0.45 * height_score, 0.0, 1.0))
            if confidence < settings.tattoo_match_threshold * 0.85:
                continue

            candidate = VictimMatch(
                victim_id=victim.id,
                victim_name=victim.full_name,
                confidence=round(confidence, 3),
                tattoo_similarity=round(tattoo_sim, 3),
                height_delta_cm=round(height_delta, 1),
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate

        return best