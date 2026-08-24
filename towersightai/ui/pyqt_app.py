from __future__ import annotations

import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from towersightai.camera.pipeline import build_preview_pipeline, normalize_rotation_degrees
from towersightai.config.settings import CameraRole, Settings
from towersightai.inference.events import DetectionEvent
from towersightai.inference.live_detection import LiveDetectionRunner, latest_events, live_multistream_detection_process
from towersightai.inference.purpose_tasks import (
    PlateOcrEvent,
    PURPOSE_LPR_IMAGE,
    PURPOSE_PERSON_PRESENCE,
    PURPOSE_TASK_SPECS,
    PURPOSE_VEHICLE_DETECTION,
    PurposeInferenceRunner,
    build_purpose_process,
)
from towersightai.cli.fast_alpr_lpr import run_fast_alpr_lpr
from towersightai.runtime_logging import new_run_id
from towersightai.state_machine.core import ParkingState
from towersightai.storage.evidence import EvidenceCoordinator
from towersightai.storage.raw_data import RawDataManager
from towersightai.ui.model import (
    AlignmentResult,
    GlobalSafetyStatus,
    OperatorDisplayModel,
    build_driver_display,
)

OPERATOR_PANEL_WIDTH = 400
OPERATOR_SIDEBAR_WIDTH = 300
WINDOWED_MAX_WIDTH = 1920
WINDOWED_MAX_HEIGHT = 1024
WINDOWED_DEFAULT_WIDTH = 1440
WINDOWED_DEFAULT_HEIGHT = 900
DETECTION_TTL_SECONDS = 1.0
FIRST_INFERENCE_TIMEOUT_SECONDS = 30.0
PERSON_ALERT_STREAK_THRESHOLD = 2
PERSON_ALERT_STALE_SECONDS = 3.0
SIDEBAR_ACTION_LABELS = (
    "사용자모드",
    "전체 카메라",
    "카메라 설정",
    "이전 AI Detection",
    "차량 전용 검출",
    "번호판 이미지 LPR",
    "정면카메라LPR",
    "사람 존재 감지",
    "차량 진입 시뮬레이션",
    "EMPTY",
    "EMPTY",
)

try:
    from PyQt6.QtCore import QObject, QRect, QSize, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen
    from PyQt6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - exercised only on GUI runtimes.
    raise RuntimeError("PyQt6 is required to launch the TowerSightAI operator UI.") from exc

from towersightai.ui.driver_view import (
    DRIVER_REFERENCE_HEIGHT,
    DRIVER_REFERENCE_WIDTH,
    DRIVER_STYLESHEET,
    DriverPreviewHost,
    DriverView,
)


class CameraSurface(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.display_mode = "contain"
        self.status = "NG: 프레임 대기"
        self._frame: QImage | None = None
        self._detections: tuple[DetectionEvent, ...] = ()
        self._frame_size_text = ""
        self._vehicle_simulation = False
        self.setMinimumSize(360, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def set_status(self, status: str) -> None:
        self.status = status
        self.update()

    def set_frame(self, frame: QImage) -> None:
        self._frame = frame
        self._frame_size_text = f"{frame.width()}x{frame.height()}"
        self.update()

    def current_frame(self) -> QImage | None:
        if self._frame is None or self._frame.isNull():
            return None
        return self._frame.copy()

    def set_detections(self, detections: tuple[DetectionEvent, ...]) -> None:
        self._detections = detections
        self.update()

    def clear_detections(self) -> None:
        self._detections = ()
        self.update()

    def set_vehicle_simulation(self, enabled: bool) -> None:
        self._vehicle_simulation = enabled
        self.update()

    def set_display_mode(self, mode: str) -> None:
        if mode not in {"contain", "cover"}:
            raise ValueError(f"Unsupported camera display mode: {mode}")
        self.display_mode = mode
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101418"))
        content = self.rect().adjusted(10, 10, -10, -10)
        painter.setPen(QPen(QColor("#4b5563"), 2))
        painter.drawRect(content)

        width = content.width()
        height = content.height()
        center_x = content.left() + width // 2
        center_y = content.top() + height // 2
        display_frame = self._frame
        source_crop_rect = None
        if display_frame is not None and not display_frame.isNull():
            if self.display_mode == "cover":
                image_rect = content
                source_crop_rect = _cover_source_rect(display_frame.size(), content.size())
                painter.drawImage(image_rect, display_frame, source_crop_rect)
            else:
                scaled_size = display_frame.size()
                scaled_size.scale(content.size(), Qt.AspectRatioMode.KeepAspectRatio)
                image_rect = content
                image_rect.setSize(scaled_size)
                image_rect.moveCenter(content.center())
                painter.drawImage(image_rect, display_frame)
        else:
            image_rect = content
            painter.setPen(QPen(QColor("#94a3b8"), 1))
            painter.drawLine(center_x, content.top() + 24, center_x, content.bottom() - 24)
            painter.drawLine(content.left() + 36, center_y, content.right() - 36, center_y)

        if self._vehicle_simulation:
            vehicle_color = QColor("#38bdf8")
            if "버드뷰" in self.title:
                vehicle_rect = QRect(
                    content.left() + int(width * 0.40),
                    content.top() + int(height * 0.50),
                    int(width * 0.20),
                    int(height * 0.28),
                )
                painter.fillRect(vehicle_rect, QColor(56, 189, 248, 80))
                painter.setPen(QPen(vehicle_color, 3))
                painter.drawRect(vehicle_rect)
                painter.drawText(vehicle_rect.adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignTop, "진입 차량")
            elif "정면" in self.title:
                vehicle_rect = QRect(
                    content.left() + int(width * 0.30),
                    content.top() + int(height * 0.38),
                    int(width * 0.40),
                    int(height * 0.42),
                )
                painter.fillRect(vehicle_rect, QColor(56, 189, 248, 70))
                painter.setPen(QPen(vehicle_color, 3))
                painter.drawRect(vehicle_rect)
                painter.drawText(vehicle_rect.adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignTop, "차량 접근")

        for detection in _fresh_detections(self._detections):
            frame_size = self._frame.size() if self._frame is not None and not self._frame.isNull() else None
            box = _bbox_to_rect(detection, image_rect, source_size=frame_size, source_crop_rect=source_crop_rect)
            if box is None:
                continue
            color = _detection_color(detection.label)
            painter.setPen(QPen(color, 3))
            painter.drawRect(box)
            text = _detection_label(detection)
            label_rect = QRect(box.left(), max(image_rect.top(), box.top() - 24), min(150, max(90, box.width())), 22)
            painter.fillRect(label_rect, QColor(3, 7, 12, 210))
            painter.setPen(color)
            painter.drawText(label_rect.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter, text)

        if self.status.startswith("NG"):
            painter.fillRect(image_rect, QColor(91, 8, 18, 132))
            painter.setPen(QPen(QColor("#ffffff"), 2))
            fault_text = "영상 수신 불가" if self._frame is None or self._frame.isNull() else "카메라 입력 확인 필요"
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, fault_text)

        painter.setPen(QColor("#e5e7eb"))
        painter.drawText(content.adjusted(12, 10, -12, -10), Qt.AlignmentFlag.AlignTop, self.title)
        if self._frame_size_text:
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(content.adjusted(-140, 10, -12, -10), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, self._frame_size_text)
        painter.setPen(QColor("#f87171" if self.status.startswith("NG") else "#86efac"))
        painter.drawText(content.adjusted(12, -32, -12, -8), Qt.AlignmentFlag.AlignBottom, self.status)


class CameraCaptureWorker(QObject):
    frame_ready = pyqtSignal(str, object)
    status_changed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, settings: Settings, camera_id: str, rotation_degrees: int = 0, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.camera = next(camera for camera in settings.cameras if camera.id == camera_id)
        self.rotation_degrees = rotation_degrees
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            self.status_changed.emit(self.camera.id, "NG: OpenCV 미설치")
            self.finished.emit()
            return

        while self._running:
            capture, using_gstreamer = self._open_capture(cv2)
            if not capture.isOpened():
                capture.release()
                self.status_changed.emit(self.camera.id, "NG: 카메라 연결 이상")
                QThread.sleep(1)
                continue

            self.status_changed.emit(self.camera.id, "정상 수신")
            missed_frames = 0
            while self._running:
                ok, frame = capture.read()
                if not ok or frame is None:
                    missed_frames += 1
                    self.status_changed.emit(self.camera.id, "NG: 프레임 지연")
                    if missed_frames >= 10:
                        break
                    QThread.msleep(100)
                    continue
                missed_frames = 0
                if frame.ndim != 3 or frame.shape[2] < 3:
                    self.status_changed.emit(self.camera.id, "NG: 프레임 형식 오류")
                    QThread.msleep(100)
                    continue
                if not using_gstreamer:
                    frame = _rotate_cv_frame(cv2, frame, self.rotation_degrees)
                if frame.shape[2] == 4:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGBA)
                elif not using_gstreamer:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                height, width, channels = frame.shape
                image_format = QImage.Format.Format_RGBA8888 if channels == 4 else QImage.Format.Format_RGB888
                image = QImage(frame.data, width, height, channels * width, image_format).copy()
                self.frame_ready.emit(self.camera.id, image)
                self.status_changed.emit(self.camera.id, "정상 수신")
            capture.release()
        self.finished.emit()

    def _open_capture(self, cv2):  # noqa: ANN001 - cv2 module is imported lazily in the worker thread.
        pipeline = build_preview_pipeline(
            self.camera,
            resolution=self.settings.ui_camera_resolution,
            rotation_degrees=self.rotation_degrees,
        )
        capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        using_gstreamer = capture.isOpened()
        if not using_gstreamer:
            capture.release()
            if self.camera.role is CameraRole.front:
                capture = cv2.VideoCapture(self.camera.rtsp_url)
            else:
                capture = cv2.VideoCapture()
        return capture, using_gstreamer


