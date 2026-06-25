"""Rutas de almacenamiento para evidencia autorizada."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MEDICAL_PHOTOS = ROOT / "uploads" / "medical_triage"
EVIDENCE_VIDEOS = ROOT / "uploads" / "crowdsourced_evidence"
DRONE_FRAMES = ROOT / "uploads" / "drone_telemetry"
AI_SNAPSHOTS = ROOT / "uploads" / "ai_snapshots"
REFERENCE_PHOTOS = ROOT / "reference_photos"

for folder in (MEDICAL_PHOTOS, EVIDENCE_VIDEOS, DRONE_FRAMES, AI_SNAPSHOTS):
    folder.mkdir(parents=True, exist_ok=True)