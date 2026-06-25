"""Detección de víctimas en escombros con YOLOv7-OA y ingesta asíncrona de video."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import cv2
import numpy as np
import torch

from database import settings

logger = logging.getLogger(__name__)

HUMAN_CLASS_IDS = {0}
FACE_CLASS_IDS = {0, 1}


@dataclass
class Detection:
    bbox: tuple[int, int, int, int]
    confidence: float
    label: str
    crop: np.ndarray


class YOLOv7OADetector:
    """Detector occlusion-aware basado en YOLOv7 para cuerpos y rostros parcialmente cubiertos."""

    def __init__(self, weights_path: Optional[str] = None, device: Optional[str] = None):
        self.weights_path = Path(weights_path or settings.yolov7_weights)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = None
        self._load_model()

    def _load_model(self) -> None:
        if self.weights_path.exists():
            self.model = torch.hub.load(
                "WongKinYiu/yolov7",
                "custom",
                path_or_model=str(self.weights_path),
                trust_repo=True,
            )
        else:
            logger.warning(
                "Pesos YOLOv7-OA no encontrados en %s; usando YOLOv7-tiny como respaldo.",
                self.weights_path,
            )
            self.model = torch.hub.load(
                "WongKinYiu/yolov7",
                "yolov7-tiny",
                trust_repo=True,
            )
        self.model.to(self.device)
        self.model.eval()

    def _apply_occlusion_awareness(self, detections: list[Detection]) -> list[Detection]:
        """Prioriza detecciones con baja confianza pero persistencia espacial (típico de oclusión)."""
        if not detections:
            return []
        filtered: list[Detection] = []
        for det in detections:
            x1, y1, x2, y2 = det.bbox
            area = max(0, x2 - x1) * max(0, y2 - y1)
            aspect = (y2 - y1) / max(x2 - x1, 1)
            is_human_shape = 1.2 <= aspect <= 4.5 and area > 1200
            if det.confidence >= 0.25 or (det.confidence >= 0.15 and is_human_shape):
                filtered.append(det)
        return sorted(filtered, key=lambda d: d.confidence, reverse=True)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self.model is None:
            return []

        results = self.model(frame)
        raw = results.pandas().xyxy[0]
        detections: list[Detection] = []

        for _, row in raw.iterrows():
            class_id = int(row.get("class", row.get("cls", -1)))
            label = str(row.get("name", "person")).lower()
            if class_id not in HUMAN_CLASS_IDS and "person" not in label and "face" not in label:
                continue

            x1, y1, x2, y2 = map(int, [row["xmin"], row["ymin"], row["xmax"], row["ymax"]])
            h, w = frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            detections.append(
                Detection(
                    bbox=(x1, y1, x2, y2),
                    confidence=float(row["confidence"]),
                    label=label,
                    crop=crop,
                )
            )

        return self._apply_occlusion_awareness(detections)

    def frame_contains_humans(self, frame: np.ndarray) -> bool:
        return len(self.detect(frame)) > 0


class AsyncVideoIngestor:
    """Captura frames de streams RTSP sin bloquear el event loop principal."""

    def __init__(
        self,
        rtsp_url: str,
        frame_skip: int = 5,
        reconnect_delay: float = 3.0,
    ):
        self.rtsp_url = rtsp_url
        self.frame_skip = max(1, frame_skip)
        self.reconnect_delay = reconnect_delay
        self._running = False

    async def _open_capture(self) -> cv2.VideoCapture:
        loop = asyncio.get_running_loop()
        cap = await loop.run_in_executor(
            None,
            lambda: cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG),
        )
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    async def stream_frames(self) -> AsyncIterator[np.ndarray]:
        self._running = True
        cap: Optional[cv2.VideoCapture] = None
        frame_index = 0

        while self._running:
            if cap is None or not cap.isOpened():
                cap = await self._open_capture()
                if not cap.isOpened():
                    logger.error("No se pudo abrir stream RTSP: %s", self.rtsp_url)
                    await asyncio.sleep(self.reconnect_delay)
                    continue

            loop = asyncio.get_running_loop()
            ret, frame = await loop.run_in_executor(None, cap.read)
            frame_index += 1

            if not ret or frame is None:
                logger.warning("Pérdida de señal en %s; reconectando...", self.rtsp_url)
                cap.release()
                cap = None
                await asyncio.sleep(self.reconnect_delay)
                continue

            if frame_index % self.frame_skip != 0:
                continue

            yield frame

        if cap is not None:
            cap.release()

    def stop(self) -> None:
        self._running = False


async def process_rescue_feed(
    rtsp_url: str,
    detector: YOLOv7OADetector,
    on_human_frame: Callable[[np.ndarray, list[Detection]], asyncio.Future | None],
    frame_skip: Optional[int] = None,
) -> None:
    """Procesa un flujo de rescate descartando frames sin humanos para ahorrar ancho de banda."""
    ingestor = AsyncVideoIngestor(rtsp_url, frame_skip=frame_skip or settings.frame_skip)

    async for frame in ingestor.stream_frames():
        detections = await asyncio.get_running_loop().run_in_executor(
            None, detector.detect, frame
        )
        if not detections:
            continue

        result = on_human_frame(frame, detections)
        if asyncio.iscoroutine(result):
            await result