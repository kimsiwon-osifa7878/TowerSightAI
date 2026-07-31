from __future__ import annotations

import os
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
from typing import Any, Callable, Iterable

from towersightai.camera.pipeline import display_orientation_element
from towersightai.config.settings import CameraConfig, Settings
from towersightai.inference.events import DetectionEvent
from towersightai.inference.hailo_apps_runtime import (
    PERSON_LABELS,
    VEHICLE_LABELS,
    hailo_apps_detection_command,
    hailo_apps_runtime_env,
)
from towersightai.inference.image_smoke import NETWORK_FORMAT, NETWORK_HEIGHT, NETWORK_WIDTH, _gst_runtime_env
from towersightai.inference.live_detection import _read_log_tail, latest_events, parse_detection_json
from towersightai.runtime_logging import (
    missing_resource_paths,
    new_run_id,
    path_diagnostic,
    redact_sensitive_text,
    write_run_status,
)


DEFAULT_PURPOSE_TASK_DIR = Path("artifacts/runtime/purpose-ai")
PERSON_PRESENCE_NETWORK_CAPS = (
    f"video/x-raw,format={NETWORK_FORMAT},width={NETWORK_WIDTH},height={NETWORK_HEIGHT},pixel-aspect-ratio=1/1"
)
FATAL_GSTREAMER_PATTERNS = (
    "HAILO_OUT_OF_PHYSICAL_DEVICES",
    "Failed to create vdevice",
    "Caught SIGSEGV",
    "CHECK_SUCCESS failed",
    "CHECK_EXPECTED failed",
    "HAILO_HEF_NOT_SUPPORTED",
    'no element "hailo',
    "파이프라인이 재생을 원하지 않음",
    "파이프라인이 PREROLL하기를 원하지 않음",
)
PURPOSE_VEHICLE_DETECTION = "vehicle_detection"
PURPOSE_LPR_IMAGE = "lpr_image"
PURPOSE_PERSON_PRESENCE = "person_presence"


@dataclass(frozen=True)
class PurposeInferenceProcess:
    task_id: str
    label: str
    command: tuple[str, ...]
    env: dict[str, str]
    log_path: Path
    event_path: Path
    model_paths: tuple[Path, ...]
    camera_ids: tuple[str, ...] = ()
    expected_runtime_seconds: float | None = None
    metadata_paths: tuple[Path, ...] = ()
    run_id: str = ""
    status_path: Path | None = None
    diagnostic_path: Path | None = None
    validate_resource_paths: bool = False
    max_consecutive_restarts: int = 0
    restart_delay_seconds: float = 1.0
    restart_stability_seconds: float = 60.0


@dataclass(frozen=True)
class PlateOcrEvent:
    plate_number: str
    confidence: float
    timestamp: datetime
    source: str = "hailo_lpr_image"


@dataclass(frozen=True)
class LprPreparedFrame:
    source_path: Path
    frame_path: Path
    source_index: int
    repeat_index: int


@dataclass(frozen=True)
class PurposeTaskSpec:
    task_id: str
    label: str
    description: str


PURPOSE_TASK_SPECS = {
    PURPOSE_VEHICLE_DETECTION: PurposeTaskSpec(
        PURPOSE_VEHICLE_DETECTION,
        "차량 전용 검출",
        "front 카메라에서 Hailo Apps 검출 모델을 실행하고 차량 라벨만 사용합니다.",
    ),
    PURPOSE_LPR_IMAGE: PurposeTaskSpec(
        PURPOSE_LPR_IMAGE,
        "번호판 이미지 LPR",
        "tmp/car_number-test 이미지를 FastALPR ONNX 모델로 순차 실행합니다.",
    ),
    PURPOSE_PERSON_PRESENCE: PurposeTaskSpec(
        PURPOSE_PERSON_PRESENCE,
        "사람 존재 감지",
        "정상 수신 중인 카메라에서 Hailo Apps 검출 모델의 person 라벨을 판단합니다.",
    ),
}


