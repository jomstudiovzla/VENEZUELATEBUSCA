"""Red de cámaras SAR en tiempo real — Venezuela 2026."""

from __future__ import annotations

import asyncio
import json
import logging
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import httpx
import numpy as np

from connection_manager import victim_room_manager
from event_bus import missing_updates_bus

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config/camaras_venezuela.json")
SNAPSHOT_DIR = Path("camera_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)
DETECTIONS_LOG = Path("camera_detections.jsonl")

TARGET_FPS = 15
DETECTION_EVERY_N_FRAMES = 30


def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for candidate in ("ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True, timeout=3)
            return candidate
        except Exception:
            continue
    return None


FFMPEG_EXE = _ffmpeg_exe()


@dataclass
class CameraConfig:
    id: str
    nombre: str
    ciudad: str
    zona: str
    ip: str
    puerto: int
    protocolo: str
    rtsp_url: str
    usuario: str = "admin"
    clave: str = ""
    http_mjpeg: Optional[str] = None
    http_snapshot: Optional[str] = None
    video_en_vivo: list[str] = field(default_factory=list)
    relay_direct: bool = True
    notas: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CameraConfig":
        video_urls = data.get("video_en_vivo") or []
        if isinstance(video_urls, str):
            video_urls = [video_urls]
        return cls(
            id=data["id"],
            nombre=data["nombre"],
            ciudad=data["ciudad"],
            zona=data.get("zona", ""),
            ip=data.get("ip", ""),
            puerto=int(data.get("puerto", 554)),
            protocolo=data.get("protocolo", "rtsp"),
            rtsp_url=data.get("rtsp_url", ""),
            usuario=data.get("usuario", "admin"),
            clave=data.get("clave", ""),
            http_mjpeg=data.get("http_mjpeg"),
            http_snapshot=data.get("http_snapshot"),
            video_en_vivo=list(video_urls),
            relay_direct=bool(data.get("relay_direct", True)),
            notas=data.get("notas", ""),
        )

    def stream_candidates(self) -> list[tuple[str, str]]:
        """(url, tipo) en orden de prioridad: mjpeg, rtsp, snapshot."""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []

        def add(url: Optional[str], kind: str) -> None:
            if url and url not in seen:
                seen.add(url)
                out.append((url, kind))

        add(self.http_mjpeg, "mjpeg")
        for url in self.video_en_vivo:
            kind = "mjpeg" if url.lower().startswith(("http://", "https://")) else "rtsp"
            add(url, kind)
        for url in self.rtsp_candidates()[:3]:
            add(url, "rtsp")
        if self.http_snapshot and self.http_snapshot not in seen:
            add(self.http_snapshot, "snapshot")
        return out

    def rtsp_candidates(self) -> list[str]:
        auth = ""
        if self.usuario:
            auth = f"{self.usuario}:{self.clave}@" if self.clave else f"{self.usuario}@"
        ip = self.ip
        port = self.puerto
        paths = [
            self.rtsp_url,
            f"rtsp://{auth}{ip}:{port}/Streaming/Channels/101",
            f"rtsp://{auth}{ip}:{port}/Streaming/Channels/1",
            f"rtsp://{auth}{ip}:{port}/cam/realmonitor?channel=1&subtype=0",
            f"rtsp://{auth}{ip}:{port}/cam/realmonitor?channel=1&subtype=1",
            f"rtsp://{auth}{ip}:{port}/h264/ch1/main/av_stream",
            f"rtsp://{auth}{ip}:{port}/live/ch00_0",
        ]
        seen: set[str] = set()
        out: list[str] = []
        for url in paths:
            if url and url not in seen:
                seen.add(url)
                out.append(url)
        return out


@dataclass
class CameraRuntime:
    config: CameraConfig
    status: str = "iniciando"
    mode: str = "directo"
    last_frame_at: Optional[str] = None
    last_error: Optional[str] = None
    frames_captured: int = 0
    fps: float = 0.0
    latest_snapshot: Optional[str] = None
    latest_jpeg: Optional[bytes] = None
    stream_source: Optional[str] = None
    relay_url: Optional[str] = None
    _thread: Optional[threading.Thread] = None
    _running: bool = False
    _jpeg_lock: threading.Lock = field(default_factory=threading.Lock)
    _frame_event: threading.Event = field(default_factory=threading.Event)
    _last_fps_tick: float = field(default_factory=time.monotonic)
    _fps_counter: int = 0


def load_camera_configs() -> list[CameraConfig]:
    if not CONFIG_PATH.exists():
        return []
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return [CameraConfig.from_dict(item) for item in data]


def _append_detection(record: dict[str, Any]) -> None:
    with DETECTIONS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_detections_24h(camera_id: Optional[str] = None) -> list[dict[str, Any]]:
    if not DETECTIONS_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    results: list[dict[str, Any]] = []
    for line in DETECTIONS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if camera_id and item.get("camera_id") != camera_id:
            continue
        try:
            ts = datetime.fromisoformat(item["detected_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            results.append(item)
    return sorted(results, key=lambda x: x.get("detected_at", ""), reverse=True)


def _detect_humans_simple(frame: np.ndarray) -> tuple[int, float]:
    try:
        small = cv2.resize(frame, (640, 360))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        boxes, weights = hog.detectMultiScale(gray, winStride=(8, 8), padding=(8, 8), scale=1.05)
        if len(boxes) == 0:
            return 0, 0.0
        conf = float(max(weights) if len(weights) else 0.5)
        if conf < 0.45:
            return 0, 0.0
        return len(boxes), conf
    except Exception:
        return 0, 0.0


def _bytes_to_frame(data: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _extract_jpegs_from_buffer(buf: bytearray) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            if len(buf) > 2:
                del buf[:-2]
            break
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start > 0:
                del buf[:start]
            break
        frames.append(bytes(buf[start : end + 2]))
        del buf[: end + 2]
    return frames


class StreamReader:
    """Lee frames JPEG de un stream HTTP MJPEG o RTSP vía ffmpeg."""

    def __init__(self, url: str, kind: str) -> None:
        self.url = url
        self.kind = kind
        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[httpx.Client] = None
        self._response: Any = None

    def open(self) -> bool:
        self.close()
        if self.kind == "mjpeg" and self.url.lower().startswith(("http://", "https://")):
            return self._open_http_mjpeg()
        if FFMPEG_EXE:
            return self._open_ffmpeg()
        return False

    def _open_http_mjpeg(self) -> bool:
        try:
            self._client = httpx.Client(
                timeout=httpx.Timeout(30.0, read=30.0),
                follow_redirects=True,
            )
            req = self._client.build_request("GET", self.url)
            self._response = self._client.send(req, stream=True)
            if self._response.status_code >= 400:
                self.close()
                return False
            return True
        except Exception:
            self.close()
            return False

    def _open_ffmpeg(self) -> bool:
        assert FFMPEG_EXE
        cmd = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.kind == "rtsp" or self.url.lower().startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        cmd += [
            "-i",
            self.url,
            "-an",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "4",
            "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            return self._proc.stdout is not None
        except Exception:
            self.close()
            return False

    def read_jpeg(self, timeout: float = 3.0) -> Optional[bytes]:
        if self._response is not None:
            return self._read_http_jpeg(timeout)
        if self._proc and self._proc.stdout:
            return self._read_ffmpeg_jpeg(timeout)
        return None

    def _read_http_jpeg(self, timeout: float) -> Optional[bytes]:
        if not hasattr(self, "_http_buf"):
            self._http_buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = next(self._response.iter_bytes(8192))
                self._http_buf.extend(chunk)
                frames = _extract_jpegs_from_buffer(self._http_buf)
                if frames:
                    return frames[-1]
            except StopIteration:
                break
            except Exception:
                break
        return None

    def _read_ffmpeg_jpeg(self, timeout: float) -> Optional[bytes]:
        assert self._proc and self._proc.stdout
        if not hasattr(self, "_ff_buf"):
            self._ff_buf = bytearray()
        deadline = time.monotonic() + timeout
        fd = self._proc.stdout.fileno()
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], min(0.4, remaining))
                if not ready:
                    continue
                chunk = self._proc.stdout.read(8192)
                if not chunk:
                    break
                self._ff_buf.extend(chunk)
                frames = _extract_jpegs_from_buffer(self._ff_buf)
                if frames:
                    return frames[-1]
            except Exception:
                break
        return None

    def close(self) -> None:
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None
        for attr in ("_http_buf", "_ff_buf"):
            if hasattr(self, attr):
                delattr(self, attr)


def _capture_loop(camera_id: str, runtime: CameraRuntime, on_detection: Callable) -> None:
    cfg = runtime.config
    frame_interval = 1.0 / TARGET_FPS
    reader: Optional[StreamReader] = None
    active_url: Optional[str] = None
    active_kind: Optional[str] = None
    candidate_idx = 0
    candidates = cfg.stream_candidates()
    frames_for_detection = 0
    failures_this_cycle = 0

    while runtime._running:
        loop_start = time.monotonic()

        if reader is None:
            if not candidates:
                runtime.status = "sin_señal"
                runtime.last_error = "Sin URLs de video configuradas en config/camaras_venezuela.json"
                time.sleep(3)
                continue

            url, kind = candidates[candidate_idx % len(candidates)]
            candidate_idx += 1
            if candidate_idx > len(candidates):
                failures_this_cycle = 0
            reader = StreamReader(url, kind)
            if not reader.open():
                reader.close()
                reader = None
                failures_this_cycle += 1
                if failures_this_cycle >= len(candidates):
                    runtime.status = "sin_señal"
                    runtime.last_error = (
                        f"No hay señal de video en {cfg.ip}. "
                        f"Configure http_mjpeg o video_en_vivo con URL RTSP/MJPEG real."
                    )
                    time.sleep(5)
                    candidate_idx = 0
                    failures_this_cycle = 0
                else:
                    runtime.status = "reconectando"
                continue

            active_url, active_kind = url, kind
            runtime.stream_source = url
            runtime.relay_url = url if kind == "mjpeg" and cfg.relay_direct else None
            runtime.mode = "mjpeg_vivo" if kind == "mjpeg" else "rtsp_vivo"
            runtime.status = "conectando"
            runtime.last_error = None
            failures_this_cycle = 0

        jpeg = reader.read_jpeg(timeout=4.0 if active_kind == "rtsp" else 2.0)
        if not jpeg:
            reader.close()
            reader = None
            failures_this_cycle += 1
            if failures_this_cycle >= len(candidates):
                runtime.status = "sin_señal"
                runtime.last_error = (
                    f"Sin video en vivo desde {cfg.ip}. "
                    f"Verifique red, credenciales o agregue video_en_vivo en la configuración."
                )
                time.sleep(5)
                candidate_idx = 0
                failures_this_cycle = 0
            else:
                runtime.status = "reconectando"
                time.sleep(1.0)
            continue

        with runtime._jpeg_lock:
            runtime.latest_jpeg = jpeg
        runtime._frame_event.set()

        runtime.frames_captured += 1
        runtime._fps_counter += 1
        now_mono = time.monotonic()
        if now_mono - runtime._last_fps_tick >= 1.0:
            runtime.fps = round(runtime._fps_counter / (now_mono - runtime._last_fps_tick), 1)
            runtime._fps_counter = 0
            runtime._last_fps_tick = now_mono

        now = datetime.now(timezone.utc).isoformat()
        runtime.last_frame_at = now
        runtime.status = "en_vivo"

        snap_name = f"{camera_id}_live.jpg"
        snap_path = SNAPSHOT_DIR / snap_name
        try:
            snap_path.write_bytes(jpeg)
            runtime.latest_snapshot = f"/camera-snapshots/{snap_name}"
        except Exception:
            pass

        frames_for_detection += 1
        if frames_for_detection >= DETECTION_EVERY_N_FRAMES and active_kind != "snapshot":
            frames_for_detection = 0
            frame = _bytes_to_frame(jpeg)
            if frame is not None:
                person_count, confidence = _detect_humans_simple(frame)
                if person_count > 0:
                    detection = {
                        "camera_id": camera_id,
                        "ciudad": cfg.ciudad,
                        "nombre": cfg.nombre,
                        "detected_at": now,
                        "person_count": person_count,
                        "confidence": round(confidence, 3),
                        "snapshot_url": f"/camera-snapshots/{snap_name}",
                        "ip": cfg.ip,
                        "fuente": runtime.mode,
                    }
                    _append_detection(detection)
                    on_detection(detection)

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, frame_interval - elapsed)
        if sleep_for:
            time.sleep(sleep_for)

    if reader is not None:
        reader.close()


class CameraNetworkService:
    def __init__(self) -> None:
        self.cameras: dict[str, CameraRuntime] = {}
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_detections: list[dict[str, Any]] = []

    def _hydrate_snapshot(self, camera_id: str, runtime: CameraRuntime) -> None:
        snap_path = SNAPSHOT_DIR / f"{camera_id}_live.jpg"
        if snap_path.exists() and snap_path.stat().st_size > 0:
            runtime.latest_snapshot = f"/camera-snapshots/{camera_id}_live.jpg"

    def load_configs(self) -> None:
        for cfg in load_camera_configs():
            if cfg.id not in self.cameras:
                runtime = CameraRuntime(config=cfg)
                self._hydrate_snapshot(cfg.id, runtime)
                self.cameras[cfg.id] = runtime
            else:
                self.cameras[cfg.id].config = cfg
                self._hydrate_snapshot(cfg.id, self.cameras[cfg.id])

    async def start_all(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self.load_configs()
        for cam_id, runtime in self.cameras.items():
            self._start_capture_thread(cam_id, runtime)
        self._started = True
        asyncio.create_task(self._broadcast_loop())
        logger.info(
            "Red de cámaras iniciada | %d nodos | ffmpeg=%s | %dfps",
            len(self.cameras),
            "sí" if FFMPEG_EXE else "no",
            TARGET_FPS,
        )

    def _start_capture_thread(self, camera_id: str, runtime: CameraRuntime) -> None:
        if runtime._thread and runtime._thread.is_alive():
            return

        def on_detection(det: dict[str, Any]) -> None:
            if self._loop:
                self._loop.call_soon_threadsafe(self._pending_detections.append, det)

        runtime._running = True
        runtime._thread = threading.Thread(
            target=_capture_loop,
            args=(camera_id, runtime, on_detection),
            name=f"cam-{camera_id}",
            daemon=True,
        )
        runtime._thread.start()

    async def _broadcast_loop(self) -> None:
        while self._started:
            while self._pending_detections:
                det = self._pending_detections.pop(0)
                await missing_updates_bus.publish("camera_detection", det)
                await victim_room_manager.broadcast("camera_detection", det)

            for cam_id, runtime in self.cameras.items():
                if runtime.status == "en_vivo" and runtime.frames_captured % 15 == 0:
                    await victim_room_manager.broadcast("camera_frame", self.camera_status(cam_id))

            await asyncio.sleep(2)

    async def stop_all(self) -> None:
        self._started = False
        for runtime in self.cameras.values():
            runtime._running = False
            if runtime._thread and runtime._thread.is_alive():
                runtime._thread.join(timeout=3)
            runtime._thread = None
            runtime.status = "detenido"

    def get_live_jpeg(self, camera_id: str, wait: bool = False, timeout: float = 2.0) -> Optional[bytes]:
        runtime = self.cameras.get(camera_id)
        if not runtime:
            return None
        if wait and runtime._frame_event:
            runtime._frame_event.wait(timeout=timeout)
            runtime._frame_event.clear()
        with runtime._jpeg_lock:
            if runtime.latest_jpeg:
                return runtime.latest_jpeg
        live_path = SNAPSHOT_DIR / f"{camera_id}_live.jpg"
        if live_path.exists():
            return live_path.read_bytes()
        return None

    def get_relay_url(self, camera_id: str) -> Optional[str]:
        runtime = self.cameras.get(camera_id)
        if runtime and runtime.relay_url and runtime.status == "en_vivo":
            return runtime.relay_url
        cfg = runtime.config if runtime else None
        if cfg and cfg.http_mjpeg and cfg.relay_direct:
            return cfg.http_mjpeg
        return None

    def list_cameras(self) -> list[dict[str, Any]]:
        return [self.camera_status(cid) for cid in self.cameras]

    def camera_status(self, camera_id: str) -> dict[str, Any]:
        runtime = self.cameras.get(camera_id)
        if not runtime:
            return {}
        cfg = runtime.config
        detections = _load_detections_24h(camera_id)
        relay = self.get_relay_url(camera_id)
        return {
            "id": cfg.id,
            "nombre": cfg.nombre,
            "ciudad": cfg.ciudad,
            "zona": cfg.zona,
            "ip": cfg.ip,
            "puerto": cfg.puerto,
            "protocolo": cfg.protocolo,
            "rtsp_url": cfg.rtsp_url,
            "stream_activo": runtime.stream_source or cfg.rtsp_url,
            "modo": runtime.mode,
            "status": runtime.status,
            "fps": runtime.fps,
            "last_frame_at": runtime.last_frame_at,
            "last_error": runtime.last_error,
            "frames_captured": runtime.frames_captured,
            "detecciones_24h": len(detections),
            "ultima_deteccion": detections[0] if detections else None,
            "latest_snapshot": runtime.latest_snapshot,
            "live_stream_url": (
                f"/api/cameras/{cfg.id}/relay.mjpg"
                if relay
                else f"/api/cameras/{cfg.id}/live.mjpg"
            ),
            "relay_url": relay,
            "notas": cfg.notas,
        }

    def detections_24h(self, camera_id: Optional[str] = None) -> list[dict[str, Any]]:
        return _load_detections_24h(camera_id)


camera_service = CameraNetworkService()