class LiveDetectionWorker(QObject):
    detections_ready = pyqtSignal(str, object)
    status_changed = pyqtSignal(str, str)
    detection_started = pyqtSignal(str, str)
    first_inference_ready = pyqtSignal(float)
    finished = pyqtSignal(str)

    def __init__(
        self,
        settings: Settings,
        camera_ids: tuple[str, ...],
        camera_rotations: dict[str, int] | None = None,
        hef_path: Path | None = None,
        legacy_mode: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.camera_ids = camera_ids
        self.camera_rotations = dict(camera_rotations or {})
        self.hef_path = hef_path
        self.legacy_mode = legacy_mode
        self.cameras = tuple(camera for camera in settings.cameras if camera.id in set(camera_ids))
        self._runner: LiveDetectionRunner | None = None
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        if self._runner is not None:
            self._runner.stop()

    def _build_process(self):
        if self.legacy_mode:
            return live_multistream_detection_process(
                self.settings,
                self.cameras,
                camera_rotations=self.camera_rotations,
            )
        return live_multistream_detection_process(
            self.settings,
            self.cameras,
            camera_rotations=self.camera_rotations,
            hef_path=self.hef_path,
        )

    def run(self) -> None:
        attempt = 0
        while not self._stop_requested:
            if self._stop_requested:
                break
            process = self._build_process()
            active_hef = Path(process.hef_path)
            if not self.legacy_mode and f"hef-path={active_hef}" not in " ".join(process.command):
                for camera_id in self.camera_ids:
                    self.status_changed.emit(camera_id, f"AI 추론 실패: 선택 모델이 파이프라인에 반영되지 않았습니다. 선택={active_hef.name}")
                break
            started_at = time.monotonic()
            first_event_sent = False
            if not self.legacy_mode:
                self.detection_started.emit(str(active_hef), str(process.log_path or ""))
            for camera_id in self.camera_ids:
                if self.legacy_mode:
                    self.status_changed.emit(camera_id, "이전 AI Detection 실행 중" if attempt == 0 else "이전 AI Detection 재시도 중")
                else:
                    self.status_changed.emit(camera_id, "AI Detection 실행 중" if attempt == 0 else "AI Detection 재시도 중")

            def on_events(events: tuple[DetectionEvent, ...]) -> None:
                nonlocal first_event_sent
                if events and not first_event_sent:
                    first_event_sent = True
                    self.first_inference_ready.emit(time.monotonic() - started_at)
                grouped: dict[str, list[DetectionEvent]] = {}
                for event in events:
                    grouped.setdefault(event.camera_id, []).append(event)
                for camera_id, camera_events in grouped.items():
                    self.detections_ready.emit(camera_id, latest_events(camera_events))

            def on_error(message: str) -> None:
                for camera_id in self.camera_ids:
                    self.status_changed.emit(camera_id, message)

            self._runner = LiveDetectionRunner(process, on_events=on_events, on_error=on_error)
            try:
                started = self._runner.run()
            except Exception as exc:  # noqa: BLE001 - worker boundary must expose unexpected runtime failures.
                logging.getLogger("towersightai.ai.general").exception(
                    "live-detection-worker-crashed cameras=%s",
                    self.camera_ids,
                )
                for camera_id in self.camera_ids:
                    self.status_changed.emit(camera_id, f"AI detection worker failed: {exc}")
                break
            if self._stop_requested:
                break
            if not started:
                break
            attempt += 1
            for camera_id in self.camera_ids:
                self.status_changed.emit(camera_id, "이전 AI Detection 재시작 대기" if self.legacy_mode else "AI Detection 재시작 대기")
            QThread.msleep(min(5000, 500 * attempt))
        for camera_id in self.camera_ids:
            self.finished.emit(camera_id)


class PurposeInferenceWorker(QObject):
    detections_ready = pyqtSignal(str, object)
    lpr_results_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str, str)
    task_started = pyqtSignal(str, str, str)
    first_inference_ready = pyqtSignal(float)
    finished = pyqtSignal(str)

    def __init__(
        self,
        task_id: str,
        settings: Settings,
        camera_ids: tuple[str, ...],
        camera_rotations: dict[str, int] | None = None,
        image_dir: Path = Path("tmp/car_number-test"),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_id = task_id
        self.settings = settings
        self.camera_ids = camera_ids
        self.camera_rotations = dict(camera_rotations or {})
        self.image_dir = image_dir
        self.cameras = tuple(camera for camera in settings.cameras if camera.id in set(camera_ids))
        self._runner: PurposeInferenceRunner | None = None
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        if self._runner is not None:
            self._runner.stop()

    def _build_process(self):
        return build_purpose_process(
            self.task_id,
            self.settings,
            cameras=self.cameras,
            camera_rotations=self.camera_rotations,
            image_dir=self.image_dir,
        )

    def run(self) -> None:
        try:
            process = self._build_process()
        except (OSError, ValueError) as exc:
            self.status_changed.emit(self.task_id, str(exc))
            self.finished.emit(self.task_id)
            return

        started_at = time.monotonic()
        first_event_sent = False
        lpr_result_sent = False
        failed = False
        self.task_started.emit(process.task_id, process.label, str(process.log_path))
        for camera_id in process.camera_ids:
            self.status_changed.emit(camera_id, f"{process.label} 실행 중")

        def on_events(events: tuple[DetectionEvent, ...]) -> None:
            nonlocal first_event_sent
            if events and not first_event_sent:
                first_event_sent = True
                self.first_inference_ready.emit(time.monotonic() - started_at)
            grouped: dict[str, list[DetectionEvent]] = {}
            for event in events:
                grouped.setdefault(event.camera_id, []).append(event)
            for camera_id, camera_events in grouped.items():
                self.detections_ready.emit(camera_id, latest_events(camera_events))

        def on_lpr_results(events: tuple[PlateOcrEvent, ...]) -> None:
            nonlocal first_event_sent, lpr_result_sent
            if events:
                lpr_result_sent = True
            if events and not first_event_sent:
                first_event_sent = True
                self.first_inference_ready.emit(time.monotonic() - started_at)
            self.lpr_results_ready.emit(events)

        def on_error(message: str) -> None:
            nonlocal failed
            failed = True
            self.status_changed.emit(process.task_id, message)

        def on_status(message: str) -> None:
            for camera_id in process.camera_ids or (process.task_id,):
                self.status_changed.emit(camera_id, message)

        self._runner = PurposeInferenceRunner(
            process,
            on_events=on_events,
            on_lpr_results=on_lpr_results,
            on_error=on_error,
            on_status=on_status,
        )
        try:
            self._runner.run()
        except Exception as exc:  # noqa: BLE001 - worker boundary must expose unexpected runtime failures.
            failed = True
            logging.getLogger("towersightai.ai.purpose").exception(
                "purpose-inference-worker-crashed task=%s",
                process.task_id,
            )
            self.status_changed.emit(process.task_id, f"Purpose AI worker failed: {exc}")
        if not failed and process.task_id == PURPOSE_LPR_IMAGE and not lpr_result_sent and not self._stop_requested:
            self.lpr_results_ready.emit(())
        if not failed and not first_event_sent and process.task_id == PURPOSE_PERSON_PRESENCE and not self._stop_requested:
            self.first_inference_ready.emit(time.monotonic() - started_at)
        self.finished.emit(process.task_id)


class FrontCameraLprWorker(QObject):
    result_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        frame: QImage,
        *,
        event_dir: Path = Path("artifacts/runtime/purpose-ai/front_camera_lpr"),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.frame = frame.copy()
        self.event_dir = event_dir
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        if self._stop_requested:
            self.finished.emit()
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = self.event_dir / f"run-{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = run_dir / f"front-camera-lpr-{stamp}.png"
        event_path = self.event_dir / "lpr.jsonl"
        log_path = self.event_dir / "lpr.gst.log"
        manifest_path = self.event_dir / "lpr_manifest.json"
        status_path = self.event_dir / "run-status.json"
        run_id = new_run_id("front-camera-lpr")
        logger = logging.getLogger("towersightai.ai.front_camera_lpr")
        if not self.frame.save(str(snapshot_path), "PNG"):
            self.result_ready.emit(
                {
                    "ok": False,
                    "message": "정면카메라LPR 실패: 스냅샷 저장 실패",
                    "log_path": str(log_path),
                }
            )
            self.finished.emit()
            return
        self.status_changed.emit(f"정면카메라LPR 실행 중: {snapshot_path}")
        logger.info(
            "front-camera-lpr-start run-id=%s snapshot=%s log=%s",
            run_id,
            snapshot_path.resolve(strict=False),
            log_path.resolve(strict=False),
        )
        try:
            returncode = run_fast_alpr_lpr(
                image_dir=run_dir,
                event_path=event_path,
                log_path=log_path,
                manifest_path=manifest_path,
                run_id=run_id,
                status_path=status_path,
            )
        except Exception as exc:  # noqa: BLE001 - worker boundary must report failures to the operator.
            logger.exception("front-camera-lpr-crashed run-id=%s snapshot=%s", run_id, snapshot_path)
            payload = {
                "ok": False,
                "message": f"Front camera LPR failed: {exc}",
            }
        else:
            payload = _front_lpr_payload(event_path)
            if returncode != 0:
                payload = {
                    "ok": False,
                    "message": f"Front camera LPR failed: FastALPR exit code {returncode}",
                }
            logger.info(
                "front-camera-lpr-end run-id=%s returncode=%s ok=%s",
                run_id,
                returncode,
                payload.get("ok"),
            )
        payload["snapshot_path"] = str(snapshot_path)
        payload["log_path"] = str(log_path)
        payload["run_id"] = run_id
        self.result_ready.emit(payload)
        self.finished.emit()


class BoundedContentViewport(QWidget):
    """Center the UI canvas while allowing the top-level window to be fullscreen."""

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = content
        self.content.setParent(self)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        super().resizeEvent(event)
        width = min(self.width(), DRIVER_REFERENCE_WIDTH)
        height = min(self.height(), DRIVER_REFERENCE_HEIGHT)
        self.content.setGeometry(
            (self.width() - width) // 2,
            (self.height() - height) // 2,
            max(1, width),
            max(1, height),
        )


class OperatorWindow(QMainWindow):
    def __init__(self, model: OperatorDisplayModel, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.settings = settings
        self.camera_widgets: dict[CameraRole, CameraSurface] = {}
        self.driver_preview_camera_widgets: dict[CameraRole, CameraSurface] = {}
        self._runtime_camera_status: dict[str, str] = {}
        self._threads: list[QThread] = []
        self._workers: list[CameraCaptureWorker] = []
        self._detection_threads: list[QThread] = []
        self._detection_workers: list[LiveDetectionWorker] = []
        self._purpose_threads: list[QThread] = []
        self._purpose_workers: list[PurposeInferenceWorker] = []
        self._front_lpr_threads: list[QThread] = []
        self._front_lpr_workers: list[FrontCameraLprWorker] = []
        self._detection_enabled = False
        self._detection_camera_ids: tuple[str, ...] = ()
        self._detection_event_counts: dict[str, int] = {}
        self._detection_load_started_at: float | None = None
        self._detection_first_inference_seconds: float | None = None
        self._detection_model_label = ""
        self._detection_log_path: Path | None = None
        self._detection_unconfirmed_reported = False
        self._detection_failed = False
        self._detection_legacy_mode = False
        self._purpose_task_enabled = False
        self._purpose_task_id = ""
        self._purpose_task_label = ""
        self._purpose_task_log_path: Path | None = None
        self._purpose_task_started_at: float | None = None
        self._purpose_task_first_inference_seconds: float | None = None
        self._purpose_lpr_results: tuple[PlateOcrEvent, ...] = ()
        self._front_lpr_enabled = False
        self._user_mode_state = "idle"
        self._driver_state_override: ParkingState | None = None
        self._driver_alignment_override: AlignmentResult | None = None
        self._driver_simulated = False
        self._driver_masked_plate = ""
        self._operator_notice_until = 0.0
        self._pending_user_purpose_task_id = ""
        self._person_detection_streak = 0
        self._last_person_detection_at: float | None = None
        self._person_detected_camera_ids: set[str] = set()
        self._operator_unlocked = False
        self._vehicle_entry_simulation = False
        self._raw_data_manager: RawDataManager | None = None
        self._evidence_coordinator: EvidenceCoordinator | None = None
        self._camera_layout_mode = "dashboard"
        self._camera_rotations: dict[str, int] = {
            camera.id: camera.rotation_degrees
            for camera in settings.cameras
        } if settings is not None else {}
        self._selected_hailo_model_path: Path | None = None
        self.sidebar_buttons: dict[str, QPushButton] = {}
        self.purpose_task_buttons: dict[str, QPushButton] = {}
        self.camera_rotation_buttons: dict[str, QPushButton] = {}
        self.clock_label = QLabel()
        self.setWindowTitle("TowerSightAI Operator Console")
        self.setStyleSheet(_stylesheet())
        self._build()
        self.apply_model(model)
        if self.settings is not None:
            self._start_camera_capture()
            self._start_raw_data_collection()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def apply_model(self, model: OperatorDisplayModel) -> None:
        self.model = model
        self.state_label.setText(model.state.value)
        self.instruction_label.setText(model.instruction)
        self.warning_label.setText(model.warning)
        self.plc_label.setText(f"PLC {model.plc_state.value}")
        self.camera_summary_label.setText(model.camera_health_summary)
        self.safety_label.setText("OK" if model.can_show_final_ok else model.safety_status.value)
        self.safety_label.setProperty("status", "ready" if model.can_show_final_ok else model.safety_status.value.lower())
        self.safety_label.style().unpolish(self.safety_label)
        self.safety_label.style().polish(self.safety_label)
        for tile in model.camera_tiles:
            self._runtime_camera_status[tile.camera_id] = tile.status_text
            for widget in self._camera_surfaces(tile.role):
                widget.set_status(tile.status_text)
        self._refresh_runtime_health_labels()
        self._refresh_driver_display()

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        for worker in self._workers:
            worker.stop()
        for thread in self._threads:
            thread.quit()
            thread.wait(10000)
        self._stop_ai_detection()
        self._stop_purpose_inference()
        self._stop_front_camera_lpr()
        for thread in self._detection_threads:
            thread.quit()
            thread.wait(5000)
        for thread in self._purpose_threads:
            thread.quit()
            thread.wait(5000)
        for thread in self._front_lpr_threads:
            thread.quit()
            thread.wait(10000)
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.record_application_stopped)
        if self._evidence_coordinator is not None:
            self._evidence_coordinator.close()
            self._evidence_coordinator = None
        if self._raw_data_manager is not None:
            close_raw = getattr(self._raw_data_manager, "close", None)
            if close_raw is not None:
                close_raw()
        super().closeEvent(event)

    def _start_raw_data_collection(self) -> None:
        if self.settings is None or not self.settings.raw_storage.enabled:
            return
        try:
            self._raw_data_manager = RawDataManager(
                self.settings.raw_storage,
                (camera.id for camera in self.settings.active_cameras),
            )
            if self.settings.raw_storage.media_enabled:
                self._evidence_coordinator = EvidenceCoordinator(
                    self.settings.raw_storage,
                    self.settings.active_cameras,
                    artifact_callback=self._raw_data_manager.record_media_artifact,
                    failure_callback=self._raw_data_manager.record_media_failure,
                )
                self._raw_data_manager.set_event_sink(self._evidence_coordinator.handle_raw_event)
                for camera_id, status in self._runtime_camera_status.items():
                    self._evidence_coordinator.update_camera_status(camera_id, status)
            self._raw_data_manager.record_application_started(
                metadata={
                    "app_env": self.settings.app_env,
                    "camera_ids": [camera.id for camera in self.settings.active_cameras],
                    "birdview_mode": self.settings.birdview_mode.value,
                }
            )
            self._raw_data_manager.start_background_sync()
        except Exception:  # noqa: BLE001 - raw logging must not crash the safety UI.
            logging.getLogger(__name__).exception("raw-data collection startup failed")
            if self._evidence_coordinator is not None:
                self._evidence_coordinator.close()
                self._evidence_coordinator = None
            if self._raw_data_manager is not None:
                self._raw_data_manager.close()
            self._raw_data_manager = None
            return
        self._raw_sample_timer = QTimer(self)
        self._raw_sample_timer.timeout.connect(self._sample_raw_person_window)
        self._raw_sample_timer.start(max(1, int(self.settings.raw_storage.sample_interval_seconds * 1000)))
        self._raw_sync_timer = QTimer(self)
        self._raw_sync_timer.timeout.connect(self._start_raw_background_sync)
        self._raw_sync_timer.start(max(1000, int(self.settings.raw_storage.sync_interval_seconds * 1000)))

    def _record_raw(self, callback, *args, **kwargs) -> None:  # noqa: ANN001 - compact failure boundary.
        try:
            callback(*args, **kwargs)
        except Exception:  # noqa: BLE001 - persistence failure must be visible in logs, not crash UI.
            logging.getLogger(__name__).exception("raw-data record failed")

    def _sample_raw_person_window(self) -> None:
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.tick)

    def _start_raw_background_sync(self) -> None:
        if self._raw_data_manager is not None:
            self._raw_data_manager.start_background_sync()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        if event.key() == Qt.Key.Key_O and event.modifiers() == (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            self._unlock_operator()
            return
        super().keyPressEvent(event)

    def _start_camera_capture(self) -> None:
        if self.settings is None:
            return
        for camera in self.settings.active_cameras:
            thread = QThread(self)
            worker = CameraCaptureWorker(self.settings, camera.id, self._camera_rotations.get(camera.id, 0))
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.frame_ready.connect(self._set_camera_frame)
            worker.status_changed.connect(self._set_camera_status)
            worker.finished.connect(thread.quit)
            self._threads.append(thread)
            self._workers.append(worker)
            thread.start()

    def _set_camera_frame(self, camera_id: str, frame: QImage) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        for widget in self._camera_surfaces(tile.role):
            widget.set_frame(frame)
        if self._evidence_coordinator is not None:
            self._evidence_coordinator.update_frame(camera_id, frame, received_at=datetime.now(timezone.utc))

    def _set_camera_status(self, camera_id: str, status: str) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        widgets = self._camera_surfaces(tile.role)
        previous_status = self._runtime_camera_status.get(camera_id)
        for widget in widgets:
            if widget.status != status:
                widget.set_status(status)
        if previous_status == status:
            return
        self._runtime_camera_status[camera_id] = status
        if self._evidence_coordinator is not None:
            self._evidence_coordinator.update_camera_status(camera_id, status)
        self._refresh_runtime_health_labels()
        self._refresh_driver_display()

    def _set_camera_detections(self, camera_id: str, detections: tuple[DetectionEvent, ...]) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        if self._raw_data_manager is not None and detections:
            task_id = self._purpose_task_id or ("legacy_detection" if self._detection_legacy_mode else "general_detection")
            self._record_raw(
                self._raw_data_manager.record_detection_batch,
                camera_id,
                detections,
                task_id=task_id,
            )
        widgets = self._camera_surfaces(tile.role)
        if not widgets:
            return
        if self._runtime_camera_status.get(camera_id, tile.status_text).startswith("NG"):
            for widget in widgets:
                widget.clear_detections()
            return
        self._detection_event_counts[camera_id] = self._detection_event_counts.get(camera_id, 0) + len(detections)
        if self._detection_enabled:
            if self._detection_legacy_mode:
                self.ai_detection_label.setText(_legacy_ai_detection_label(self._detection_camera_ids, self._detection_event_counts))
            else:
                self.ai_detection_label.setText(
                    _ai_detection_label(
                        self._detection_camera_ids,
                        self._detection_event_counts,
                        first_inference_seconds=self._detection_first_inference_seconds,
                    )
                )
        elif self._purpose_task_enabled:
            self.ai_detection_label.setText(
                _purpose_detection_label(
                    self._purpose_task_label,
                    self._detection_camera_ids,
                    self._detection_event_counts,
                    first_inference_seconds=self._purpose_task_first_inference_seconds,
                )
            )
        self._update_user_person_alert(camera_id, detections)
        self._refresh_user_mode_labels()
        for widget in widgets:
            widget.set_detections(detections)

    def _camera_surfaces(self, role: CameraRole) -> tuple[CameraSurface, ...]:
        surfaces = (
            self.camera_widgets.get(role),
            self.driver_preview_camera_widgets.get(role),
        )
        return tuple(surface for surface in surfaces if surface is not None)

    def _all_camera_surfaces(self) -> tuple[CameraSurface, ...]:
        return tuple(self.camera_widgets.values()) + tuple(self.driver_preview_camera_widgets.values())

    def _update_user_person_alert(self, camera_id: str, detections: tuple[DetectionEvent, ...]) -> None:
        if self._purpose_task_id != PURPOSE_PERSON_PRESENCE:
            return
        if not any(event.label.strip().lower() in {"person", "human"} for event in detections):
            return
        self._person_detection_streak += 1
        self._last_person_detection_at = time.monotonic()
        self._person_detected_camera_ids.add(camera_id)
        if self._person_detection_streak >= PERSON_ALERT_STREAK_THRESHOLD:
            cameras = ", ".join(sorted(self._person_detected_camera_ids))
            message = f"사람이 감지되었습니다. ({cameras})"
            self.warning_label.setText(f"{message} 최종 OK는 차단됩니다.")
            self._driver_state_override = ParkingState.HUMAN_DETECTED
            self._driver_simulated = False
            self._refresh_driver_display()

    def _set_detection_status(self, camera_id: str, message: str) -> None:
        if "실행 중" in message or "재시도 중" in message or "재시작 대기" in message:
            if self._detection_legacy_mode:
                self.ai_detection_label.setText(_legacy_ai_detection_label(self._detection_camera_ids, self._detection_event_counts))
            else:
                self.ai_detection_label.setText(
                    _ai_detection_label(
                        self._detection_camera_ids,
                        self._detection_event_counts,
                        first_inference_seconds=self._detection_first_inference_seconds,
                    )
                )
            return
        self.ai_detection_label.setText(f"{'이전 AI Detection' if self._detection_legacy_mode else 'AI Detection'} 오류: {camera_id}")
        self._detection_failed = True
        if hasattr(self, "model_status_label"):
            self.model_status_label.setText(f"추론 실패: {self._detection_model_label or '모델 미확인'}")
        self.warning_label.setText(f"Hailo detection 문제: {message[:120]}")

    def _set_detection_started(self, hef_path: str, log_path: str) -> None:
        self._detection_load_started_at = time.monotonic()
        self._detection_first_inference_seconds = None
        self._detection_unconfirmed_reported = False
        self._detection_failed = False
        self._detection_model_label = Path(hef_path).name
        self._detection_log_path = Path(log_path) if log_path else None
        self.ai_detection_label.setText(_ai_detection_label(self._detection_camera_ids, self._detection_event_counts, loading_seconds=0.0))
        self.model_status_label.setText(f"실행 모델: {self._detection_model_label}")
        self.warning_label.setText(f"AI 모델 로드 중: {self._detection_model_label}")

    def _set_first_inference_ready(self, elapsed_seconds: float) -> None:
        self._detection_first_inference_seconds = elapsed_seconds
        self.ai_detection_label.setText(
            _ai_detection_label(
                self._detection_camera_ids,
                self._detection_event_counts,
                first_inference_seconds=elapsed_seconds,
            )
        )
        self.model_status_label.setText(f"실행 모델: {self._detection_model_label}")
        self.warning_label.setText(f"AI 추론 시작 완료: {self._detection_model_label} ({elapsed_seconds:.1f}s)")

    def _refresh_runtime_health_labels(self) -> None:
        if self._detection_failed:
            return
        preserve_notice = time.monotonic() < self._operator_notice_until
        blocked_tiles = tuple(
            tile
            for tile in self.model.camera_tiles
            if self._runtime_camera_status.get(tile.camera_id, tile.status_text).startswith("NG")
        )
        blocked_count = len(blocked_tiles)
        camera_total = len(self.model.camera_tiles)
        birdview_suffix = " · 버드뷰 OFF" if not self.model.birdview_available else ""
        if blocked_count:
            self.camera_summary_label.setText(f"카메라 {blocked_count}/{camera_total} 차단{birdview_suffix}")
            if self._detection_enabled or preserve_notice:
                return
            warning = "카메라 입력 차단: " + ", ".join(tile.title for tile in blocked_tiles)
            if not self.model.birdview_available:
                warning = f"{warning} · 버드뷰 OFF · 최종 OK 차단"
            self.warning_label.setText(warning)
            return

        self.camera_summary_label.setText(f"카메라 {camera_total}/{camera_total} 정상{birdview_suffix}")
        if self._detection_enabled or preserve_notice:
            return
        if self.model.plc_state.value != "CONNECTED":
            self.warning_label.setText("PLC 상태 미확인: 최종 OK 차단")
        else:
            self.warning_label.setText(self.model.warning)

    def _refresh_driver_display(
        self,
        *,
        apply_layout: bool | None = None,
        force_layout: bool = False,
    ) -> None:
        if not hasattr(self, "driver_view"):
            return
        blocked_roles: set[CameraRole] = set()
        for tile in self.model.camera_tiles:
            status = self._runtime_camera_status.get(tile.camera_id, tile.status_text)
            if not status.startswith("정상 수신"):
                blocked_roles.add(tile.role)
        primary_alert_role = None
        if self._person_detected_camera_ids:
            camera_id = sorted(self._person_detected_camera_ids)[0]
            tile = next((item for item in self.model.camera_tiles if item.camera_id == camera_id), None)
            if tile is not None:
                primary_alert_role = tile.role
        effective_state = self._driver_state_override or self.model.state
        layout_state_override = None
        if self._user_mode_state == "idle" and effective_state is ParkingState.HUMAN_DETECTED:
            layout_state_override = ParkingState.IDLE
        display = build_driver_display(
            self.model,
            state_override=self._driver_state_override,
            layout_state_override=layout_state_override,
            alignment_override=self._driver_alignment_override,
            blocked_roles=blocked_roles,
            primary_alert_role=primary_alert_role,
            masked_plate_text=self._driver_masked_plate,
            simulated=self._driver_simulated,
        )
        if apply_layout is None:
            apply_layout = hasattr(self, "user_view") and self.stack.currentWidget() is self.user_view
        self.driver_view.apply_display(
            display,
            apply_layout=apply_layout,
            force_layout=force_layout,
        )
        if hasattr(self, "driver_preview"):
            self.driver_preview.apply_display(display, apply_layout=True)

    def _build(self) -> None:
        self.stack = QStackedWidget()
        self.operator_view = self._build_operator_view()
        self.user_view = self._build_user_view()
        self.settings_view = self._build_settings_view()
        self.stack.addWidget(self.operator_view)
        self.stack.addWidget(self.user_view)
        self.stack.addWidget(self.settings_view)
        self.content_viewport = BoundedContentViewport(self.stack)
        self.setCentralWidget(self.content_viewport)
        self._show_user_mode()

    def _build_user_view(self) -> QWidget:
        self.driver_view = DriverView(self.camera_widgets)
        self.driver_view.operator_requested.connect(self._unlock_operator)
        self.user_grid = self.driver_view.camera_grid
        self.user_instruction_label = self.driver_view.headline_label
        self.user_warning_label = self.driver_view.blocking_label
        self.user_plate_label = QLabel("번호판: -")
        self.user_progress_label = self.driver_view.status_label
        return self.driver_view

    def _build_operator_view(self) -> QWidget:
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 10, 10)
        outer.setSpacing(10)

        self.operator_sidebar = QWidget()
        self.operator_sidebar.setObjectName("sidePanel")
        self.operator_sidebar.setFixedWidth(OPERATOR_SIDEBAR_WIDTH)
        self.operator_sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        sidebar_layout = QVBoxLayout(self.operator_sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(8)
        title = QLabel("운영 메뉴")
        title.setObjectName("testTitleLabel")
        sidebar_layout.addWidget(title)
        self._add_sidebar_buttons(sidebar_layout)
        self.driver_test_toggle = QPushButton("사용자 화면 테스트")
        self.driver_test_toggle.setCheckable(True)
        self.driver_test_toggle.clicked.connect(self._toggle_driver_test_panel)
        sidebar_layout.addWidget(self.driver_test_toggle)
        self.driver_test_panel = QFrame()
        self.driver_test_panel.setObjectName("driverTestPanel")
        driver_test_layout = QGridLayout(self.driver_test_panel)
        driver_test_layout.setContentsMargins(6, 6, 6, 6)
        driver_test_layout.setSpacing(5)
        self.driver_test_buttons: dict[str, QPushButton] = {}
        for index, (label, handler) in enumerate(
            (
                ("실제 상태", self._clear_user_test_state),
                ("IDLE", self._user_idle),
                ("진입", self._user_entry),
                ("진입완료", self._user_entry_complete),
                ("번호판인식", self._user_plate_recognition),
                ("주차시작", self._user_parking_started),
            )
        ):
            button = QPushButton(label)
            button.setObjectName("smallModeButton")
            button.clicked.connect(handler)
            self.driver_test_buttons[label] = button
            driver_test_layout.addWidget(button, index // 2, index % 2)
        self.driver_test_panel.setVisible(False)
        sidebar_layout.addWidget(self.driver_test_panel)
        sidebar_layout.addStretch(1)
        self.operator_sidebar.setVisible(False)
        outer.addWidget(self.operator_sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(10, 10, 0, 0)
        main_layout.setSpacing(10)
        outer.addWidget(main, 1)

        header = QHBoxLayout()
        header.setSpacing(10)
        main_layout.addLayout(header)

        self.sidebar_toggle_button = QPushButton("메뉴")
        self.sidebar_toggle_button.setObjectName("menuButton")
        self.sidebar_toggle_button.setFixedWidth(88)
        self.sidebar_toggle_button.clicked.connect(self._toggle_sidebar)
        header.addWidget(self.sidebar_toggle_button)

        self.safety_label = QLabel("NG")
        self.safety_label.setObjectName("safetyLabel")
        self.safety_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.safety_label.setFixedWidth(150)
        header.addWidget(self.safety_label)

        header_text = QVBoxLayout()
        header_text.setSpacing(6)
        header.addLayout(header_text, 1)

        self.instruction_label = QLabel()
        self.instruction_label.setObjectName("instructionLabel")
        self.instruction_label.setWordWrap(True)
        header_text.addWidget(self.instruction_label)

        self.warning_label = QLabel()
        self.warning_label.setObjectName("warningLabel")
        self.warning_label.setWordWrap(True)
        header_text.addWidget(self.warning_label)

        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        self.operator_camera_area = QWidget()
        self.operator_camera_area.setLayout(self.grid)

        roles = [tile.role for tile in self.model.camera_tiles]
        for role in roles:
            title = next((tile.title for tile in self.model.camera_tiles if tile.role is role), role.value)
            surface = CameraSurface(title)
            self.camera_widgets[role] = surface
            self.driver_preview_camera_widgets[role] = CameraSurface(title)

        self.driver_preview = DriverView(
            self.driver_preview_camera_widgets,
            preview_mode=True,
        )
        self.driver_preview_host = DriverPreviewHost(self.driver_preview)
        self.operator_workspace_stack = QStackedWidget()
        self.operator_workspace_stack.addWidget(self.operator_camera_area)
        self.operator_workspace_stack.addWidget(self.driver_preview_host)
        self.operator_workspace_stack.setCurrentWidget(self.operator_camera_area)
        main_layout.addWidget(self.operator_workspace_stack, 1)

        status_bar = QWidget()
        status_bar.setObjectName("statusStrip")
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(8, 4, 8, 4)
        status_layout.setSpacing(8)
        self.state_label = QLabel()
        self.plc_label = QLabel()
        self.camera_summary_label = QLabel()
        self.model_status_label = QLabel("모델 선택: 없음")
        self.ai_detection_label = QLabel("AI 추론 OFF")
        self.evidence_status_label = QLabel("증거 OFF")
        for label in (self.state_label, self.plc_label, self.camera_summary_label, self.model_status_label, self.ai_detection_label, self.evidence_status_label, self.clock_label):
            label.setObjectName("telemetryLabel")
            # Runtime inference text can become very long. Ignore its natural
            # width so the hidden operator page cannot enlarge the shared
            # stacked window after returning to the driver display.
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            status_layout.addWidget(label)
        main_layout.addWidget(status_bar)

        self._set_camera_layout("dashboard")
        return root

    def _add_sidebar_buttons(self, layout: QVBoxLayout) -> None:
        empty_count = 0
        for label in SIDEBAR_ACTION_LABELS:
            button = QPushButton(label)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if label == "사용자모드":
                button.clicked.connect(self._show_user_mode)
            elif label == "전체 카메라":
                button.clicked.connect(self._show_all_cameras)
            elif label == "카메라 설정":
                button.clicked.connect(self._show_camera_settings)
            elif label == "이전 AI Detection":
                self.legacy_ai_detection_button = button
                button.setCheckable(True)
                button.clicked.connect(self._toggle_legacy_ai_detection)
            elif label == "차량 전용 검출":
                button.setCheckable(True)
                self.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION] = button
                button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_VEHICLE_DETECTION))
            elif label == "번호판 이미지 LPR":
                button.setCheckable(True)
                self.purpose_task_buttons[PURPOSE_LPR_IMAGE] = button
                button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_LPR_IMAGE))
            elif label == "정면카메라LPR":
                self.front_lpr_button = button
                button.setCheckable(True)
                button.clicked.connect(self._toggle_front_camera_lpr)
            elif label == "사람 존재 감지":
                button.setCheckable(True)
                self.purpose_task_buttons[PURPOSE_PERSON_PRESENCE] = button
                button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_PERSON_PRESENCE))
            elif label == "차량 진입 시뮬레이션":
                button.clicked.connect(self._simulate_vehicle_entry)
            else:
                empty_count += 1
                button.clicked.connect(lambda _checked=False, name=f"EMPTY {empty_count}": self._empty_action(name))
            key = f"EMPTY_{empty_count}" if label == "EMPTY" else label
            self.sidebar_buttons[key] = button
            layout.addWidget(button)

    def _build_settings_view(self) -> QWidget:
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        panel = QWidget()
        panel.setObjectName("sidePanel")
        panel.setFixedWidth(380)
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(8)
        title = QLabel("카메라 설정")
        title.setObjectName("testTitleLabel")
        panel_layout.addWidget(title)

        for tile in self.model.camera_tiles:
            button = QPushButton()
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, camera_id=tile.camera_id: self._rotate_camera(camera_id))
            self.camera_rotation_buttons[tile.camera_id] = button
            panel_layout.addWidget(button)

        panel_layout.addStretch(1)
        back = QPushButton("운영자 화면")
        back.clicked.connect(self._show_operator)
        panel_layout.addWidget(back)
        outer.addWidget(panel)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.rotation_summary_label = QLabel()
        self.rotation_summary_label.setObjectName("instructionLabel")
        self.rotation_summary_label.setWordWrap(True)
        detail_layout.addWidget(self.rotation_summary_label)
        rotation_note = QLabel("회전값은 카메라 preview 파이프라인과 AI 추론 파이프라인에 동일하게 적용됩니다.")
        rotation_note.setObjectName("warningLabel")
        rotation_note.setWordWrap(True)
        detail_layout.addWidget(rotation_note)
        detail_layout.addStretch(1)
        outer.addWidget(detail, 1)
        self._refresh_rotation_controls()
        return root

    def _tick(self) -> None:
        text = datetime.now().strftime("%m-%d %H:%M:%S")
        self.clock_label.setText(text)
        if self._evidence_coordinator is not None:
            self.evidence_status_label.setText(self._evidence_coordinator.status_summary)
        if self._last_person_detection_at is not None and time.monotonic() - self._last_person_detection_at >= PERSON_ALERT_STALE_SECONDS:
            self._reset_person_alert()
            if self._driver_state_override is ParkingState.HUMAN_DETECTED and not self._driver_simulated:
                self._driver_state_override = None
                self._refresh_driver_display()
        self._refresh_user_mode_labels()
        if (
            self._detection_enabled
            and not self._detection_legacy_mode
            and self._detection_load_started_at is not None
            and self._detection_first_inference_seconds is None
        ):
            elapsed = time.monotonic() - self._detection_load_started_at
            self.ai_detection_label.setText(
                _ai_detection_label(self._detection_camera_ids, self._detection_event_counts, loading_seconds=elapsed)
            )
            if elapsed >= FIRST_INFERENCE_TIMEOUT_SECONDS and not self._detection_unconfirmed_reported:
                self._detection_unconfirmed_reported = True
                self._detection_failed = True
                self.model_status_label.setText(f"추론 미확인: {self._detection_model_label}")
                self.warning_label.setText(
                    f"AI 추론 실패: {self._detection_model_label} 첫 detection 이벤트가 {FIRST_INFERENCE_TIMEOUT_SECONDS:.0f}s 내 확인되지 않았습니다."
                )
        if self._purpose_task_enabled and self._purpose_task_started_at is not None and self._purpose_task_first_inference_seconds is None:
            elapsed = time.monotonic() - self._purpose_task_started_at
            self.ai_detection_label.setText(
                _purpose_detection_label(self._purpose_task_label, self._detection_camera_ids, self._detection_event_counts, loading_seconds=elapsed)
            )

    def _show_operator(self) -> None:
        if not self._operator_unlocked:
            return
        self._activate_operator_layout(self._camera_layout_mode)

    def _show_user_mode(self) -> None:
        self._operator_unlocked = False
        if hasattr(self, "driver_view"):
            self.driver_view.operator_hotspot.cancel_hold()
        self.setUpdatesEnabled(False)
        try:
            self._set_user_camera_layout()
            self.stack.setCurrentWidget(self.user_view)
            self._refresh_driver_display(apply_layout=True, force_layout=True)
            self.driver_view.restore_presentation()
        finally:
            self.setUpdatesEnabled(True)
        self.update()
        self._refresh_user_mode_labels()

    def _show_operator_dashboard(self) -> None:
        self._operator_unlocked = True
        self._set_driver_test_preview(False)
        self._activate_operator_layout("dashboard")

    def _show_all_cameras(self) -> None:
        self._operator_unlocked = True
        self._set_driver_test_preview(False)
        self._activate_operator_layout("all")

    def _activate_operator_layout(self, mode: str) -> None:
        self.setUpdatesEnabled(False)
        try:
            self.stack.setCurrentWidget(self.operator_view)
            self._set_camera_layout(mode)
            self.grid.invalidate()
            self.grid.activate()
        finally:
            self.setUpdatesEnabled(True)
        self.update()

    def _show_camera_settings(self) -> None:
        if not self._operator_unlocked:
            return
        self._refresh_rotation_controls()
        self.stack.setCurrentWidget(self.settings_view)

    def _unlock_operator(self) -> None:
        self._operator_unlocked = True
        self._show_operator_dashboard()

    def _toggle_sidebar(self) -> None:
        self.operator_sidebar.setVisible(not self.operator_sidebar.isVisible())

    def _toggle_driver_test_panel(self, checked: bool = False) -> None:
        self._set_driver_test_preview(checked)
        if checked:
            self._refresh_driver_display(apply_layout=False)
            self.driver_preview.restore_presentation()

    def _set_driver_test_preview(self, enabled: bool) -> None:
        self.driver_test_toggle.setChecked(enabled)
        self.driver_test_panel.setVisible(enabled)
        self.operator_workspace_stack.setCurrentWidget(
            self.driver_preview_host if enabled else self.operator_camera_area
        )

    def _clear_user_test_state(self) -> None:
        self._driver_state_override = None
        self._driver_alignment_override = None
        self._driver_simulated = False
        self._driver_masked_plate = ""
        self._user_mode_state = "idle"
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.end_vehicle_session, reason="test_state_cleared")
        self._refresh_driver_display()
        self.warning_label.setText("실제 표시 모델로 복귀했습니다. 안전 조건 확인 전 최종 OK는 차단됩니다.")
        self._refresh_user_mode_labels()

    def _empty_action(self, name: str) -> None:
        self._operator_notice_until = time.monotonic() + 3.0
        self.warning_label.setText(f"{name}: 아직 기능이 연결되지 않았습니다. 최종 OK 차단 상태를 유지합니다.")

    def _simulate_vehicle_entry(self) -> None:
        self._operator_notice_until = time.monotonic() + 3.0
        self._vehicle_entry_simulation = True
        self._driver_state_override = ParkingState.VEHICLE_ENTERING
        self._driver_alignment_override = AlignmentResult.UNKNOWN
        self._driver_simulated = True
        self._user_mode_state = "entry"
        if self._raw_data_manager is not None:
            self._record_raw(
                self._raw_data_manager.record_vehicle_entry,
                camera_id="front",
                simulated=True,
            )
        simulation_roles = (CameraRole.front,)
        if self.model.birdview_available:
            simulation_roles = (CameraRole.ceiling, CameraRole.front)
        for role in simulation_roles:
            for widget in self._camera_surfaces(role):
                widget.set_vehicle_simulation(True)
        self._show_operator_dashboard()
        if self.model.birdview_available:
            self.instruction_label.setText("진입 차량 감지: 버드뷰와 정면 영상을 확인 중입니다.")
        else:
            self.instruction_label.setText("진입 차량 감지: 정면 영상을 확인 중입니다.")
        self.warning_label.setText("차량 진입 시뮬레이션: UI 확인 전용이며 PLC OK는 차단됩니다.")
        self._refresh_driver_display(apply_layout=False)
        self._refresh_user_mode_labels()

    def _user_idle(self) -> None:
        self._user_mode_state = "idle"
        self._driver_state_override = ParkingState.IDLE
        self._driver_alignment_override = None
        self._driver_simulated = True
        self._reset_person_alert()
        self._set_user_status("대기 중: 주차기 내부 사람 감지 중", "사람 감지 AI 실행 준비 중입니다.")
        self._switch_user_purpose_task(PURPOSE_PERSON_PRESENCE)

    def _user_entry(self) -> None:
        self._user_mode_state = "entry"
        self._driver_state_override = ParkingState.VEHICLE_ENTERING
        self._driver_alignment_override = AlignmentResult.UNKNOWN
        self._driver_simulated = True
        if self._raw_data_manager is not None:
            self._record_raw(
                self._raw_data_manager.record_vehicle_entry,
                camera_id="front",
                simulated=True,
            )
        self._reset_person_alert()
        self._set_user_status("진입 차량 감지 중", "정면 카메라 차량 감지 AI로 전환합니다.")
        self._switch_user_purpose_task(PURPOSE_VEHICLE_DETECTION)

    def _user_entry_complete(self) -> None:
        self._user_mode_state = "entry_complete"
        self._driver_state_override = ParkingState.SAFETY_CHECK
        self._driver_alignment_override = None
        self._driver_simulated = True
        self._reset_person_alert()
        self._set_user_status("진입 완료: 사람 감지 중", "차량 감지 AI를 종료하고 사람 감지를 다시 시작합니다.")
        self._switch_user_purpose_task(PURPOSE_PERSON_PRESENCE)

    def _user_plate_recognition(self) -> None:
        self._user_mode_state = "plate_recognition"
        self._driver_state_override = ParkingState.PLATE_RECOGNITION
        self._driver_alignment_override = None
        self._driver_simulated = True
        self._set_user_status("번호판 인식 중", "정면 카메라 스냅샷으로 번호판을 인식합니다.")
        self._start_front_camera_lpr()

    def _user_parking_started(self) -> None:
        self._user_mode_state = "parking_started"
        self._driver_state_override = ParkingState.AI_STOP
        self._driver_alignment_override = None
        self._driver_simulated = True
        self._pending_user_purpose_task_id = ""
        self._stop_purpose_inference()
        self._stop_ai_detection()
        self._stop_front_camera_lpr()
        self._reset_person_alert()
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.end_vehicle_session, reason="parking_started")
        self._set_user_status("주차 시작: AI 감시 종료", "모든 AI 추론을 종료했습니다. 최종 OK는 차단됩니다.")

    def _switch_user_purpose_task(self, task_id: str) -> None:
        if task_id == PURPOSE_PERSON_PRESENCE and not self._user_person_detection_can_start():
            if self._purpose_task_enabled or self._purpose_workers:
                self._stop_purpose_inference()
            self._pending_user_purpose_task_id = ""
            self._set_purpose_buttons_checked(False)
            self._set_user_status(
                self.user_instruction_label.text(),
                "사람 감지는 프론트 카메라가 정상 수신일 때 시작합니다.",
            )
            return
        if self._purpose_task_enabled and self._purpose_task_id == task_id:
            self._refresh_user_mode_labels()
            return
        if self._purpose_workers:
            self._pending_user_purpose_task_id = task_id
            self._stop_purpose_inference()
            self._set_user_status(self.user_instruction_label.text(), "기존 AI 추론 종료 후 다음 추론을 시작합니다.")
            return
        self._pending_user_purpose_task_id = ""
        self._start_purpose_inference(task_id)

    def _set_user_status(self, instruction: str, warning: str) -> None:
        self.instruction_label.setText(instruction)
        self.warning_label.setText(f"{warning} 최종 OK는 차단됩니다.")
        self._refresh_driver_display()
        self._refresh_user_mode_labels()

    def _user_person_detection_can_start(self) -> bool:
        if self.settings is None:
            return False
        front = next((camera for camera in self.settings.active_cameras if camera.role is CameraRole.front), None)
        return front is not None and self._runtime_camera_status.get(front.id) == "정상 수신"

    def _refresh_user_mode_labels(self) -> None:
        if not hasattr(self, "driver_test_buttons"):
            return
        for label, button in self.driver_test_buttons.items():
            active = (
                (label == "IDLE" and self._user_mode_state == "idle")
                or (label == "진입" and self._user_mode_state == "entry")
                or (label == "진입완료" and self._user_mode_state == "entry_complete")
                or (label == "번호판인식" and self._user_mode_state == "plate_recognition")
                or (label == "주차시작" and self._user_mode_state == "parking_started")
                or (label == "실제 상태" and not self._driver_simulated and self._driver_state_override is None)
            )
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _user_progress_text(self) -> str:
        task = self._purpose_task_label or "AI OFF"
        if self._front_lpr_enabled:
            task = "정면카메라LPR 실행 중"
        return f"{self._user_mode_state.upper()} / {task} / {self.ai_detection_label.text() if hasattr(self, 'ai_detection_label') else ''}"

    def _set_user_camera_layout(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
        self.driver_view.detach_cameras()

    def _reset_person_alert(self) -> None:
        self._person_detection_streak = 0
        self._last_person_detection_at = None
        self._person_detected_camera_ids.clear()

    def _rotate_camera(self, camera_id: str) -> None:
        next_rotation = _next_rotation(self._camera_rotations.get(camera_id, 0))
        self._camera_rotations[camera_id] = next_rotation
        self._refresh_rotation_controls()
        self._restart_camera_capture()
        if self._detection_enabled:
            self._stop_ai_detection()
            self.ai_detection_label.setText("AI 추론 OFF: 회전 변경")
        if self._purpose_task_enabled:
            self._stop_purpose_inference()
            self.ai_detection_label.setText("목적 추론 OFF: 회전 변경")
        self.warning_label.setText(
            f"{camera_id} 회전 {_rotation_label(next_rotation)} 적용. AI 추론은 다음 시작부터 같은 회전 스트림을 사용합니다."
        )

    def _refresh_rotation_controls(self) -> None:
        summaries: list[str] = []
        for tile in self.model.camera_tiles:
            rotation = self._camera_rotations.get(tile.camera_id, 0)
            text = f"{tile.title}: {_rotation_label(rotation)}"
            summaries.append(text)
            button = self.camera_rotation_buttons.get(tile.camera_id)
            if button is not None:
                button.setText(f"{text} 회전")
        if hasattr(self, "rotation_summary_label"):
            self.rotation_summary_label.setText(" / ".join(summaries))

    def _restart_camera_capture(self) -> None:
        if self.settings is None:
            return
        for worker in tuple(self._workers):
            worker.stop()
        for thread in tuple(self._threads):
            thread.quit()
            thread.wait(5000)
        self._threads.clear()
        self._workers.clear()
        self._start_camera_capture()

    def _set_camera_layout(self, mode: str) -> None:
        self._camera_layout_mode = mode
        for widget in self.camera_widgets.values():
            widget.set_display_mode("contain")
        if hasattr(self, "driver_view"):
            self.driver_view.detach_cameras()
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
        if not self.model.birdview_available and mode == "all":
            layout = (
                (CameraRole.front, 0, 0, 2, 1),
                (CameraRole.rear_side, 0, 1, 1, 1),
                (CameraRole.opposite_side, 1, 1, 1, 1),
            )
        elif not self.model.birdview_available:
            layout = ((CameraRole.front, 0, 0, 1, 1),)
        elif mode == "all":
            layout = (
                (CameraRole.ceiling, 0, 0, 1, 1),
                (CameraRole.front, 0, 1, 1, 1),
                (CameraRole.rear_side, 1, 0, 1, 1),
                (CameraRole.opposite_side, 1, 1, 1, 1),
            )
        else:
            layout = (
                (CameraRole.ceiling, 0, 0, 1, 1),
                (CameraRole.front, 0, 1, 1, 1),
            )
        for role, row, col, row_span, col_span in layout:
            widget = self.camera_widgets[role]
            if mode == "dashboard" and role is CameraRole.ceiling:
                widget.setMinimumSize(320, 520)
                widget.setMaximumWidth(520)
            else:
                widget.setMinimumSize(360, 220)
                widget.setMaximumWidth(16777215)
            self.grid.addWidget(widget, row, col, row_span, col_span)
            widget.show()
        if not self.model.birdview_available:
            self.grid.setColumnStretch(0, 3)
            self.grid.setColumnStretch(1, 2 if mode == "all" else 0)
        else:
            self.grid.setColumnStretch(0, 2 if mode == "dashboard" else 1)
            self.grid.setColumnStretch(1, 5 if mode == "dashboard" else 1)
        for row in range(2):
            self.grid.setRowStretch(row, 1 if mode == "all" or row == 0 else 0)

    def _toggle_ai_detection(self, checked: bool = False) -> None:
        del checked
        if self._detection_enabled:
            self._stop_ai_detection()
            return
        self._start_ai_detection(legacy_mode=False)

    def _toggle_legacy_ai_detection(self, checked: bool = False) -> None:
        del checked
        if self._detection_enabled:
            self._stop_ai_detection()
            return
        self._start_ai_detection(legacy_mode=True)

    def _start_ai_detection(self, *, legacy_mode: bool = False) -> None:
        if self.settings is None:
            self._set_detection_buttons_checked(False)
            self.warning_label.setText("설정이 없어 AI 추론을 시작할 수 없습니다.")
            return
        if self._purpose_task_enabled:
            self._stop_purpose_inference()
        if self._detection_workers:
            self._set_detection_buttons_checked(False)
            self.warning_label.setText("AI 추론 종료 처리 중입니다. 잠시 후 다시 시도해 주세요.")
            return
        streaming_camera_ids = _streaming_camera_ids(self.settings, self._runtime_camera_status)
        if not streaming_camera_ids:
            self._set_detection_buttons_checked(False)
            self._set_detection_button_texts()
            self.ai_detection_label.setText("AI 추론 OFF")
            self.warning_label.setText("정상 스트리밍 중인 카메라가 없어 AI 추론을 시작하지 않습니다.")
            return
        self._detection_enabled = True
        self._detection_legacy_mode = legacy_mode
        self._detection_camera_ids = streaming_camera_ids
        if self._raw_data_manager is not None:
            self._record_raw(
                self._raw_data_manager.record_ai_started,
                "legacy_detection" if legacy_mode else "general_detection",
                streaming_camera_ids,
                simulated=self._driver_simulated,
            )
        self._detection_event_counts = {camera_id: 0 for camera_id in streaming_camera_ids}
        self._detection_load_started_at = time.monotonic()
        self._detection_first_inference_seconds = None
        self._detection_unconfirmed_reported = False
        self._detection_failed = False
        selected_hef = Path(self.settings.hailo_hef_path) if legacy_mode else Path(self._selected_hailo_model_path or self.settings.hailo_hef_path)
        self._detection_model_label = selected_hef.name
        self._detection_log_path = None
        if legacy_mode:
            self.model_status_label.setText(f"이전 방식: {self._detection_model_label}")
            self.ai_detection_label.setText(_legacy_ai_detection_label(streaming_camera_ids, self._detection_event_counts))
            self.warning_label.setText("이전 방식 AI Detection 실행 중: " + ", ".join(streaming_camera_ids))
        else:
            self.model_status_label.setText(f"모델 로드 중: {self._detection_model_label}")
            self.ai_detection_label.setText(_ai_detection_label(streaming_camera_ids, self._detection_event_counts, loading_seconds=0.0))
            self.warning_label.setText(f"AI 모델 로드 중: {self._detection_model_label}")
        self._set_detection_buttons_checked(True, legacy_mode=legacy_mode)
        self._set_detection_button_texts(legacy_mode=legacy_mode)
        thread = QThread(self)
        worker = LiveDetectionWorker(
            self.settings,
            streaming_camera_ids,
            camera_rotations={camera_id: self._camera_rotations.get(camera_id, 0) for camera_id in streaming_camera_ids},
            hef_path=None if legacy_mode else self._selected_hailo_model_path,
            legacy_mode=legacy_mode,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.detections_ready.connect(self._set_camera_detections)
        worker.status_changed.connect(self._set_detection_status)
        if not legacy_mode:
            worker.detection_started.connect(self._set_detection_started)
            worker.first_inference_ready.connect(self._set_first_inference_ready)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_detection_worker(thread, worker))
        self._detection_threads.append(thread)
        self._detection_workers.append(worker)
        thread.start()

    def _stop_ai_detection(self) -> None:
        raw_task_id = "legacy_detection" if self._detection_legacy_mode else "general_detection"
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.record_ai_stopped, raw_task_id)
        self._detection_enabled = False
        self._detection_legacy_mode = False
        self._detection_camera_ids = ()
        self._detection_event_counts = {}
        self._detection_load_started_at = None
        self._detection_first_inference_seconds = None
        self._detection_model_label = ""
        self._detection_log_path = None
        self._detection_unconfirmed_reported = False
        self._detection_failed = False
        self._set_detection_buttons_checked(False)
        self._set_detection_button_texts()
        if hasattr(self, "ai_detection_label"):
            self.ai_detection_label.setText("AI 추론 OFF")
        if hasattr(self, "model_status_label"):
            selected_text = self._selected_hailo_model_path.name if self._selected_hailo_model_path is not None else "없음"
            self.model_status_label.setText(f"모델 선택: {selected_text}")
        for worker in tuple(self._detection_workers):
            worker.stop()
        for widget in self._all_camera_surfaces():
            widget.clear_detections()

    def _toggle_purpose_inference(self, task_id: str) -> None:
        if self._purpose_task_enabled and self._purpose_task_id == task_id:
            self._stop_purpose_inference()
            return
        self._start_purpose_inference(task_id)

    def _toggle_front_camera_lpr(self, checked: bool = False) -> None:
        del checked
        if self._front_lpr_enabled or self._front_lpr_workers:
            self._set_front_lpr_button_text()
            self.warning_label.setText("정면카메라LPR 실행 중입니다. 완료 후 다시 시도해 주세요.")
            return
        self._start_front_camera_lpr()

    def _start_front_camera_lpr(self) -> None:
        front_widget = self.camera_widgets.get(CameraRole.front)
        frame = front_widget.current_frame() if front_widget is not None else None
        if frame is None and front_widget is not None and front_widget.status == "정상 수신":
            frame = front_widget.grab().toImage()
        if frame is None:
            self._front_lpr_enabled = False
            self._set_front_lpr_button_text()
            self.instruction_label.setText("정면카메라LPR 실패: 정면 프레임 없음")
            self.warning_label.setText("정면카메라LPR을 실행할 최신 정면 카메라 프레임이 없습니다. 최종 OK는 차단됩니다.")
            self._refresh_driver_display()
            return

        self._front_lpr_enabled = True
        self._set_front_lpr_button_text()
        self.warning_label.setText("정면카메라LPR 실행 중입니다. 기존 AI 추론은 유지됩니다.")
        thread = QThread(self)
        worker = FrontCameraLprWorker(frame)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status_changed.connect(self._set_front_lpr_status)
        worker.result_ready.connect(self._set_front_lpr_result)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_front_lpr_worker(thread, worker))
        self._front_lpr_threads.append(thread)
        self._front_lpr_workers.append(worker)
        thread.start()

    def _set_front_lpr_status(self, message: str) -> None:
        self.warning_label.setText(f"{message}. 최종 OK는 차단됩니다.")

    def _set_front_lpr_result(self, payload: dict[str, object]) -> None:
        if payload.get("ok"):
            last4 = str(payload.get("last4") or "")
            plate_number = str(payload.get("plate_number") or "")
            if self._raw_data_manager is not None and plate_number:
                self._record_raw(
                    self._raw_data_manager.record_plate,
                    plate_number,
                    confidence=float(payload["confidence"]) if isinstance(payload.get("confidence"), (float, int)) else None,
                    simulated=self._driver_simulated,
                    source_image_path=str(payload.get("snapshot_path") or "") or None,
                    plate_bbox=payload.get("plate_bbox") if isinstance(payload.get("plate_bbox"), dict) else None,
                )
            self.instruction_label.setText(f"정면카메라LPR: {last4}")
            self.warning_label.setText(f"정면카메라LPR 완료. 로그: {payload.get('log_path')}. 최종 OK는 차단됩니다.")
            self._driver_masked_plate = f"•••• {last4}" if last4 else ""
            self.user_plate_label.setText(f"번호판: {last4}")
            self._refresh_driver_display()
            return
        message = str(payload.get("message") or "정면카메라LPR 실패: 결과 없음")
        self.instruction_label.setText(message)
        self.warning_label.setText(f"{message}. 로그: {payload.get('log_path')}. 최종 OK는 차단됩니다.")
        self._refresh_driver_display()

    def _stop_front_camera_lpr(self) -> None:
        self._front_lpr_enabled = False
        self._set_front_lpr_button_text()
        for worker in tuple(self._front_lpr_workers):
            worker.stop()

    def _cleanup_front_lpr_worker(self, thread: QThread, worker: FrontCameraLprWorker) -> None:
        if thread in self._front_lpr_threads:
            self._front_lpr_threads.remove(thread)
        if worker in self._front_lpr_workers:
            self._front_lpr_workers.remove(worker)
        if not self._front_lpr_workers:
            self._front_lpr_enabled = False
            self._set_front_lpr_button_text()

    def _start_purpose_inference(self, task_id: str) -> None:
        if self.settings is None:
            self._set_purpose_buttons_checked(False)
            self.warning_label.setText("설정이 없어 목적별 AI 추론을 시작할 수 없습니다.")
            return
        if self._purpose_workers:
            self._set_purpose_buttons_checked(False)
            self.warning_label.setText("목적별 AI 추론 종료 처리 중입니다. 잠시 후 다시 시도해 주세요.")
            return
        if self._detection_enabled:
            self._stop_ai_detection()
        spec = PURPOSE_TASK_SPECS[task_id]
        camera_ids = self._purpose_camera_ids(task_id)
        if task_id != PURPOSE_LPR_IMAGE and not camera_ids:
            self._set_purpose_buttons_checked(False)
            self.warning_label.setText(f"{spec.label}: 정상 스트리밍 중인 대상 카메라가 없습니다.")
            return
        self._purpose_task_enabled = True
        self._purpose_task_id = task_id
        self._purpose_task_label = spec.label
        self._purpose_task_log_path = None
        self._purpose_task_started_at = time.monotonic()
        self._purpose_task_first_inference_seconds = None
        self._purpose_lpr_results = ()
        self._detection_enabled = False
        self._detection_failed = False
        self._detection_camera_ids = camera_ids
        if self._raw_data_manager is not None:
            self._record_raw(
                self._raw_data_manager.record_ai_started,
                task_id,
                camera_ids,
                simulated=self._driver_simulated,
            )
        self._detection_event_counts = {camera_id: 0 for camera_id in camera_ids}
        self.model_status_label.setText(f"목적 모델 로드 중: {spec.label}")
        self.ai_detection_label.setText(_purpose_detection_label(spec.label, camera_ids, self._detection_event_counts, loading_seconds=0.0))
        self.warning_label.setText(f"{spec.label} 실행 준비 중: 최종 OK는 차단됩니다.")
        if hasattr(self, "user_warning_label"):
            self.user_warning_label.setText(f"{spec.label} 실행 준비 중입니다. 최종 OK는 차단됩니다.")
            self._refresh_user_mode_labels()
        self._set_detection_buttons_checked(False)
        self._set_detection_button_texts()
        self._set_purpose_buttons_checked(True, task_id=task_id)
        self._set_purpose_button_texts()

        thread = QThread(self)
        worker = PurposeInferenceWorker(
            task_id,
            self.settings,
            camera_ids,
            camera_rotations={camera_id: self._camera_rotations.get(camera_id, 0) for camera_id in camera_ids},
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.detections_ready.connect(self._set_camera_detections)
        worker.lpr_results_ready.connect(self._set_lpr_results)
        worker.status_changed.connect(self._set_purpose_status)
        worker.task_started.connect(self._set_purpose_started)
        worker.first_inference_ready.connect(self._set_purpose_first_inference_ready)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_purpose_worker(thread, worker))
        self._purpose_threads.append(thread)
        self._purpose_workers.append(worker)
        thread.start()

    def _purpose_camera_ids(self, task_id: str) -> tuple[str, ...]:
        if self.settings is None:
            return ()
        if task_id == PURPOSE_LPR_IMAGE:
            return ()
        streaming = _streaming_camera_ids(self.settings, self._runtime_camera_status)
        if task_id == PURPOSE_VEHICLE_DETECTION:
            for camera in self.settings.cameras:
                if camera.role is CameraRole.front and camera.id in streaming:
                    return (camera.id,)
            return ()
        if task_id == PURPOSE_PERSON_PRESENCE:
            return streaming
        return ()

    def _set_purpose_started(self, task_id: str, label: str, log_path: str) -> None:
        self._purpose_task_id = task_id
        self._purpose_task_label = label
        self._purpose_task_log_path = Path(log_path) if log_path else None
        self._purpose_task_started_at = time.monotonic()
        self._purpose_task_first_inference_seconds = None
        self._purpose_lpr_results = ()
        self.ai_detection_label.setText(_purpose_detection_label(label, self._detection_camera_ids, self._detection_event_counts, loading_seconds=0.0))
        self.model_status_label.setText(f"목적 모델 실행: {label}")
        self.warning_label.setText(f"{label} 추론 시작 중. 로그: {self._purpose_task_log_path}")
        if hasattr(self, "user_warning_label"):
            self.user_warning_label.setText(f"{label} 추론 시작 중입니다. 최종 OK는 차단됩니다.")
            self._refresh_user_mode_labels()

    def _set_purpose_first_inference_ready(self, elapsed_seconds: float) -> None:
        self._purpose_task_first_inference_seconds = elapsed_seconds
        self.ai_detection_label.setText(
            _purpose_detection_label(
                self._purpose_task_label,
                self._detection_camera_ids,
                self._detection_event_counts,
                first_inference_seconds=elapsed_seconds,
            )
        )
        self.model_status_label.setText(f"목적 모델 실행: {self._purpose_task_label}")
        self.warning_label.setText(f"{self._purpose_task_label} 추론 확인: {elapsed_seconds:.1f}s. 최종 OK는 차단됩니다.")
        if hasattr(self, "user_warning_label"):
            self.user_warning_label.setText(f"{self._purpose_task_label} 추론 확인: {elapsed_seconds:.1f}s. 최종 OK는 차단됩니다.")
            self._refresh_user_mode_labels()

    def _set_lpr_results(self, events: tuple[PlateOcrEvent, ...]) -> None:
        if self._purpose_task_id != PURPOSE_LPR_IMAGE:
            return
        if not events:
            self._purpose_lpr_results = ()
            self.instruction_label.setText("번호판 인식 실패: 결과 없음")
            self.warning_label.setText("번호판 이미지 LPR 결과가 없습니다. 최종 OK는 차단됩니다.")
            return
        merged = self._purpose_lpr_results + tuple(events)
        self._purpose_lpr_results = tuple(sorted(merged, key=lambda event: event.timestamp, reverse=True))
        latest = self._purpose_lpr_results[0]
        if self._raw_data_manager is not None:
            for event in events:
                self._record_raw(
                    self._raw_data_manager.record_plate,
                    event.plate_number,
                    confidence=event.confidence,
                    simulated=True,
                    at=event.timestamp,
                )
        suffix = f" 외 {len(self._purpose_lpr_results) - 1}건" if len(self._purpose_lpr_results) > 1 else ""
        self.instruction_label.setText(f"번호판 인식: {latest.plate_number}{suffix}")
        self.warning_label.setText("번호판 이미지 LPR 결과입니다. 최종 OK는 차단됩니다.")

    def _set_purpose_status(self, target_id: str, message: str) -> None:
        if "실행 중" in message:
            self.ai_detection_label.setText(
                _purpose_detection_label(
                    self._purpose_task_label,
                    self._detection_camera_ids,
                    self._detection_event_counts,
                    first_inference_seconds=self._purpose_task_first_inference_seconds,
                )
            )
            return
        self._detection_failed = True
        self.ai_detection_label.setText(f"{self._purpose_task_label or '목적 추론'} 오류")
        self.model_status_label.setText(f"목적 추론 실패: {self._purpose_task_label or target_id}")
        self.warning_label.setText(f"{self._purpose_task_label or target_id} 문제: {message[:120]}")
        if hasattr(self, "user_warning_label"):
            self.user_warning_label.setText(f"{self._purpose_task_label or target_id} 문제: {message[:120]}. 최종 OK는 차단됩니다.")

    def _stop_purpose_inference(self) -> None:
        raw_task_id = self._purpose_task_id
        if self._raw_data_manager is not None and raw_task_id:
            self._record_raw(self._raw_data_manager.record_ai_stopped, raw_task_id)
        self._purpose_task_enabled = False
        self._purpose_task_id = ""
        self._purpose_task_label = ""
        self._purpose_task_log_path = None
        self._purpose_task_started_at = None
        self._purpose_task_first_inference_seconds = None
        self._purpose_lpr_results = ()
        self._set_purpose_buttons_checked(False)
        self._set_purpose_button_texts()
        for worker in tuple(self._purpose_workers):
            worker.stop()
        for widget in self._all_camera_surfaces():
            widget.clear_detections()

    def _cleanup_purpose_worker(self, thread: QThread, worker: PurposeInferenceWorker) -> None:
        if thread in self._purpose_threads:
            self._purpose_threads.remove(thread)
        if worker in self._purpose_workers:
            self._purpose_workers.remove(worker)
        if self._purpose_task_enabled and not self._purpose_workers:
            failed = self._detection_failed
            label = self._purpose_task_label
            raw_task_id = self._purpose_task_id
            if self._raw_data_manager is not None and raw_task_id:
                self._record_raw(
                    self._raw_data_manager.record_ai_stopped,
                    raw_task_id,
                    reason="worker_finished",
                )
            self._purpose_task_enabled = False
            self._purpose_task_id = ""
            self._purpose_task_label = ""
            self._purpose_task_log_path = None
            self._purpose_task_started_at = None
            self._purpose_task_first_inference_seconds = None
            self._purpose_lpr_results = ()
            self._set_purpose_buttons_checked(False)
            self._set_purpose_button_texts()
            if failed:
                self.ai_detection_label.setText(f"{label} 오류")
            else:
                self.ai_detection_label.setText(f"{label} 완료")
                self.model_status_label.setText(f"목적 추론 완료: {label}")
                self.warning_label.setText(f"{label} 실행이 종료되었습니다. 결과는 로그를 확인하세요. 최종 OK는 차단됩니다.")
        pending_task_id = self._pending_user_purpose_task_id
        if pending_task_id and not self._purpose_workers:
            self._pending_user_purpose_task_id = ""
            self._start_purpose_inference(pending_task_id)

    def _set_purpose_buttons_checked(self, checked: bool, *, task_id: str = "") -> None:
        for current_task_id, button in self.purpose_task_buttons.items():
            button.setChecked(checked and current_task_id == task_id)

    def _set_purpose_button_texts(self) -> None:
        for task_id, button in self.purpose_task_buttons.items():
            label = PURPOSE_TASK_SPECS[task_id].label
            button.setText(f"{label} ON" if self._purpose_task_enabled and self._purpose_task_id == task_id else label)

    def _set_front_lpr_button_text(self) -> None:
        button = getattr(self, "front_lpr_button", None)
        if button is None:
            return
        button.setChecked(self._front_lpr_enabled)
        button.setText("정면카메라LPR ON" if self._front_lpr_enabled else "정면카메라LPR")

    def _cleanup_detection_worker(self, thread: QThread, worker: LiveDetectionWorker) -> None:
        if thread in self._detection_threads:
            self._detection_threads.remove(thread)
        if worker in self._detection_workers:
            self._detection_workers.remove(worker)
        if self._detection_enabled and not self._detection_workers:
            failed = self._detection_failed
            legacy_mode = self._detection_legacy_mode
            if self._raw_data_manager is not None:
                self._record_raw(
                    self._raw_data_manager.record_ai_stopped,
                    "legacy_detection" if legacy_mode else "general_detection",
                    reason="worker_finished",
                )
            self._detection_enabled = False
            self._detection_legacy_mode = False
            self._detection_camera_ids = ()
            self._detection_event_counts = {}
            self._detection_load_started_at = None
            self._detection_first_inference_seconds = None
            self._detection_model_label = ""
            self._detection_log_path = None
            self._detection_unconfirmed_reported = False
            self._set_detection_buttons_checked(False)
            self._set_detection_button_texts()
            if failed and hasattr(self, "ai_detection_label"):
                self.ai_detection_label.setText("이전 AI Detection 오류" if legacy_mode else "AI 추론 오류")
            if hasattr(self, "model_status_label"):
                if failed:
                    failed_model = Path(self.settings.hailo_hef_path).name if legacy_mode and self.settings is not None else "모델 미확인"
                    if worker.hef_path is not None:
                        failed_model = worker.hef_path.name
                    self.model_status_label.setText(f"추론 실패: {failed_model}")
                else:
                    selected_text = self._selected_hailo_model_path.name if self._selected_hailo_model_path is not None else "없음"
                    self.model_status_label.setText(f"모델 선택: {selected_text}")

    def _set_detection_buttons_checked(self, checked: bool, *, legacy_mode: bool = False) -> None:
        if hasattr(self, "legacy_ai_detection_button"):
            self.legacy_ai_detection_button.setChecked(checked and legacy_mode)

    def _set_detection_button_texts(self, *, legacy_mode: bool = False) -> None:
        if hasattr(self, "legacy_ai_detection_button"):
            text = "이전 AI Detection ON" if self._detection_enabled and legacy_mode else "이전 AI Detection"
            self.legacy_ai_detection_button.setText(text)