class PurposeInferenceRunner:
    def __init__(
        self,
        process: PurposeInferenceProcess,
        *,
        on_events: Callable[[tuple[DetectionEvent, ...]], None],
        on_lpr_results: Callable[[tuple[PlateOcrEvent, ...]], None] | None = None,
        on_error: Callable[[str], None],
        on_status: Callable[[str], None] | None = None,
        poll_seconds: float = 0.1,
    ) -> None:
        self.process = process
        self.on_events = on_events
        self.on_lpr_results = on_lpr_results or (lambda _events: None)
        self.on_error = on_error
        self.on_status = on_status or (lambda _message: None)
        self.poll_seconds = poll_seconds
        self._running = True
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._running = False
        self._terminate_process()

    def run(self) -> bool:
        logger = logging.getLogger("towersightai.ai.purpose")
        run_id = self.process.run_id or new_run_id(self.process.task_id)
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        event_counts: Counter[str] = Counter()
        lpr_result_count = 0
        terminal_error = False
        logger.info(
            "ai-launch run-id=%s task=%s cameras=%s cwd=%s python=%s event-path=%s raw-log=%s",
            run_id,
            self.process.task_id,
            self.process.camera_ids,
            Path.cwd(),
            sys.executable,
            self.process.event_path.resolve(strict=False),
            self.process.log_path.resolve(strict=False),
        )
        for resource in self.process.model_paths:
            logger.info(
                "ai-resource run-id=%s task=%s detail=%s",
                run_id,
                self.process.task_id,
                path_diagnostic(resource),
            )
        logger.info(
            "ai-command run-id=%s task=%s executable=%s command=%s virtualenv=%s",
            run_id,
            self.process.task_id,
            shutil.which(self.process.command[0]) or "missing",
            _redacted_command_text(self.process.command),
            self.process.env.get("VIRTUAL_ENV", "not-used"),
        )

        missing = missing_resource_paths(self.process.model_paths) if self.process.validate_resource_paths else ()
        if missing:
            message = "missing AI resource(s): " + ", ".join(str(path.resolve(strict=False)) for path in missing)
            logger.error("ai-preflight-failed run-id=%s task=%s reason=%s", run_id, self.process.task_id, message)
            self._write_status(
                run_id=run_id,
                status="preflight_failed",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                lpr_result_count=0,
                error=message,
            )
            self.on_error(message)
            return False
        if shutil.which(self.process.command[0]) is None:
            message = f"{self.process.command[0]} not found"
            logger.error("ai-preflight-failed run-id=%s task=%s reason=%s", run_id, self.process.task_id, message)
            self._write_status(
                run_id=run_id,
                status="preflight_failed",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                lpr_result_count=0,
                error=message,
            )
            self.on_error(message)
            return False

        self.process.event_path.parent.mkdir(parents=True, exist_ok=True)
        if self.process.event_path.exists():
            self.process.event_path.unlink()
        self.process.log_path.parent.mkdir(parents=True, exist_ok=True)
        _archive_runtime_file(self.process.log_path, run_id)
        if self.process.diagnostic_path is not None:
            _archive_runtime_file(self.process.diagnostic_path, run_id)
        tail = PurposeEventFileTail(self.process.event_path)
        with self.process.log_path.open("w", encoding="utf-8") as log_fp:
            log_fp.write(_launch_log_text(self.process))
            log_fp.flush()
            try:
                self._process = subprocess.Popen(
                    self.process.command,
                    stdout=log_fp,
                    stderr=log_fp,
                    text=True,
                    env=self.process.env,
                    start_new_session=True,
                )
            except OSError as exc:
                message = f"failed to launch {self.process.command[0]}: {exc}"
                logger.exception("ai-launch-failed run-id=%s task=%s", run_id, self.process.task_id)
                self._write_status(
                    run_id=run_id,
                    status="launch_failed",
                    started_at=started_at,
                    returncode=None,
                    duration_seconds=time.monotonic() - started_monotonic,
                    event_counts=event_counts,
                    lpr_result_count=0,
                    error=message,
                )
                self.on_error(message)
                return False
            process_pid = getattr(self._process, "pid", None)
            logger.info(
                "ai-process-start run-id=%s task=%s pid=%s",
                run_id,
                self.process.task_id,
                process_pid,
            )
            self._write_status(
                run_id=run_id,
                status="running",
                started_at=started_at,
                returncode=None,
                duration_seconds=0.0,
                event_counts=event_counts,
                lpr_result_count=0,
                pid=process_pid,
            )

            deadline = None
            if self.process.expected_runtime_seconds is not None:
                deadline = time.monotonic() + self.process.expected_runtime_seconds
            first_event_logged = False
            next_activity_log = time.monotonic() + 30.0
            consecutive_restarts = 0
            recovery_pending = False
            healthy_since: float | None = None

            while self._running:
                events, lpr_results = tail.read_new_events()
                if events:
                    event_counts.update(event.camera_id for event in events)
                    self.on_events(events)
                if lpr_results:
                    lpr_result_count += len(lpr_results)
                    self.on_lpr_results(lpr_results)
                if (events or lpr_results) and not first_event_logged:
                    first_event_logged = True
                    logger.info(
                        "ai-first-result run-id=%s task=%s elapsed-seconds=%.3f events=%s lpr-results=%s",
                        run_id,
                        self.process.task_id,
                        time.monotonic() - started_monotonic,
                        dict(event_counts),
                        lpr_result_count,
                    )
                    self._write_status(
                        run_id=run_id,
                        status="recovering" if recovery_pending else "running",
                        started_at=started_at,
                        returncode=None,
                        duration_seconds=time.monotonic() - started_monotonic,
                        event_counts=event_counts,
                        lpr_result_count=lpr_result_count,
                    )
                fatal_message = _fatal_log_message(self.process.log_path)
                if fatal_message:
                    safe_message = redact_sensitive_text(fatal_message)
                    logger.error(
                        "ai-fatal run-id=%s task=%s log-tail=%s",
                        run_id,
                        self.process.task_id,
                        safe_message,
                    )
                    self.on_error(safe_message)
                    terminal_error = True
                    self._terminate_process(force=True)
                    break
                stall_message = _diagnostic_stall_message(self.process.diagnostic_path)
                if stall_message:
                    if consecutive_restarts < self.process.max_consecutive_restarts:
                        consecutive_restarts += 1
                        logger.warning(
                            "ai-process-restart-request run-id=%s task=%s attempt=%s max-attempts=%s detail=%s",
                            run_id,
                            self.process.task_id,
                            consecutive_restarts,
                            self.process.max_consecutive_restarts,
                            stall_message,
                        )
                        self.on_status(
                            f"AI 입력 스트림 복구 중 "
                            f"({consecutive_restarts}/{self.process.max_consecutive_restarts})"
                        )
                        self._write_status(
                            run_id=run_id,
                            status="recovering",
                            started_at=started_at,
                            returncode=None,
                            duration_seconds=time.monotonic() - started_monotonic,
                            event_counts=event_counts,
                            lpr_result_count=lpr_result_count,
                            error=stall_message,
                        )
                        self._terminate_process(force=True)
                        if self.process.diagnostic_path is not None:
                            _archive_runtime_file(
                                self.process.diagnostic_path,
                                f"{run_id}.restart-{consecutive_restarts}",
                            )
                        if not self._wait_before_restart(self.process.restart_delay_seconds):
                            break
                        try:
                            log_fp.write(
                                f"\nPROCESS_RESTART attempt={consecutive_restarts} "
                                f"reason=pipeline-stall\n"
                            )
                            log_fp.flush()
                            self._process = subprocess.Popen(
                                self.process.command,
                                stdout=log_fp,
                                stderr=log_fp,
                                text=True,
                                env=self.process.env,
                                start_new_session=True,
                            )
                        except OSError as exc:
                            message = f"failed to restart {self.process.command[0]}: {exc}"
                            logger.exception(
                                "ai-process-restart-failed run-id=%s task=%s attempt=%s",
                                run_id,
                                self.process.task_id,
                                consecutive_restarts,
                            )
                            self.on_error(message)
                            terminal_error = True
                            break
                        recovery_pending = True
                        healthy_since = None
                        logger.info(
                            "ai-process-restart run-id=%s task=%s attempt=%s pid=%s",
                            run_id,
                            self.process.task_id,
                            consecutive_restarts,
                            getattr(self._process, "pid", None),
                        )
                        continue
                    logger.error(
                        "ai-stall run-id=%s task=%s restart-attempts=%s detail=%s",
                        run_id,
                        self.process.task_id,
                        consecutive_restarts,
                        stall_message,
                    )
                    self.on_error(stall_message)
                    terminal_error = True
                    self._terminate_process(force=True)
                    break
                diagnostic_status = _diagnostic_status(self.process.diagnostic_path)
                if diagnostic_status == "running":
                    now = time.monotonic()
                    if healthy_since is None:
                        healthy_since = now
                    if recovery_pending:
                        recovery_pending = False
                        logger.info(
                            "ai-process-recovered run-id=%s task=%s attempts=%s pid=%s",
                            run_id,
                            self.process.task_id,
                            consecutive_restarts,
                            getattr(self._process, "pid", None),
                        )
                        self.on_status("AI 입력 스트림 복구 완료")
                        self._write_status(
                            run_id=run_id,
                            status="running",
                            started_at=started_at,
                            returncode=None,
                            duration_seconds=now - started_monotonic,
                            event_counts=event_counts,
                            lpr_result_count=lpr_result_count,
                        )
                    if (
                        consecutive_restarts
                        and now - healthy_since >= self.process.restart_stability_seconds
                    ):
                        consecutive_restarts = 0
                else:
                    healthy_since = None
                if self._process.poll() is not None:
                    if recovery_pending:
                        message = "AI recovery process exited before all required camera heartbeats became healthy"
                        logger.error(
                            "ai-process-recovery-incomplete run-id=%s task=%s returncode=%s",
                            run_id,
                            self.process.task_id,
                            self._process.returncode,
                        )
                        self.on_error(message)
                        terminal_error = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if time.monotonic() >= next_activity_log:
                    log_method = logger.warning if not event_counts and not lpr_result_count else logger.info
                    log_method(
                        "ai-activity run-id=%s task=%s process-alive=true events=%s lpr-results=%s elapsed-seconds=%.1f",
                        run_id,
                        self.process.task_id,
                        dict(event_counts),
                        lpr_result_count,
                        time.monotonic() - started_monotonic,
                    )
                    self._write_status(
                        run_id=run_id,
                        status="recovering" if recovery_pending else "running",
                        started_at=started_at,
                        returncode=None,
                        duration_seconds=time.monotonic() - started_monotonic,
                        event_counts=event_counts,
                        lpr_result_count=lpr_result_count,
                    )
                    next_activity_log = time.monotonic() + 30.0
                time.sleep(self.poll_seconds)

            events, lpr_results = tail.read_new_events()
            if events:
                event_counts.update(event.camera_id for event in events)
                self.on_events(events)
            if lpr_results:
                lpr_result_count += len(lpr_results)
                self.on_lpr_results(lpr_results)
            if self._running and self._process.poll() not in (None, 0):
                message = redact_sensitive_text(_process_error_message(self._process.returncode, self.process.log_path))
                logger.error(
                    "ai-process-error run-id=%s task=%s returncode=%s log-tail=%s",
                    run_id,
                    self.process.task_id,
                    self._process.returncode,
                    message,
                )
                self.on_error(message)
                terminal_error = True
        self._terminate_process()
        if self.process.task_id == PURPOSE_LPR_IMAGE:
            _append_lpr_image_summary(self.process)
        returncode = getattr(self._process, "returncode", None) if self._process is not None else None
        duration_seconds = time.monotonic() - started_monotonic
        if not self._running:
            status = "stopped"
        elif terminal_error or returncode != 0:
            status = "failed"
        elif self.process.task_id == PURPOSE_LPR_IMAGE and lpr_result_count == 0:
            status = "no_result"
        else:
            status = "completed"
        logger.info(
            "ai-process-end run-id=%s task=%s status=%s returncode=%s duration-seconds=%.3f events=%s lpr-results=%s",
            run_id,
            self.process.task_id,
            status,
            returncode,
            duration_seconds,
            dict(event_counts),
            lpr_result_count,
        )
        self._write_status(
            run_id=run_id,
            status=status,
            started_at=started_at,
            returncode=returncode,
            duration_seconds=duration_seconds,
            event_counts=event_counts,
            lpr_result_count=lpr_result_count,
        )
        return True

    def _wait_before_restart(self, delay_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, delay_seconds)
        while self._running and time.monotonic() < deadline:
            time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))
        return self._running

    def _write_status(
        self,
        *,
        run_id: str,
        status: str,
        started_at: datetime,
        returncode: int | None,
        duration_seconds: float,
        event_counts: Counter[str],
        lpr_result_count: int,
        pid: int | None = None,
        error: str | None = None,
    ) -> None:
        if self.process.status_path is None:
            return
        effective_pid = pid if pid is not None else (getattr(self._process, "pid", None) if self._process is not None else None)
        write_run_status(
            self.process.status_path,
            run_id=run_id,
            task_id=self.process.task_id,
            status=status,
            started_at=started_at.isoformat(),
            pid=effective_pid,
            returncode=returncode,
            duration_seconds=round(duration_seconds, 3),
            event_counts=dict(event_counts),
            event_total=sum(event_counts.values()),
            lpr_result_count=lpr_result_count,
            event_path=str(self.process.event_path.resolve(strict=False)),
            log_path=str(self.process.log_path.resolve(strict=False)),
            error=error,
        )

    def _terminate_process(self, *, force: bool = False) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
        if not force:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def vehicle_detection_process(
    settings: Settings,
    camera: CameraConfig,
    *,
    event_dir: Path = DEFAULT_PURPOSE_TASK_DIR / PURPOSE_VEHICLE_DETECTION,
    rotation_degrees: int | None = None,
    latency_ms: int = 100,
    min_confidence: float = 0.1,
    gst_launch: str = "gst-launch-1.0",
) -> PurposeInferenceProcess:
    hef_path = settings.hailo_vehicle_detection_hef_path
    postprocess_so = settings.hailo_vehicle_detection_postprocess_so
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "vehicle.jsonl"
    log_path = event_dir / "vehicle.gst.log"
    diagnostic_path = event_dir / "vehicle.heartbeat.jsonl"
    command = hailo_apps_detection_command(
        settings,
        (camera,),
        event_path=event_path,
        hef_path=hef_path,
        postprocess_so=postprocess_so,
        min_confidence=min_confidence,
        allowed_labels=VEHICLE_LABELS,
        camera_rotations={
            camera.id: camera.rotation_degrees if rotation_degrees is None else rotation_degrees,
        },
        diagnostic_path=diagnostic_path,
    )
    env = hailo_apps_runtime_env(settings)
    return PurposeInferenceProcess(
        task_id=PURPOSE_VEHICLE_DETECTION,
        label=PURPOSE_TASK_SPECS[PURPOSE_VEHICLE_DETECTION].label,
        command=command,
        env=env,
        log_path=log_path,
        event_path=event_path,
        model_paths=(hef_path, postprocess_so),
        camera_ids=(camera.id,),
        run_id=new_run_id(PURPOSE_VEHICLE_DETECTION),
        status_path=event_dir / "run-status.json",
        diagnostic_path=diagnostic_path,
        validate_resource_paths=True,
        max_consecutive_restarts=3,
    )


