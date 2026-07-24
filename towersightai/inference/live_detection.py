from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from towersightai.camera.pipeline import display_orientation_element
from towersightai.config.settings import CameraConfig, Settings
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.inference.image_smoke import HAILO_CALLBACK_MODULE, NETWORK_FORMAT, NETWORK_HEIGHT, NETWORK_WIDTH, _gst_runtime_env
from towersightai.runtime_logging import (
    missing_resource_paths,
    new_run_id,
    path_diagnostic,
    redact_sensitive_text,
    write_run_status,
)


DEFAULT_DETECTION_DIR = Path("artifacts/runtime/detections")
FATAL_GSTREAMER_PATTERNS = (
    "HAILO_OUT_OF_PHYSICAL_DEVICES",
    "Failed to create vdevice",
    "Caught SIGSEGV",
    "CHECK_SUCCESS failed",
    "CHECK_EXPECTED failed",
    "파이프라인이 재생을 원하지 않음",
    "파이프라인이 PREROLL하기를 원하지 않음",
    "Internal data stream error",
)


@dataclass(frozen=True)
class LiveDetectionProcess:
    command: tuple[str, ...]
    event_path: Path
    env: dict[str, str]
    hef_path: Path
    log_path: Path | None = None
    resource_paths: tuple[Path, ...] = ()
    run_id: str = ""
    status_path: Path | None = None
    validate_resource_paths: bool = False


def build_live_detection_pipeline(
    settings: Settings,
    camera: CameraConfig,
    *,
    latency_ms: int = 100,
    min_confidence: float = 0.1,
    rotation_degrees: int | None = None,
    hef_path: Path | None = None,
) -> str:
    rotation = camera.rotation_degrees if rotation_degrees is None else rotation_degrees
    orientation = display_orientation_element(rotation)
    effective_hef_path = Path(hef_path or settings.hailo_hef_path)
    return " ".join(
        (
            f"rtspsrc location={camera.rtsp_url} latency={latency_ms} protocols=tcp drop-on-latency=true",
            "! rtph264depay",
            "! h264parse",
            "! decodebin",
            "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
            f"! {orientation}" if orientation else "",
            "! videoscale add-borders=true n-threads=2",
            "! videoconvert n-threads=3",
            f"! video/x-raw,format={NETWORK_FORMAT},width={NETWORK_WIDTH},height={NETWORK_HEIGHT},pixel-aspect-ratio=1/1",
            "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
            f"! hailonet hef-path={effective_hef_path} batch-size=1 nms-score-threshold={min_confidence} "
            "nms-iou-threshold=0.45 output-format-type=HAILO_FORMAT_TYPE_FLOAT32",
            "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
            f"! hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so} qos=false",
            "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
            f"! hailopython module={HAILO_CALLBACK_MODULE} qos=false",
            "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
            "! fakesink sync=false",
        )
    )


def build_live_multistream_detection_pipeline(
    settings: Settings,
    cameras: tuple[CameraConfig, ...],
    *,
    callback_modules: dict[str, Path],
    latency_ms: int = 100,
    min_confidence: float = 0.1,
    camera_rotations: dict[str, int] | None = None,
    hef_path: Path | None = None,
) -> str:
    effective_hef_path = Path(hef_path or settings.hailo_hef_path)
    streamrouter_inputs = " ".join(
        f'src_{index}::input-streams="<sink_{index}>"'
        for index, _camera in enumerate(cameras)
    )
    source_branches = " ".join(
        _multistream_source_branch(
            camera,
            index=index,
            callback_module=callback_modules[camera.id],
            latency_ms=latency_ms,
            rotation_degrees=(camera_rotations or {}).get(camera.id, camera.rotation_degrees),
        )
        for index, camera in enumerate(cameras)
    )
    return " ".join(
        (
            "hailoroundrobin mode=0 name=fun",
            "! queue name=hailo_pre_infer_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={effective_hef_path} batch-size=1 nms-score-threshold={min_confidence} "
            "nms-iou-threshold=0.45 output-format-type=HAILO_FORMAT_TYPE_FLOAT32",
            "! queue name=hailo_postprocess_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so} qos=false",
            "! queue name=hailo_router_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailostreamrouter name=sid {streamrouter_inputs}",
            source_branches,
        )
    )


