from __future__ import annotations

import os
import json
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from towersightai.camera.pipeline import display_orientation_element
from towersightai.config.settings import CameraConfig, Settings
from towersightai.inference.events import DetectionEvent
from towersightai.inference.image_smoke import NETWORK_FORMAT, NETWORK_HEIGHT, NETWORK_WIDTH, _gst_runtime_env
from towersightai.inference.live_detection import _read_log_tail, latest_events, parse_detection_json


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
    "파이프라인이 재생을 원하지 않음",
    "파이프라인이 PREROLL하기를 원하지 않음",
    "Internal data stream error",
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
        "front 카메라에서 Hailo LPR 예제의 yolov5m_vehicles 모델을 사용합니다.",
    ),
    PURPOSE_LPR_IMAGE: PurposeTaskSpec(
        PURPOSE_LPR_IMAGE,
        "번호판 이미지 LPR",
        "tmp/car_number-test 이미지를 FastALPR ONNX 모델로 순차 실행합니다.",
    ),
    PURPOSE_PERSON_PRESENCE: PurposeTaskSpec(
        PURPOSE_PERSON_PRESENCE,
        "사람 존재 감지",
        "정상 수신 중인 카메라에서 TAPPAS person detector로 사람 존재 여부를 판단합니다.",
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
        poll_seconds: float = 0.1,
    ) -> None:
        self.process = process
        self.on_events = on_events
        self.on_lpr_results = on_lpr_results or (lambda _events: None)
        self.on_error = on_error
        self.poll_seconds = poll_seconds
        self._running = True
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._running = False
        self._terminate_process()

    def run(self) -> bool:
        if shutil.which(self.process.command[0]) is None:
            self.on_error(f"{self.process.command[0]} not found")
            return False

        self.process.event_path.parent.mkdir(parents=True, exist_ok=True)
        if self.process.event_path.exists():
            self.process.event_path.unlink()
        self.process.log_path.parent.mkdir(parents=True, exist_ok=True)
        tail = PurposeEventFileTail(self.process.event_path)
        with self.process.log_path.open("w", encoding="utf-8") as log_fp:
            log_fp.write(_launch_log_text(self.process))
            log_fp.flush()
            self._process = subprocess.Popen(
                self.process.command,
                stdout=log_fp,
                stderr=log_fp,
                text=True,
                env=self.process.env,
                start_new_session=True,
            )

            deadline = None
            if self.process.expected_runtime_seconds is not None:
                deadline = time.monotonic() + self.process.expected_runtime_seconds

            while self._running:
                events, lpr_results = tail.read_new_events()
                if events:
                    self.on_events(events)
                if lpr_results:
                    self.on_lpr_results(lpr_results)
                fatal_message = _fatal_log_message(self.process.log_path)
                if fatal_message:
                    self.on_error(fatal_message)
                    self._terminate_process(force=True)
                    break
                if self._process.poll() is not None:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(self.poll_seconds)

            events, lpr_results = tail.read_new_events()
            if events:
                self.on_events(events)
            if lpr_results:
                self.on_lpr_results(lpr_results)
            if self._running and self._process.poll() not in (None, 0):
                self.on_error(_process_error_message(self._process.returncode, self.process.log_path))
        self._terminate_process()
        if self.process.task_id == PURPOSE_LPR_IMAGE:
            _append_lpr_image_summary(self.process)
        return True

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
    resources = _lpr_resources(settings)
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "vehicle.jsonl"
    log_path = event_dir / "vehicle.gst.log"
    callback_module = _write_vehicle_callback_module(
        event_dir=event_dir,
        camera_id=camera.id,
        event_path=event_path,
        min_confidence=min_confidence,
    )
    rotation = camera.rotation_degrees if rotation_degrees is None else rotation_degrees
    orientation = display_orientation_element(rotation)
    pipeline = " ".join(
        (
            f"rtspsrc location={camera.rtsp_url} latency={latency_ms} protocols=tcp drop-on-latency=true",
            "! rtph264depay",
            "! h264parse",
            "! decodebin",
            "! queue name=vehicle_decode_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! {orientation}" if orientation else "",
            "! videoscale n-threads=2",
            "! video/x-raw,pixel-aspect-ratio=1/1",
            "! videoconvert n-threads=3",
            "! queue name=vehicle_hailonet_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={resources / 'yolov5m_vehicles.hef'} batch-size=1 nms-score-threshold={min_confidence} "
            "nms-iou-threshold=0.45 output-format-type=HAILO_FORMAT_TYPE_FLOAT32",
            "! queue name=vehicle_hailofilter_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/libyolo_hailortpp_post.so'} "
            f"config-path={resources / 'configs/yolov5_vehicle_detection.json'} function-name=yolov5m_vehicles qos=false",
            "! queue name=vehicle_callback_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailopython module={callback_module} qos=false",
            "! queue name=vehicle_sink_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! fakesink sync=false",
        )
    )
    env = _gst_runtime_env(settings)
    return PurposeInferenceProcess(
        task_id=PURPOSE_VEHICLE_DETECTION,
        label=PURPOSE_TASK_SPECS[PURPOSE_VEHICLE_DETECTION].label,
        command=(gst_launch, "-q", *shlex.split(pipeline)),
        env=env,
        log_path=log_path,
        event_path=event_path,
        model_paths=(resources / "yolov5m_vehicles.hef",),
        camera_ids=(camera.id,),
    )