def _fresh_detections(detections: tuple[DetectionEvent, ...]) -> tuple[DetectionEvent, ...]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DETECTION_TTL_SECONDS)
    return tuple(event for event in detections if event.timestamp >= cutoff)


def _streaming_camera_ids(settings: Settings, runtime_status: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        camera.id
        for camera in settings.active_cameras
        if runtime_status.get(camera.id) == "정상 수신"
    )


def _next_rotation(rotation_degrees: int) -> int:
    return {0: 90, 90: 180, 180: 270, 270: 0}[rotation_degrees % 360]


def _rotation_label(rotation_degrees: int) -> str:
    return {
        0: "0도",
        90: "CCW 90도",
        180: "180도",
        270: "CW 90도",
    }[rotation_degrees % 360]


def _front_lpr_payload(event_path: Path) -> dict[str, object]:
    if not event_path.exists():
        return {"ok": False, "message": "정면카메라LPR 실패: 결과 없음"}
    best_plate = ""
    best_confidence: float | None = None
    best_bbox: dict[str, int] | None = None
    for line in event_path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "plate_ocr":
            candidate = payload.get("best_plate")
            if not isinstance(candidate, dict):
                continue
            plate_number = str(candidate.get("plate_number") or "").strip()
            if plate_number:
                best_plate = plate_number
                confidence = candidate.get("confidence")
                best_confidence = float(confidence) if isinstance(confidence, (float, int)) else None
                bbox = candidate.get("bbox")
                best_bbox = dict(bbox) if isinstance(bbox, dict) else None
            continue
        plate_number = str(payload.get("plate_number") or "").strip()
        if plate_number:
            best_plate = plate_number
    digits = "".join(char for char in best_plate if char.isdigit())
    if not digits:
        return {"ok": False, "message": "정면카메라LPR 실패: 결과 없음"}
    result: dict[str, object] = {
        "ok": True,
        "plate_number": best_plate,
        "last4": digits[-4:],
    }
    if best_confidence is not None:
        result["confidence"] = best_confidence
    if best_bbox is not None:
        result["plate_bbox"] = best_bbox
    return result