def _multistream_source_branch(
    camera: CameraConfig,
    *,
    index: int,
    callback_module: Path,
    latency_ms: int,
    rotation_degrees: int,
) -> str:
    orientation = display_orientation_element(rotation_degrees)
    return " ".join(
        (
            f"rtspsrc location={camera.rtsp_url} name=source_{index} message-forward=true "
            f"latency={latency_ms} protocols=tcp drop-on-latency=true",
            "! rtph264depay",
            "! h264parse",
            "! decodebin",
            f"! queue name=hailo_preprocess_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! {orientation}" if orientation else "",
            "! videoscale add-borders=true n-threads=2",
            "! videoconvert n-threads=3",
            f"! video/x-raw,format={NETWORK_FORMAT},width={NETWORK_WIDTH},height={NETWORK_HEIGHT},pixel-aspect-ratio=1/1",
            f"! queue name=hailo_roundrobin_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! fun.sink_{index}",
            f"sid.src_{index}",
            f"! queue name=hailo_callback_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailopython module={callback_module} qos=false",
            f"! queue name=hailo_sink_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! fakesink sync=false",
        )
    )


def live_detection_process(
    settings: Settings,
    camera: CameraConfig,
    *,
    event_dir: Path = DEFAULT_DETECTION_DIR,
    latency_ms: int = 100,
    min_confidence: float = 0.1,
    rotation_degrees: int | None = None,
    hef_path: Path | None = None,
    gst_launch: str = "gst-launch-1.0",
) -> LiveDetectionProcess:
    event_path = event_dir / f"{camera.id}.jsonl"
    effective_hef_path = Path(hef_path or settings.hailo_hef_path)
    pipeline = build_live_detection_pipeline(
        settings,
        camera,
        latency_ms=latency_ms,
        min_confidence=min_confidence,
        rotation_degrees=rotation_degrees,
        hef_path=effective_hef_path,
    )
    env = _gst_runtime_env(settings)
    env.update(
        {
            "TOWERSIGHTAI_HAILO_CAMERA_ID": camera.id,
            "TOWERSIGHTAI_HAILO_EVENT_PATH": str(event_path),
            "TOWERSIGHTAI_HAILO_MIN_CONFIDENCE": str(min_confidence),
        }
    )
    return LiveDetectionProcess(
        command=(gst_launch, "-q", *shlex.split(pipeline)),
        event_path=event_path,
        env=env,
        hef_path=effective_hef_path,
        log_path=event_dir / f"{camera.id}.gst.log",
        resource_paths=(effective_hef_path, settings.hailo_postprocess_so),
        run_id=new_run_id(f"general-{camera.id}"),
        status_path=event_dir / f"{camera.id}.run-status.json",
        validate_resource_paths=True,
    )


def live_multistream_detection_process(
    settings: Settings,
    cameras: tuple[CameraConfig, ...],
    *,
    event_dir: Path = DEFAULT_DETECTION_DIR,
    latency_ms: int = 100,
    min_confidence: float = 0.1,
    camera_rotations: dict[str, int] | None = None,
    hef_path: Path | None = None,
    gst_launch: str = "gst-launch-1.0",
) -> LiveDetectionProcess:
    if not cameras:
        raise ValueError("At least one camera is required for live multistream detection.")
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "multistream.jsonl"
    effective_hef_path = Path(hef_path or settings.hailo_hef_path)
    callback_modules = {
        camera.id: _write_multistream_callback_module(
            event_dir=event_dir,
            camera_id=camera.id,
            event_path=event_path,
            min_confidence=min_confidence,
        )
        for camera in cameras
    }
    pipeline = build_live_multistream_detection_pipeline(
        settings,
        cameras,
        callback_modules=callback_modules,
        latency_ms=latency_ms,
        min_confidence=min_confidence,
        camera_rotations=camera_rotations,
        hef_path=effective_hef_path,
    )
    env = _gst_runtime_env(settings)
    env.update(
        {
            "TOWERSIGHTAI_HAILO_EVENT_PATH": str(event_path),
            "TOWERSIGHTAI_HAILO_MIN_CONFIDENCE": str(min_confidence),
        }
    )
    return LiveDetectionProcess(
        command=(gst_launch, "-q", *shlex.split(pipeline)),
        event_path=event_path,
        env=env,
        hef_path=effective_hef_path,
        log_path=event_dir / "multistream.gst.log",
        resource_paths=(effective_hef_path, settings.hailo_postprocess_so),
        run_id=new_run_id("general-multistream"),
        status_path=event_dir / "multistream.run-status.json",
        validate_resource_paths=True,
    )