def lpr_image_process(
    settings: Settings,
    *,
    image_dir: Path = Path("tmp/car_number-test"),
    event_dir: Path = DEFAULT_PURPOSE_TASK_DIR / PURPOSE_LPR_IMAGE,
    gst_launch: str = "gst-launch-1.0",
) -> PurposeInferenceProcess:
    event_dir.mkdir(parents=True, exist_ok=True)
    prepared = _prepare_lpr_images(image_dir, event_dir)
    if not prepared:
        raise ValueError(f"LPR test image not found: {image_dir}")
    event_path = event_dir / "lpr.jsonl"
    log_path = event_dir / "lpr.gst.log"
    manifest_path = _write_lpr_manifest(event_dir=event_dir, prepared=prepared)
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
    )


def _legacy_hailo_lpr_pipeline(settings: Settings, resources: Path, callback_module: Path, location: Path, stop_index: int) -> str:
    return " ".join(
        (
            f"multifilesrc location={location} index=0 stop-index={stop_index} caps=image/png,framerate=1/1",
            "! pngdec",
            "! videoscale n-threads=2",
            "! video/x-raw,pixel-aspect-ratio=1/1",
            "! videoconvert n-threads=3",
            "! queue name=lpr_vehicle_hailonet_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={resources / 'yolov5m_vehicles.hef'} vdevice-group-id=1 scheduling-algorithm=1 "
            "scheduler-threshold=1 scheduler-timeout-ms=100 batch-size=1 nms-score-threshold=0.3 "
            "nms-iou-threshold=0.45 output-format-type=HAILO_FORMAT_TYPE_FLOAT32",
            "! queue name=lpr_vehicle_hailofilter_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/libyolo_hailortpp_post.so'} "
            f"config-path={resources / 'configs/yolov5_vehicle_detection.json'} function-name=yolov5m_vehicles qos=false",
            "! queue name=lpr_tracker_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! hailotracker name=hailo_tracker keep-past-metadata=true kalman-dist-thr=.5 iou-thr=.6 keep-tracked-frames=2 keep-lost-frames=2",
            "! queue name=lpr_tee_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! tee name=context_tee",
            "context_tee.",
            "! queue name=lpr_overlay_sink_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! fakesink sync=false async=false",
            "context_tee.",
            "! queue name=lpr_plate_crop_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailocropper so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/cropping_algorithms/liblpr_croppers.so'} "
            "function-name=vehicles_without_ocr internal-offset=true drop-uncropped-buffers=true name=cropper1",
            "hailoaggregator name=agg1",
            "cropper1.",
            "! queue name=lpr_lp_bypass_q leaky=no max-size-buffers=50 max-size-bytes=0 max-size-time=0",
            "! agg1.",
            "cropper1.",
            "! queue name=lpr_plate_hailonet_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={resources / 'tiny_yolov4_license_plates.hef'} vdevice-group-id=1 scheduling-algorithm=1 "
            "scheduler-threshold=5 scheduler-timeout-ms=100",
            "! queue name=lpr_plate_hailofilter_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/libyolo_post.so'} "
            f"config-path={resources / 'configs/yolov4_license_plate.json'} function-name=tiny_yolov4_license_plates qos=false",
            "! queue name=lpr_plate_to_agg_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! agg1.",
            "agg1.",
            "! queue name=lpr_ocr_crop_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailocropper so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/cropping_algorithms/liblpr_croppers.so'} "
            "function-name=license_plate_quality_estimation internal-offset=true drop-uncropped-buffers=true name=cropper2",
            "hailoaggregator name=agg2",
            "cropper2.",
            "! queue name=lpr_ocr_bypass_q leaky=no max-size-buffers=50 max-size-bytes=0 max-size-time=0",
            "! agg2.",
            "cropper2.",
            "! queue name=lpr_ocr_hailonet_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={resources / 'lprnet.hef'} vdevice-group-id=1 scheduling-algorithm=1 "
            "scheduler-threshold=1 scheduler-timeout-ms=100",
            "! queue name=lpr_ocr_post_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/libocr_post.so'} qos=false",
            "! queue name=lpr_ocr_to_agg_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! agg2.",
            "agg2.",
            "! queue name=lpr_ocr_callback_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailopython module={callback_module} qos=false",
            "! queue name=lpr_ocrsink_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter use-gst-buffer=true so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/apps/license_plate_recognition/liblpr_ocrsink.so'} qos=false",
            "! fakesink sync=false",
        )
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
    resources = _person_presence_resources(settings)
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "person_presence.jsonl"
    log_path = event_dir / "person_presence.gst.log"
    callback_modules = {
        camera.id: _write_person_presence_callback_module(
            event_dir=event_dir,
            camera_id=camera.id,
            event_path=event_path,
            min_confidence=min_confidence,
        )
        for camera in cameras
    }
    source_branches = " ".join(
        _person_presence_source_branch(
            camera,
            index=index,
            callback_module=callback_modules[camera.id],
            latency_ms=latency_ms,
            rotation_degrees=(camera_rotations or {}).get(camera.id, camera.rotation_degrees),
        )
        for index, camera in enumerate(cameras)
    )
    streamrouter_inputs = " ".join(f'src_{index}::input-streams="<sink_{index}>"' for index, _camera in enumerate(cameras))
    pipeline = " ".join(
        (
            "hailoroundrobin mode=0 name=fun",
            "! queue name=person_pre_convert_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! videoconvert n-threads=1 qos=false",
            f"! {PERSON_PRESENCE_NETWORK_CAPS}",
            "! queue name=person_pre_cropper_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailocropper so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/cropping_algorithms/libwhole_buffer.so'} "
            "function-name=create_crops use-letterbox=true resize-method=inter-area internal-offset=true name=cropper1",
            "hailoaggregator name=agg1",
            "cropper1.",
            "! queue name=person_detector_bypass_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! agg1.",
            "cropper1.",
            "! queue name=person_detector_q leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={resources / 'yolov5s_personface_reid.hef'} scheduling-algorithm=1 "
            "vdevice-group-id=1 force-writable=true",
            "! queue name=person_detector_post_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailofilter so-path={settings.tappas_workspace / 'apps/h8/gstreamer/libs/post_processes/libyolo_post.so'} "
            f"config-path={resources / 'configs/yolov5_personface.json'} function-name=yolov5_personface_letterbox qos=false",
            "! queue name=person_detector_to_agg_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            "! agg1.",
            "agg1.",
            "! queue name=person_router_q leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0",
            f"! hailostreamrouter name=sid {streamrouter_inputs}",
            source_branches,
        )
    )
    env = _gst_runtime_env(settings)
    return PurposeInferenceProcess(
        task_id=PURPOSE_PERSON_PRESENCE,
        label=PURPOSE_TASK_SPECS[PURPOSE_PERSON_PRESENCE].label,
        command=(gst_launch, "-q", *shlex.split(pipeline)),
        env=env,
        log_path=log_path,
        event_path=event_path,
        model_paths=(resources / "yolov5s_personface_reid.hef",),
        camera_ids=tuple(camera.id for camera in cameras),
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


def _lpr_resources(settings: Settings) -> Path:
    return settings.tappas_workspace / "apps/h8/gstreamer/general/license_plate_recognition/resources"


def _person_presence_resources(settings: Settings) -> Path:
    return settings.tappas_workspace / "apps/h8/gstreamer/general/multi_person_multi_camera_tracking/resources"


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


def _write_lpr_manifest(*, event_dir: Path, prepared: tuple[LprPreparedFrame, ...]) -> Path:
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
        "detector_model": "yolo-v9-t-384-license-plate-end2end",
        "ocr_model": "cct-xs-v2-global-model",
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
            f"task-id={process.task_id}",
            f"task-label={process.label}",
            f"active-model-paths={models}",
            f"metadata-paths={metadata}",
            f"event-path={process.event_path}",
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
    return re.sub(r"(rtsp://)([^:/@\s]+):([^@\s]+)@", r"\1***:***@", text)


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