def _rotate_cv_frame(cv2, frame, rotation_degrees: int):  # noqa: ANN001, ANN201 - cv2/numpy are optional runtime deps.
    rotation = normalize_rotation_degrees(rotation_degrees)
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    return frame


def _cover_source_rect(source_size: QSize, target_size: QSize) -> QRect:
    source_width = source_size.width()
    source_height = source_size.height()
    target_width = target_size.width()
    target_height = target_size.height()
    if source_width <= 0 or source_height <= 0 or target_width <= 0 or target_height <= 0:
        return QRect(0, 0, max(0, source_width), max(0, source_height))
    source_aspect = source_width / source_height
    target_aspect = target_width / target_height
    if source_aspect > target_aspect:
        crop_width = max(1, min(source_width, int(round(source_height * target_aspect))))
        left = (source_width - crop_width) // 2
        return QRect(left, 0, crop_width, source_height)
    crop_height = max(1, min(source_height, int(round(source_width / target_aspect))))
    top = (source_height - crop_height) // 2
    return QRect(0, top, source_width, crop_height)


def _bbox_to_rect(
    event: DetectionEvent,
    image_rect: QRect,
    *,
    source_size: QSize | None = None,
    source_crop_rect: QRect | None = None,
) -> QRect | None:
    bbox = event.bbox
    if bbox.w <= 0 or bbox.h <= 0:
        return None
    x_norm, y_norm, w_norm, h_norm = _network_bbox_to_source_bbox(bbox.x, bbox.y, bbox.w, bbox.h, source_size)
    if source_size is not None and source_crop_rect is not None:
        return _cropped_bbox_to_rect(x_norm, y_norm, w_norm, h_norm, source_size, source_crop_rect, image_rect)
    x = image_rect.left() + int(x_norm * image_rect.width())
    y = image_rect.top() + int(y_norm * image_rect.height())
    width = max(2, int(w_norm * image_rect.width()))
    height = max(2, int(h_norm * image_rect.height()))
    rect = QRect(x, y, width, height).intersected(image_rect)
    if rect.isEmpty():
        return None
    return rect