def lpr_image_process(
    settings: Settings,
    *,
    image_dir: Path = Path("tmp/car_number-test"),
    event_dir: Path = DEFAULT_PURPOSE_TASK_DIR / PURPOSE_LPR_IMAGE,
    gst_launch: str = "gst-launch-1.0",
) -> PurposeInferenceProcess:
    event_dir.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id(PURPOSE_LPR_IMAGE)
    prepared = _prepare_lpr_images(image_dir, event_dir)
    if not prepared:
        raise ValueError(f"LPR test image not found: {image_dir}")
    event_path = event_dir / "lpr.jsonl"
    log_path = event_dir / "lpr.gst.log"
    manifest_path = _write_lpr_manifest(
        event_dir=event_dir,
        prepared=prepared,
        detector_model=settings.fast_alpr_detector_model,
        ocr_model=settings.fast_alpr_ocr_model,
    )
    command = (
        sys.executable,
        "-m",
        "towersightai.cli.fast_alpr_lpr",
        "--image-dir",
        str(image_dir),
        "--event-path",
        str(event_path),
        "--log-path",
        str(log_path),
        "--manifest-path",
        str(manifest_path),
        "--detector-model",
        settings.fast_alpr_detector_model,
        "--ocr-model",
        settings.fast_alpr_ocr_model,
        "--run-id",
        run_id,
        "--append-log",
    )
    env = os.environ.copy()
    return PurposeInferenceProcess(
        task_id=PURPOSE_LPR_IMAGE,
        label=PURPOSE_TASK_SPECS[PURPOSE_LPR_IMAGE].label,
        command=command,
        env=env,
        log_path=log_path,
        event_path=event_path,
        model_paths=(),
        metadata_paths=(manifest_path,),
        run_id=run_id,
        status_path=event_dir / "run-status.json",
        validate_resource_paths=True,
    )