def _write_multistream_callback_module(
    *,
    event_dir: Path,
    camera_id: str,
    event_path: Path,
    min_confidence: float,
) -> Path:
    safe_camera_id = "".join(char if char.isalnum() or char == "_" else "_" for char in camera_id)
    module_path = event_dir / f"callback_{safe_camera_id}.py"
    module_path.write_text(
        "\n".join(
            (
                "from towersightai.inference.callback import run_with_config",
                "",
                "def run(video_frame):",
                "    return run_with_config(",
                "        video_frame,",
                f"        camera_id={camera_id!r},",
                f"        event_path={str(event_path)!r},",
                f"        min_confidence={min_confidence!r},",
                "    )",
                "",
                "def close():",
                "    pass",
                "",
            )
        ),
        encoding="utf-8",
    )
    return module_path


def parse_detection_json(line: str) -> DetectionEvent | None:
    try:
        payload = json.loads(line)
        bbox = payload["bbox"]
        timestamp = datetime.fromisoformat(payload["timestamp"])
        return DetectionEvent(
            camera_id=str(payload["camera_id"]),
            label=str(payload["label"]),
            confidence=float(payload["confidence"]),
            bbox=BoundingBox(
                x=float(bbox["x"]),
                y=float(bbox["y"]),
                w=float(bbox["w"]),
                h=float(bbox["h"]),
            ),
            timestamp=timestamp,
            source=str(payload.get("source", "hailo")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class DetectionFileTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0

    def read_new_events(self) -> tuple[DetectionEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[DetectionEvent] = []
        with self.path.open("r", encoding="utf-8") as fp:
            fp.seek(self._offset)
            for line in fp:
                event = parse_detection_json(line)
                if event is not None:
                    events.append(event)
            self._offset = fp.tell()
        return tuple(events)


class LiveDetectionRunner:
    def __init__(
        self,
        process: LiveDetectionProcess,
        *,
        on_events: Callable[[tuple[DetectionEvent, ...]], None],
        on_error: Callable[[str], None],
        poll_seconds: float = 0.1,
    ) -> None:
        self.process = process
        self.on_events = on_events
        self.on_error = on_error
        self.poll_seconds = poll_seconds
        self._running = True
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._running = False
        self._terminate_process()

    def run(self) -> bool:
        logger = logging.getLogger("towersightai.ai.general")
        run_id = self.process.run_id or new_run_id("general")
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        event_counts: Counter[str] = Counter()
        resources = self.process.resource_paths or (self.process.hef_path,)
        logger.info(
            "ai-launch run-id=%s task=general cwd=%s python=%s event-path=%s raw-log=%s",
            run_id,
            Path.cwd(),
            sys.executable,
            self.process.event_path.resolve(strict=False),
            self.process.log_path.resolve(strict=False) if self.process.log_path else "disabled",
        )
        for resource in resources:
            logger.info("ai-resource run-id=%s task=general detail=%s", run_id, path_diagnostic(resource))
        logger.info(
            "ai-command run-id=%s task=general executable=%s command=%s virtualenv=%s",
            run_id,
            shutil.which(self.process.command[0]) or "missing",
            _redacted_command_text(self.process.command),
            self.process.env.get("VIRTUAL_ENV", "not-used"),
        )

        missing = missing_resource_paths(resources) if self.process.validate_resource_paths else ()
        if missing:
            message = "missing AI resource(s): " + ", ".join(str(path.resolve(strict=False)) for path in missing)
            logger.error("ai-preflight-failed run-id=%s task=general reason=%s", run_id, message)
            self._write_status(
                run_id=run_id,
                status="preflight_failed",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                error=message,
            )
            self.on_error(message)
            return False
        if shutil.which(self.process.command[0]) is None:
            message = f"{self.process.command[0]} not found"
            logger.error("ai-preflight-failed run-id=%s task=general reason=%s", run_id, message)
            self._write_status(
                run_id=run_id,
                status="preflight_failed",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                error=message,
            )
            self.on_error(message)
            return False

        self.process.event_path.parent.mkdir(parents=True, exist_ok=True)
        if self.process.event_path.exists():
            self.process.event_path.unlink()
        if self.process.log_path is not None:
            self.process.log_path.parent.mkdir(parents=True, exist_ok=True)
        tail = DetectionFileTail(self.process.event_path)
        stderr_target = self.process.log_path.open("w", encoding="utf-8") if self.process.log_path is not None else subprocess.DEVNULL
        try:
            if hasattr(stderr_target, "write"):
                stderr_target.write(_launch_log_text(self.process))
                stderr_target.flush()
            try:
                self._process = subprocess.Popen(
                    self.process.command,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_target,
                    text=True,
                    env=self.process.env,
                    start_new_session=True,
                )
            except OSError as exc:
                message = f"failed to launch {self.process.command[0]}: {exc}"
                logger.exception("ai-launch-failed run-id=%s task=general", run_id)
                self._write_status(
                    run_id=run_id,
                    status="launch_failed",
                    started_at=started_at,
                    returncode=None,
                    duration_seconds=time.monotonic() - started_monotonic,
                    event_counts=event_counts,
                    error=message,
                )
                self.on_error(message)
                return False
            process_pid = getattr(self._process, "pid", None)
            logger.info("ai-process-start run-id=%s task=general pid=%s", run_id, process_pid)
            self._write_status(
                run_id=run_id,
                status="running",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                pid=process_pid,
            )
            first_event_logged = False
            next_activity_log = time.monotonic() + 30.0

            while self._running:
                events = tail.read_new_events()
                if events:
                    event_counts.update(event.camera_id for event in events)
                    if not first_event_logged:
                        first_event_logged = True
                        logger.info(
                            "ai-first-detection run-id=%s task=general elapsed-seconds=%.3f cameras=%s",
                            run_id,
                            time.monotonic() - started_monotonic,
                            sorted(event_counts),
                        )
                        self._write_status(
                            run_id=run_id,
                            status="running",
                            started_at=started_at,
                            returncode=None,
                            duration_seconds=time.monotonic() - started_monotonic,
                            event_counts=event_counts,
                        )
                    self.on_events(events)
                fatal_message = _fatal_log_message(self.process.log_path)
                if fatal_message:
                    safe_message = redact_sensitive_text(fatal_message)
                    logger.error("ai-fatal run-id=%s task=general log-tail=%s", run_id, safe_message)
                    self.on_error(safe_message)
                    self._terminate_process(force=True)
                    break
                if self._process.poll() is not None:
                    break
                if time.monotonic() >= next_activity_log:
                    log_method = logger.warning if not event_counts else logger.info
                    log_method(
                        "ai-activity run-id=%s task=general process-alive=true events=%s elapsed-seconds=%.1f",
                        run_id,
                        dict(event_counts),
                        time.monotonic() - started_monotonic,
                    )
                    self._write_status(
                        run_id=run_id,
                        status="running",
                        started_at=started_at,
                        returncode=None,
                        duration_seconds=time.monotonic() - started_monotonic,
                        event_counts=event_counts,
                    )
                    next_activity_log = time.monotonic() + 30.0
                time.sleep(self.poll_seconds)

            events = tail.read_new_events()
            if events:
                event_counts.update(event.camera_id for event in events)
                self.on_events(events)
            if self._running and self._process.poll() not in (None, 0):
                message = redact_sensitive_text(_process_error_message(self._process.returncode, self.process.log_path))
                logger.error(
                    "ai-process-error run-id=%s task=general returncode=%s log-tail=%s",
                    run_id,
                    self._process.returncode,
                    message,
                )
                self.on_error(message)
        finally:
            self._terminate_process()
            if hasattr(stderr_target, "close"):
                stderr_target.close()
        returncode = self._process.returncode if self._process is not None else None
        duration_seconds = time.monotonic() - started_monotonic
        status = "stopped" if not self._running else ("completed" if returncode == 0 else "failed")
        logger.info(
            "ai-process-end run-id=%s task=general status=%s returncode=%s duration-seconds=%.3f events=%s",
            run_id,
            status,
            returncode,
            duration_seconds,
            dict(event_counts),
        )
        self._write_status(
            run_id=run_id,
            status=status,
            started_at=started_at,
            returncode=returncode,
            duration_seconds=duration_seconds,
            event_counts=event_counts,
        )
        return True

    def _write_status(
        self,
        *,
        run_id: str,
        status: str,
        started_at: datetime,
        returncode: int | None,
        duration_seconds: float,
        event_counts: Counter[str],
        pid: int | None = None,
        error: str | None = None,
    ) -> None:
        if self.process.status_path is None:
            return
        effective_pid = pid if pid is not None else (getattr(self._process, "pid", None) if self._process is not None else None)
        write_run_status(
            self.process.status_path,
            run_id=run_id,
            task_id="general",
            status=status,
            started_at=started_at.isoformat(),
            pid=effective_pid,
            returncode=returncode,
            duration_seconds=round(duration_seconds, 3),
            event_counts=dict(event_counts),
            event_total=sum(event_counts.values()),
            event_path=str(self.process.event_path.resolve(strict=False)),
            log_path=str(self.process.log_path.resolve(strict=False)) if self.process.log_path else None,
            error=error,
        )

    def _terminate_process(self, *, force: bool = False) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os_killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
        if not force:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os_killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()


def os_killpg(pid: int, sig: signal.Signals) -> None:
    import os

    os.killpg(pid, sig)


def latest_events(events: Iterable[DetectionEvent], *, limit: int = 20) -> tuple[DetectionEvent, ...]:
    return tuple(sorted(events, key=lambda event: event.confidence, reverse=True)[:limit])


def _launch_log_text(process: LiveDetectionProcess) -> str:
    resources = process.resource_paths or (process.hef_path,)
    return "\n".join(
        (
            "",
            "TowerSightAI AI Detection launch",
            f"run-id={process.run_id or '-'}",
            f"cwd={Path.cwd()}",
            f"python={sys.executable}",
            f"active-hef-path={process.hef_path}",
            *(f"resource={path_diagnostic(path)}" for path in resources),
            f"event-path={process.event_path}",
            f"virtualenv={process.env.get('VIRTUAL_ENV', 'not-used')}",
            f"gst-launch={shutil.which(process.command[0]) or 'missing'}",
            f"command-redacted={_redacted_command_text(process.command)}",
            "",
        )
    )


def _redacted_command_text(command: tuple[str, ...]) -> str:
    return _redact_rtsp_credentials(" ".join(shlex.quote(part) for part in command))


def _redact_rtsp_credentials(text: str) -> str:
    return re.sub(r"(rtsp://)([^@\s/]+)@", r"\1***:***@", text)


def _process_error_message(returncode: int | None, log_path: Path | None) -> str:
    log_tail = _read_log_tail(log_path)
    if log_tail:
        return log_tail
    return f"gst-launch exited with {returncode}"


def _fatal_log_message(log_path: Path | None) -> str:
    log_tail = _read_log_tail(log_path)
    if not log_tail:
        return ""
    for pattern in FATAL_GSTREAMER_PATTERNS:
        if pattern in log_tail:
            return log_tail
    return ""


def _read_log_tail(log_path: Path | None, *, max_chars: int = 4000) -> str:
    if log_path is None or not log_path.exists():
        return ""
    try:
        with log_path.open("rb") as fp:
            fp.seek(0, 2)
            size = fp.tell()
            fp.seek(max(0, size - max_chars))
            return fp.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