def _cropped_bbox_to_rect(
    x_norm: float,
    y_norm: float,
    w_norm: float,
    h_norm: float,
    source_size: QSize,
    source_crop_rect: QRect,
    image_rect: QRect,
) -> QRect | None:
    source_width = source_size.width()
    source_height = source_size.height()
    if source_width <= 0 or source_height <= 0 or source_crop_rect.width() <= 0 or source_crop_rect.height() <= 0:
        return None
    source_box = QRect(
        int(round(x_norm * source_width)),
        int(round(y_norm * source_height)),
        max(1, int(round(w_norm * source_width))),
        max(1, int(round(h_norm * source_height))),
    )
    visible_box = source_box.intersected(source_crop_rect)
    if visible_box.isEmpty():
        return None
    scale_x = image_rect.width() / source_crop_rect.width()
    scale_y = image_rect.height() / source_crop_rect.height()
    x = image_rect.left() + int(round((visible_box.left() - source_crop_rect.left()) * scale_x))
    y = image_rect.top() + int(round((visible_box.top() - source_crop_rect.top()) * scale_y))
    width = max(2, int(round(visible_box.width() * scale_x)))
    height = max(2, int(round(visible_box.height() * scale_y)))
    rect = QRect(x, y, width, height).intersected(image_rect)
    if rect.isEmpty():
        return None
    return rect