def person_presence_process(
    settings: Settings,
    cameras: tuple[CameraConfig, ...],
    *,
    event_dir: Path = DEFAULT_PURPOSE_TASK_DIR / PURPOSE_PERSON_PRESENCE,
    camera_rotations: dict[str, int] | None = None,
    latency_ms: int = 100,
    min_confidence: float = 0.3,
    gst_launch: str = "gst-launch-1.0",
) -> PurposeInferenceProcess:
    if not cameras:
        raise ValueError("At least one camera is required for person presence detection.")
    hef_path = settings.hailo_person_presence_hef_path
    postprocess_so = settings.hailo_person_presence_postprocess_so
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "person_presence.jsonl"
    log_path = event_dir / "person_presence.gst.log"
    diagnostic_path = event_dir / "person_presence.heartbeat.jsonl"
    command = hailo_apps_detection_command(
        settings,
        cameras,
        event_path=event_path,
        hef_path=hef_path,
        postprocess_so=postprocess_so,
        min_confidence=min_confidence,
        allowed_labels=PERSON_LABELS,
        camera_rotations=camera_rotations,
        diagnostic_path=diagnostic_path,
    )
    env = hailo_apps_runtime_env(settings)
    return PurposeInferenceProcess(
        task_id=PURPOSE_PERSON_PRESENCE,
        label=PURPOSE_TASK_SPECS[PURPOSE_PERSON_PRESENCE].label,
        command=command,
        env=env,
        log_path=log_path,
        event_path=event_path,
        model_paths=(hef_path, postprocess_so),
        camera_ids=tuple(camera.id for camera in cameras),
        run_id=new_run_id(PURPOSE_PERSON_PRESENCE),
        status_path=event_dir / "run-status.json",
        diagnostic_path=diagnostic_path,
        validate_resource_paths=True,
        max_consecutive_restarts=3,
    )


