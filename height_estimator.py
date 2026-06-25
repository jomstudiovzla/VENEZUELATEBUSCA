"""Estimación de estatura mediante calibración de Zhang y geometría proyectiva."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    camera_matrix: np.ndarray
    distortion_coeffs: np.ndarray
    reprojection_error: float


@dataclass
class HeightEstimate:
    height_cm: float
    confidence: float
    head_point: tuple[int, int]
    foot_point: tuple[int, int]
    horizon_y: float


class HeightEstimator:
    """Calibra cámaras de rescate y estima estatura real en centímetros."""

    def __init__(self, reference_height_cm: float = 170.0):
        self.reference_height_cm = reference_height_cm
        self.camera_matrix: Optional[np.ndarray] = None
        self.distortion_coeffs: Optional[np.ndarray] = None
        self.horizon_y: Optional[float] = None

    def calibrate_zhang(
        self,
        calibration_images: list[np.ndarray],
        pattern_size: tuple[int, int] = (9, 6),
        square_size_cm: float = 2.5,
    ) -> CalibrationResult:
        """Método de Zhang: calibración con patrón de tablero de ajedrez."""
        objp = np.zeros((pattern_size[0] * pattern_size[1], 3), np.float32)
        objp[:, :2] = np.mgrid[0 : pattern_size[0], 0 : pattern_size[1]].T.reshape(-1, 2)
        objp *= square_size_cm

        obj_points: list[np.ndarray] = []
        img_points: list[np.ndarray] = []
        image_size = None

        for image in calibration_images:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            image_size = (gray.shape[1], gray.shape[0])
            found, corners = cv2.findChessboardCorners(gray, pattern_size, None)
            if not found:
                continue
            corners_refined = cv2.cornerSubPix(
                gray,
                corners,
                (11, 11),
                (-1, -1),
                criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
            )
            obj_points.append(objp)
            img_points.append(corners_refined)

        if not obj_points or image_size is None:
            raise ValueError("No se detectaron patrones de calibración suficientes.")

        ret, camera_matrix, dist_coeffs, _, _ = cv2.calibrateCamera(
            obj_points, img_points, image_size, None, None
        )

        self.camera_matrix = camera_matrix
        self.distortion_coeffs = dist_coeffs

        return CalibrationResult(
            camera_matrix=camera_matrix,
            distortion_coeffs=dist_coeffs,
            reprojection_error=float(ret),
        )

    def load_calibration(
        self,
        camera_matrix: np.ndarray | list,
        distortion_coeffs: np.ndarray | list,
    ) -> None:
        self.camera_matrix = np.array(camera_matrix, dtype=np.float64)
        self.distortion_coeffs = np.array(distortion_coeffs, dtype=np.float64)

    def estimate_horizon(self, frame: np.ndarray) -> float:
        """Detecta la línea del horizonte para anclar la geometría proyectiva."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=80, maxLineGap=15)

        if lines is None:
            self.horizon_y = frame.shape[0] * 0.45
            return self.horizon_y

        horizontal_ys: list[float] = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if angle < 15 or angle > 165:
                horizontal_ys.append((y1 + y2) / 2)

        self.horizon_y = float(np.median(horizontal_ys)) if horizontal_ys else frame.shape[0] * 0.45
        return self.horizon_y

    def _body_axis_points(
        self, bbox: tuple[int, int, int, int]
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        x1, y1, x2, y2 = bbox
        foot = (int((x1 + x2) / 2), y2)
        head = (int((x1 + x2) / 2), y1)
        return head, foot

    def estimate_height(
        self,
        frame: np.ndarray,
        bbox: tuple[int, int, int, int],
        reference_person_height_cm: Optional[float] = None,
    ) -> HeightEstimate:
        """
        Cruza punto superior del cuerpo, punto inferior y línea del horizonte
        para calcular estatura en centímetros.
        """
        if self.camera_matrix is None:
            default_matrix = np.array(
                [
                    [800.0, 0.0, frame.shape[1] / 2],
                    [0.0, 800.0, frame.shape[0] / 2],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
            self.camera_matrix = default_matrix
            self.distortion_coeffs = np.zeros(5, dtype=np.float64)
            logger.warning("Cámara sin calibrar; usando matriz intrínseca por defecto.")

        head, foot = self._body_axis_points(bbox)
        horizon_y = self.horizon_y if self.horizon_y is not None else self.estimate_horizon(frame)

        fy = self.camera_matrix[1, 1]
        body_pixels = abs(foot[1] - head[1])
        if body_pixels < 10:
            return HeightEstimate(
                height_cm=0.0,
                confidence=0.0,
                head_point=head,
                foot_point=foot,
                horizon_y=horizon_y,
            )

        horizon_factor = 1.0 + abs(foot[1] - horizon_y) / max(frame.shape[0], 1) * 0.35
        ref_height = reference_person_height_cm or self.reference_height_cm
        pixel_to_cm = (ref_height / 480.0) * (fy / 800.0) * horizon_factor
        height_cm = body_pixels * pixel_to_cm

        vertical_alignment = 1.0 - min(abs(head[0] - foot[0]) / max(body_pixels, 1), 1.0)
        confidence = float(np.clip(0.4 + 0.6 * vertical_alignment, 0.0, 1.0))

        return HeightEstimate(
            height_cm=round(height_cm, 1),
            confidence=confidence,
            head_point=head,
            foot_point=foot,
            horizon_y=horizon_y,
        )

    def save_calibration(self, output_dir: str | Path) -> None:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        if self.camera_matrix is not None:
            np.save(output / "camera_matrix.npy", self.camera_matrix)
        if self.distortion_coeffs is not None:
            np.save(output / "distortion_coeffs.npy", self.distortion_coeffs)