def _network_bbox_to_source_bbox(
    x: float,
    y: float,
    w: float,
    h: float,
    source_size: QSize | None,
) -> tuple[float, float, float, float]:
    if source_size is None or source_size.width() <= 0 or source_size.height() <= 0:
        return x, y, w, h
    source_width = float(source_size.width())
    source_height = float(source_size.height())
    network_width = 640.0
    network_height = 640.0
    scale = min(network_width / source_width, network_height / source_height)
    scaled_width = source_width * scale
    scaled_height = source_height * scale
    pad_x = (network_width - scaled_width) / 2.0
    pad_y = (network_height - scaled_height) / 2.0
    net_x = x * network_width
    net_y = y * network_height
    net_w = w * network_width
    net_h = h * network_height
    return _clip_bbox(
        (net_x - pad_x) / scaled_width,
        (net_y - pad_y) / scaled_height,
        net_w / scaled_width,
        net_h / scaled_height,
    )


def _clip_bbox(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    left = max(0.0, min(1.0, x))
    top = max(0.0, min(1.0, y))
    right = max(0.0, min(1.0, x + w))
    bottom = max(0.0, min(1.0, y + h))
    return left, top, max(0.0, right - left), max(0.0, bottom - top)


def _detection_color(label: str) -> QColor:
    normalized = label.strip().lower()
    if normalized in {"person", "human"}:
        return QColor("#ef4444")
    if normalized in {"car", "truck", "bus", "vehicle"}:
        return QColor("#22c55e")
    return QColor("#facc15")


def _detection_label(event: DetectionEvent) -> str:
    return f"{event.label} {event.confidence:.2f}"


def _ai_detection_label(
    camera_ids: tuple[str, ...],
    event_counts: dict[str, int] | None = None,
    *,
    loading_seconds: float | None = None,
    first_inference_seconds: float | None = None,
) -> str:
    if not camera_ids:
        return "AI 추론 OFF"
    if event_counts is None:
        label = "AI 추론 ON: " + ", ".join(camera_ids)
    else:
        parts = [f"{camera_id}({event_counts.get(camera_id, 0)})" for camera_id in camera_ids]
        label = "AI 추론 ON: " + ", ".join(parts)
    if first_inference_seconds is not None:
        return f"{label} / first inference {first_inference_seconds:.1f}s"
    if loading_seconds is not None:
        return f"{label} / loading {loading_seconds:.1f}s"
    return label


def _legacy_ai_detection_label(camera_ids: tuple[str, ...], event_counts: dict[str, int] | None = None) -> str:
    if not camera_ids:
        return "이전 AI Detection OFF"
    if event_counts is None:
        return "이전 AI Detection ON: " + ", ".join(camera_ids)
    parts = [f"{camera_id}({event_counts.get(camera_id, 0)})" for camera_id in camera_ids]
    return "이전 AI Detection ON: " + ", ".join(parts)


def _purpose_detection_label(
    label: str,
    camera_ids: tuple[str, ...],
    event_counts: dict[str, int] | None = None,
    *,
    loading_seconds: float | None = None,
    first_inference_seconds: float | None = None,
) -> str:
    prefix = label or "목적 추론"
    if not camera_ids:
        text = f"{prefix} ON"
    elif event_counts is None:
        text = f"{prefix} ON: " + ", ".join(camera_ids)
    else:
        parts = [f"{camera_id}({event_counts.get(camera_id, 0)})" for camera_id in camera_ids]
        text = f"{prefix} ON: " + ", ".join(parts)
    if first_inference_seconds is not None:
        return f"{text} / first inference {first_inference_seconds:.1f}s"
    if loading_seconds is not None:
        return f"{text} / loading {loading_seconds:.1f}s"
    return text


def _prepare_operator_window(window: OperatorWindow, *, fullscreen: bool) -> None:
    screen = window.screen() or QApplication.primaryScreen()
    available = screen.availableGeometry() if screen is not None else QRect(0, 0, WINDOWED_MAX_WIDTH, WINDOWED_MAX_HEIGHT)
    window.setMinimumSize(0, 0)
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
    if fullscreen:
        window.setMaximumSize(16777215, 16777215)
        return

    maximum_width = min(WINDOWED_MAX_WIDTH, available.width())
    maximum_height = min(WINDOWED_MAX_HEIGHT, available.height())
    window.setMaximumSize(maximum_width, maximum_height)

    window.resize(
        min(WINDOWED_DEFAULT_WIDTH, maximum_width),
        min(WINDOWED_DEFAULT_HEIGHT, maximum_height),
    )


def launch_operator_ui(model: OperatorDisplayModel, settings: Settings | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = OperatorWindow(model, settings=settings)
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def stop_ui(_signum, _frame) -> None:  # noqa: ANN001 - Python signal handler signature.
        window.close()
        app.quit()

    for signum in previous_handlers:
        signal.signal(signum, stop_ui)
    _prepare_operator_window(window, fullscreen=model.fullscreen)
    if model.fullscreen:
        window.showFullScreen()
    else:
        window.show()
    try:
        return app.exec()
    finally:
        window.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def launch_from_settings(settings: Settings, model: OperatorDisplayModel) -> int:
    return launch_operator_ui(model, settings=settings)


def _stylesheet() -> str:
    return """
    QMainWindow, QWidget {
        background: #0b0f14;
        color: #e5e7eb;
        font-family: "Noto Sans CJK KR", "Noto Sans", sans-serif;
        letter-spacing: 0px;
    }
    #sidePanel {
        background: #151a21;
        border: 1px solid #374151;
    }
    #safetyLabel {
        min-height: 96px;
        font-size: 54px;
        font-weight: 800;
        border: 2px solid #ef4444;
        background: #3f1115;
        color: #fee2e2;
    }
    #safetyLabel[status="ready"] {
        border-color: #22c55e;
        background: #10351f;
        color: #dcfce7;
    }
    #safetyLabel[status="wait"] {
        border-color: #facc15;
        background: #3f3410;
        color: #fef9c3;
    }
    #instructionLabel {
        font-size: 32px;
        font-weight: 700;
        line-height: 1.25;
        padding: 12px;
        border: 1px solid #4b5563;
    }
    #userInstructionLabel {
        font-size: 34px;
        font-weight: 800;
        line-height: 1.25;
        padding: 10px;
        border: 1px solid #4b5563;
    }
    #userWarningLabel {
        font-size: 22px;
        font-weight: 700;
        color: #fecaca;
        padding: 8px;
        border: 1px solid #7f1d1d;
        background: #241316;
    }
    #userPlateLabel {
        min-width: 190px;
        font-size: 30px;
        font-weight: 800;
        padding: 10px;
        border: 1px solid #4b5563;
        background: #101820;
    }
    #warningLabel {
        font-size: 22px;
        font-weight: 600;
        color: #fecaca;
        padding: 10px;
        border: 1px solid #7f1d1d;
        background: #241316;
    }
    #telemetryLabel {
        font-size: 18px;
        font-family: "DejaVu Sans Mono", monospace;
        padding: 8px;
        border-bottom: 1px solid #293241;
    }
    #testTitleLabel {
        font-size: 26px;
        font-weight: 800;
        padding: 8px;
    }
    QPushButton {
        min-height: 38px;
        font-size: 17px;
        font-weight: 700;
        background: #1f2937;
        color: #e5e7eb;
        border: 1px solid #4b5563;
        padding: 8px;
    }
    QPushButton:disabled {
        color: #6b7280;
        background: #111827;
    }
    #smallModeButton {
        min-height: 30px;
        font-size: 14px;
        padding: 4px 8px;
    }
    #smallModeButton[active="true"] {
        border-color: #38bdf8;
        background: #123142;
        color: #e0f2fe;
    }
    #testLog {
        font-size: 15px;
        font-family: "DejaVu Sans Mono", monospace;
        background: #090d12;
        color: #d1d5db;
        border: 1px solid #374151;
    }
    #testListScroll {
        background: transparent;
        border: 0;
    }
    """ + DRIVER_STYLESHEET