def build_purpose_process(
    task_id: str,
    settings: Settings,
    *,
    cameras: tuple[CameraConfig, ...] = (),
    camera_rotations: dict[str, int] | None = None,
    image_dir: Path = Path("tmp/car_number-test"),
    event_dir: Path = DEFAULT_PURPOSE_TASK_DIR,
) -> PurposeInferenceProcess:
    if task_id == PURPOSE_VEHICLE_DETECTION:
        camera = _front_camera(cameras or tuple(settings.cameras))
        return vehicle_detection_process(
            settings,
            camera,
            event_dir=event_dir / PURPOSE_VEHICLE_DETECTION,
            rotation_degrees=(camera_rotations or {}).get(camera.id, camera.rotation_degrees),
        )
    if task_id == PURPOSE_LPR_IMAGE:
        return lpr_image_process(settings, image_dir=image_dir, event_dir=event_dir / PURPOSE_LPR_IMAGE)
    if task_id == PURPOSE_PERSON_PRESENCE:
        return person_presence_process(
            settings,
            cameras or tuple(settings.cameras),
            event_dir=event_dir / PURPOSE_PERSON_PRESENCE,
            camera_rotations=camera_rotations,
        )
    raise ValueError(f"Unknown purpose AI task: {task_id}")


def _front_camera(cameras: Iterable[CameraConfig]) -> CameraConfig:
    camera_tuple = tuple(cameras)
    for camera in camera_tuple:
        if camera.role.value == "front" or camera.id == "front":
            return camera
    if not camera_tuple:
        raise ValueError("A front camera is required for vehicle detection.")
    return camera_tuple[0]


def _person_presence_source_branch(
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
            f"rtspsrc location={camera.rtsp_url} name=person_source_{index} message-forward=true "
            f"latency={latency_ms} protocols=tcp drop-on-latency=true",
            "! rtph264depay",
            "! h264parse",
            "! decodebin",
            f"! queue name=person_source_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! {orientation}" if orientation else "",
            "! videoscale add-borders=true n-threads=2",
            "! videoconvert n-threads=3",
            f"! {PERSON_PRESENCE_NETWORK_CAPS}",
            f"! queue name=person_roundrobin_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! fun.sink_{index}",
            f"sid.src_{index}",
            f"! queue name=person_callback_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailopython module={callback_module} qos=false",
            f"! queue name=person_sink_q_{index} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! fakesink sync=false",
        )
    )


def _write_person_presence_callback_module(
    *,
    event_dir: Path,
    camera_id: str,
    event_path: Path,
    min_confidence: float,
) -> Path:
    safe_camera_id = "".join(char if char.isalnum() or char == "_" else "_" for char in camera_id)
    module_path = event_dir / f"person_presence_callback_{safe_camera_id}.py"
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
                "        allowed_labels=('person', 'human'),",
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


def _prepare_lpr_images(image_dir: Path, event_dir: Path, *, frames_per_image: int = 8) -> tuple[LprPreparedFrame, ...]:
    extensions = {".png", ".jpg", ".jpeg", ".bmp"}
    source_paths = tuple(sorted(path for path in image_dir.iterdir() if path.suffix.lower() in extensions)) if image_dir.exists() else ()
    prepared: list[LprPreparedFrame] = []
    for source_index, source_path in enumerate(source_paths):
        prepared.append(
            LprPreparedFrame(
                source_path=source_path,
                frame_path=source_path,
                source_index=source_index,
                repeat_index=0,
            )
        )
    return tuple(prepared)


def _write_lpr_manifest(
    *,
    event_dir: Path,
    prepared: tuple[LprPreparedFrame, ...],
    detector_model: str,
    ocr_model: str,
) -> Path:
    manifest_path = event_dir / "lpr_manifest.json"
    images: dict[str, dict[str, Any]] = {}
    frames: list[dict[str, Any]] = []
    for frame_index, item in enumerate(prepared):
        key = str(item.source_path)
        images.setdefault(
            key,
            {
                "image_index": item.source_index,
                "source_image": key,
            },
        )
        frames.append(
            {
                "image_index": item.source_index,
                "source_image": key,
            }
        )
    payload = {
        "type": "fast_alpr_image_manifest",
        "created_at": datetime.now().isoformat(),
        "detector_model": detector_model,
        "ocr_model": ocr_model,
        "images": tuple(sorted(images.values(), key=lambda image: image["image_index"])),
        "frames": tuple(frames),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def _write_lpr_ocr_callback_module(
    *,
    event_dir: Path,
    event_path: Path,
    prepared: tuple[LprPreparedFrame, ...],
    min_confidence: float = 0.0,
) -> Path:
    module_path = event_dir / "lpr_ocr_callback.py"
    frame_sources = tuple(str(item.source_path) for item in prepared)
    module_path.write_text(
        "\n".join(
            (
                "from towersightai.inference.callback import run_lpr_ocr_with_config",
                "",
                f"_FRAME_SOURCES = {frame_sources!r}",
                "_FRAME_INDEX = 0",
                "",
                "def run(video_frame):",
                "    global _FRAME_INDEX",
                "    frame_index = _FRAME_INDEX",
                "    _FRAME_INDEX += 1",
                "    source_image = _FRAME_SOURCES[frame_index] if frame_index < len(_FRAME_SOURCES) else None",
                "    return run_lpr_ocr_with_config(",
                "        video_frame,",
                f"        event_path={str(event_path)!r},",
                f"        min_confidence={min_confidence!r},",
                "        source_image=source_image,",
                "        frame_index=frame_index,",
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


def parse_plate_ocr_json(line: str) -> PlateOcrEvent | None:
    try:
        payload: dict[str, Any] = json.loads(line)
        if payload.get("type") != "plate_ocr":
            return None
        plate_number = str(payload["plate_number"]).strip()
        if not plate_number:
            return None
        return PlateOcrEvent(
            plate_number=plate_number,
            confidence=float(payload["confidence"]),
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
            source=str(payload.get("source", "hailo_lpr_image")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class PurposeEventFileTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0

    def read_new_events(self) -> tuple[tuple[DetectionEvent, ...], tuple[PlateOcrEvent, ...]]:
        if not self.path.exists():
            return (), ()
        detections: list[DetectionEvent] = []
        lpr_results: list[PlateOcrEvent] = []
        with self.path.open("r", encoding="utf-8") as fp:
            fp.seek(self._offset)
            for line in fp:
                detection = parse_detection_json(line)
                if detection is not None:
                    detections.append(detection)
                    continue
                lpr_result = parse_plate_ocr_json(line)
                if lpr_result is not None:
                    lpr_results.append(lpr_result)
            self._offset = fp.tell()
        return tuple(detections), tuple(lpr_results)


def _write_vehicle_callback_module(
    *,
    event_dir: Path,
    camera_id: str,
    event_path: Path,
    min_confidence: float,
) -> Path:
    module_path = event_dir / "vehicle_callback.py"
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


def _launch_log_text(process: PurposeInferenceProcess) -> str:
    models = ", ".join(str(path) for path in process.model_paths)
    metadata = ", ".join(str(path) for path in process.metadata_paths) if process.metadata_paths else "-"
    return "\n".join(
        (
            "",
            "TowerSightAI purpose AI launch",
            f"run-id={process.run_id or '-'}",
            f"task-id={process.task_id}",
            f"task-label={process.label}",
            f"cwd={Path.cwd()}",
            f"python={sys.executable}",
            f"active-model-paths={models}",
            *(f"resource={path_diagnostic(path)}" for path in process.model_paths),
            f"metadata-paths={metadata}",
            f"event-path={process.event_path}",
            f"virtualenv={process.env.get('VIRTUAL_ENV', 'not-used')}",
            f"executable={shutil.which(process.command[0]) or 'missing'}",
            f"command-redacted={_redacted_command_text(process.command)}",
            "",
        )
    )


def _append_lpr_image_summary(process: PurposeInferenceProcess) -> None:
    manifest_path = process.metadata_paths[0] if process.metadata_paths else process.event_path.parent / "lpr_manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    images = tuple(manifest.get("images") or ())
    attempts_by_image: dict[str, list[dict[str, Any]]] = {str(image.get("source_image")): [] for image in images}
    if process.event_path.exists():
        with process.event_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "plate_ocr_attempt":
                    continue
                source_image = str(payload.get("source_image") or "")
                attempts_by_image.setdefault(source_image, []).append(payload)

    lines = ["", "TowerSightAI LPR image summary"]
    for image in images:
        source_image = str(image.get("source_image") or "")
        attempts = attempts_by_image.get(source_image, [])
        recognized: list[dict[str, Any]] = []
        for attempt in attempts:
            best_plate = attempt.get("best_plate")
            if isinstance(best_plate, dict) and best_plate.get("plate_number"):
                recognized.append(best_plate)
        if recognized:
            result = ", ".join(
                f"{plate.get('plate_number')}:{float(plate.get('confidence', 0.0)):.2f}" for plate in recognized
            )
            status = "recognized"
        elif attempts:
            result = "no_result"
            status = "no_result"
        else:
            result = "no_callback_result"
            status = "no_callback_result"
        lines.append(
            " ".join(
                (
                    f"image[{image.get('image_index')}]={source_image}",
                    f"attempts={len(attempts)}",
                    f"status={status}",
                    f"result={result}",
                )
            )
        )
    lines.append("")
    with process.log_path.open("a", encoding="utf-8") as fp:
        fp.write("\n".join(lines))


def _redacted_command_text(command: tuple[str, ...]) -> str:
    return _redact_rtsp_credentials(" ".join(shlex.quote(part) for part in command))


def _redact_rtsp_credentials(text: str) -> str:
    return re.sub(r"(rtsp://)([^@\s/]+)@", r"\1***:***@", text)


def _process_error_message(returncode: int | None, log_path: Path) -> str:
    log_tail = _read_log_tail(log_path)
    if log_tail:
        return log_tail
    return f"gst-launch exited with {returncode}"


def _fatal_log_message(log_path: Path) -> str:
    log_tail = _read_log_tail(log_path)
    if not log_tail:
        return ""
    for pattern in FATAL_GSTREAMER_PATTERNS:
        if pattern in log_tail:
            return log_tail
    return ""


def _diagnostic_stall_message(path: Path | None) -> str:
    payload = _diagnostic_payload(path)
    if payload.get("status") != "stalled":
        return ""
    stale = ",".join(str(value) for value in payload.get("stale_cameras") or ())
    cameras = payload.get("cameras") or {}
    stages = payload.get("stages") or {}
    stage_summary = {
        stage: {
            "buffers": detail.get("buffers"),
            "age_seconds": detail.get("age_seconds"),
        }
        for stage, detail in stages.items()
        if isinstance(detail, dict)
    }
    queue_levels = payload.get("queue_levels") or {}
    return (
        f"AI pipeline stalled; stale-cameras={stale or '-'} "
        f"heartbeat={json.dumps(cameras, sort_keys=True)} "
        f"stages={json.dumps(stage_summary, sort_keys=True)} "
        f"queue-levels={json.dumps(queue_levels, sort_keys=True)}"
    )


def _diagnostic_status(path: Path | None) -> str:
    return str(_diagnostic_payload(path).get("status") or "")


def _diagnostic_payload(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        last_line = _read_last_text_line(path)
        payload = json.loads(last_line) if last_line else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_last_text_line(path: Path, *, max_bytes: int = 256 * 1024) -> str:
    with path.open("rb") as fp:
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        fp.seek(max(0, size - max_bytes))
        chunk = fp.read()
    lines = chunk.splitlines()
    if not lines:
        return ""
    return lines[-1].decode("utf-8", errors="replace")


def _archive_runtime_file(path: Path, run_id: str) -> Path | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    archive = path.with_name(f"{path.name}.{run_id}.previous")
    path.replace(archive)
    try:
        archived_text = archive.read_text(encoding="utf-8", errors="replace")
        redacted_text = redact_sensitive_text(archived_text)
        if redacted_text != archived_text:
            archive.write_text(redacted_text, encoding="utf-8")
    except OSError:
        logging.getLogger("towersightai.ai.purpose").warning(
            "ai-log-archive-redaction-failed path=%s",
            archive,
        )
    return archive
