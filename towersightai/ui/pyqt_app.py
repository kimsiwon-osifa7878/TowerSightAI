from __future__ import annotations

import json
import logging
import signal
import socket
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from towersightai.camera.pipeline import build_preview_pipeline, normalize_rotation_degrees
from towersightai.config.settings import CameraRole, Settings
from towersightai.inference.events import DetectionEvent
from towersightai.inference.live_detection import latest_events
from towersightai.inference.purpose_tasks import (
    PlateOcrEvent,
    PURPOSE_LPR_IMAGE,
    PURPOSE_PERSON_PRESENCE,
    PURPOSE_PROCESS_MONITORING,
    PURPOSE_TASK_SPECS,
    PURPOSE_VEHICLE_DETECTION,
    PurposeInferenceRunner,
    build_purpose_process,
)
from towersightai.cli.fast_alpr_lpr import run_fast_alpr_lpr
from towersightai.plc.adapter import FakePLCAdapter
from towersightai.process.engine import EngineOutput, ParkingProcessEngine
from towersightai.process.settings_store import (
    DEFAULT_SETTINGS_PATH as OPERATOR_SETTINGS_PATH,
    OperatorRuntimeSettings,
    load_operator_settings,
    save_operator_settings,
    settings_from_payload,
)
from towersightai.diagnostics import DiagnosticResult, DiagnosticsService, DiagnosticStatus
from towersightai.inference.hailo_health import (
    HailoHealthSnapshot,
    collect_hailo_health,
    log_hailo_health,
    make_subprocess_temp_probe,
)
from towersightai.storage.hailo_incident import HailoIncidentReporter, IncidentReport
from towersightai.runtime_logging import DEFAULT_RUNTIME_LOG, new_run_id
from towersightai.state_machine.core import ParkingState
from towersightai.sensors.ld2410 import LD2410Frame, LD2410TCPService
from towersightai.storage.connection_test import NasConnectionTestResult, run_nas_connection_test
from towersightai.storage.file_transfer import NasFileTransferResult, upload_files_to_nas
from towersightai.calibration.checkerboard import CheckerboardSpec
from towersightai.calibration.share import (
    CalibrationEntry,
    CalibrationShareResult,
    fetch_calibration,
    list_remote_calibration,
    local_entries,
    publish_calibration,
)
from towersightai.calibration.ground import (
    REQUIRED_CORNERS,
    GroundCalibrationStore,
    GroundPoseResult,
    pallet_grid,
    pallet_outline,
    project_ground_points,
    scale_camera_matrix,
    solve_ground_pose,
)
from towersightai.calibration.intrinsics import (
    CAPTURE_POSES,
    DEFAULT_INTRINSICS_ROOT,
    MIN_SAMPLES as CALIBRATION_MIN_SAMPLES,
    CheckerboardDetection,
    IntrinsicsResult,
    IntrinsicsSample,
    IntrinsicsSessionStore,
    build_verification_image,
    calibrate_intrinsics,
    detect_checkerboard,
    evaluate_pose_fit,
    load_intrinsics,
    result_from_dict,
)
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
# Operator mode is the developer console. The sidebar is grouped into sections; every
# entry either navigates to a workspace page or performs a mode/lifecycle action.
SIDEBAR_SECTIONS = (
    ("운영", ("사용자 화면", "감시 설정", "주차 프로세스 테스트")),
    (
        "진단",
        (
            "전체 카메라",
            "차량 감지",
            "사람 감지",
            "번호판 인식",
            "레이더 (LD2410)",
            "NAS 연결 확인",
            "NAS 파일 전송",
            "카메라 캘리브레이션",
            "지면 기준점",
            "시스템 점검",
            "실행 로그",
        ),
    ),
    ("시스템", ("카메라 설정", "프로그램 종료")),
)
SIDEBAR_ACTION_LABELS = tuple(label for _section, labels in SIDEBAR_SECTIONS for label in labels)
LD2410_CONSOLE_MAX_LINES = 500
HAILO_HEALTH_INTERVAL_SECONDS = 60
# Healthy Hailo rows in the daily JSONL are thinned to this cadence; bad rows are always kept.
HAILO_HEALTH_RECORD_INTERVAL_SECONDS = 600
LOG_VIEW_TAIL_BYTES = 64 * 1024
LOG_VIEW_MAX_LINES = 1200
NAS_TEST_CLIP_SECONDS = 2.0
CALIBRATION_DETECT_INTERVAL_MS = 400
# Process-monitoring auto-start: wait for the streaming camera set to settle so one child is
# launched with every camera instead of front-only followed by an immediate relaunch, and back
# off after failed runs so RTSP 400 (Tapo session budget) storms cannot feed themselves.
MONITORING_CAMERA_SETTLE_SECONDS = 4.0
MONITORING_START_COOLDOWN_SECONDS = 10.0
MONITORING_FAILURE_COOLDOWN_SECONDS = 30.0
MONITORING_FAILURE_COOLDOWN_MAX_SECONDS = 120.0
# The board must sit inside the pose's on-screen target box continuously for this long before a
# frame is kept. The old "two consecutive detections anywhere in frame" rule fired within a second
# and captured boards that were nowhere near the wanted pose (field feedback 2026-09-16).
CALIBRATION_DWELL_SECONDS = 3.0
CALIBRATION_HOLD_SECONDS = 2.5
NAS_TEST_CLIP_FPS = 10
NAS_TEST_DIR = Path("artifacts/runtime/nas-connection-test")

try:
    from PyQt6.QtCore import QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QSpinBox,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
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
    #: Normalized (x, y) inside the drawn frame, emitted only while picking is enabled.
    picked = pyqtSignal(float, float)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.display_mode = "contain"
        self.status = "NG: 프레임 대기"
        self._frame: QImage | None = None
        self._detections: tuple[DetectionEvent, ...] = ()
        self._frame_size_text = ""
        self._vehicle_simulation = False
        # (bottom_left_x, bottom_right_x, top_left_x, top_right_x, top_y, stop_y), all normalized
        self._guide_overlay: tuple[float, float, float, float, float, float] | None = None
        self._plate_line: float | None = None
        self._marker_points: tuple[tuple[float, float], ...] = ()
        # Ground-calibration picking: labelled click markers and projected polylines.
        self._pick_markers: tuple[tuple[float, float, str], ...] = ()
        self._ground_overlay: tuple[tuple[tuple[float, float], ...], ...] = ()
        self._picking = False
        # (rect|None, state, progress, caption) for the calibration target box.
        self._target_box: tuple[tuple[float, float, float, float] | None, str, float, str] = (
            None,
            "waiting",
            0.0,
            "",
        )
        # Pixels reserved at the bottom for an overlay that covers the tile, such as the
        # driver bottom strip. Operator layouts keep this at 0.
        self.bottom_inset = 0
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

    def set_marker_points(self, points: tuple[tuple[float, float], ...]) -> None:
        """Normalized (0..1) image points drawn as small dots, e.g. detected checkerboard corners."""
        self._marker_points = tuple(points)
        self.update()

    def image_rect(self) -> QRect:
        """Where the frame is actually drawn inside the tile (same maths as paintEvent)."""
        if self._instrument:
            content = QRect(
                1,
                1 + self.HEADER_BAR_HEIGHT + 1,
                self.width() - 2,
                self.height() - self.HEADER_BAR_HEIGHT - self.FOOTER_BAR_HEIGHT - 4,
            )
        else:
            content = self.rect().adjusted(10, 10, -10, -10)
        frame = self._frame
        if frame is None or frame.isNull() or self.display_mode == "cover":
            return content
        scaled = frame.size()
        scaled.scale(content.size(), Qt.AspectRatioMode.KeepAspectRatio)
        rect = QRect(content)
        rect.setSize(scaled)
        rect.moveCenter(content.center())
        return rect

    def set_picking(self, enabled: bool) -> None:
        """Turn click-to-pick on. Only the ground-calibration page uses this."""
        self._picking = bool(enabled)
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    def set_pick_markers(self, markers: Sequence[tuple[float, float, str]]) -> None:
        """Labelled points the operator has clicked (normalized image coordinates)."""
        self._pick_markers = tuple((float(x), float(y), str(label)) for x, y, label in markers)
        self.update()

    def set_ground_overlay(self, polylines: Sequence[Sequence[tuple[float, float]]]) -> None:
        """Projected ground geometry (pallet outline, metric grid) in normalized coordinates."""
        self._ground_overlay = tuple(tuple((float(x), float(y)) for x, y in line) for line in polylines)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        if self._picking and event.button() == Qt.MouseButton.LeftButton:
            rect = self.image_rect()
            if rect.width() > 0 and rect.height() > 0:
                position = event.position() if hasattr(event, "position") else event.pos()
                x = (position.x() - rect.left()) / rect.width()
                y = (position.y() - rect.top()) / rect.height()
                if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                    self.picked.emit(float(x), float(y))
                    return
        super().mousePressEvent(event)

    def set_target_box(
        self,
        rect: tuple[float, float, float, float] | None,
        *,
        state: str = "waiting",
        progress: float = 0.0,
        caption: str = "",
    ) -> None:
        """Calibration target box: put the checkerboard inside this rectangle.

        ``rect`` is normalized (x0, y0, x1, y1) and is drawn at the A4-landscape aspect the
        caller computed. ``state`` is ``waiting`` (board not in the box), ``holding``
        (inside, counting down) or ``captured``; ``progress`` 0..1 fills the countdown bar.
        Operator calibration page only — the driver view never sets this.
        """
        payload = (rect, state, round(float(progress), 3), caption)
        if payload == self._target_box:
            return
        self._target_box = payload
        self.update()

    def clear_detections(self) -> None:
        self._detections = ()
        self.update()

    def set_vehicle_simulation(self, enabled: bool) -> None:
        self._vehicle_simulation = enabled
        self.update()

    def set_guide_overlay(self, guides: tuple[float, float, float, float, float, float] | None) -> None:
        """Trapezoidal wheel guides on the front camera.

        ``guides`` = (bottom_left_x, bottom_right_x, top_left_x, top_right_x,
        top_y, stop_y), all normalized. Wide at the bottom, narrow at the top.
        """
        if guides == self._guide_overlay:
            return
        self._guide_overlay = guides
        self.update()

    def set_plate_line(self, y_norm: float | None) -> None:
        """Plate-zone boundary preview. Operator settings page only, never the driver view."""
        if y_norm == self._plate_line:
            return
        self._plate_line = y_norm
        self.update()

    def set_display_mode(self, mode: str) -> None:
        if mode not in {"contain", "cover"}:
            raise ValueError(f"Unsupported camera display mode: {mode}")
        self.display_mode = mode
        self.update()

    def set_bottom_inset(self, pixels: int) -> None:
        inset = max(0, int(pixels))
        if inset == self.bottom_inset:
            return
        self.bottom_inset = inset
        self.update()

    # Proposal-B instrument chrome (operator "contain" tiles only).
    HEADER_BAR_HEIGHT = 30
    FOOTER_BAR_HEIGHT = 26

    @property
    def _instrument(self) -> bool:
        """Operator tiles draw header/footer bars; driver-view (cover) tiles stay chromeless."""
        return self.display_mode == "contain"

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        super().paintEvent(event)
        painter = QPainter(self)
        is_ng = self.status.startswith("NG")
        if self._instrument:
            painter.fillRect(self.rect(), QColor("#10161F"))
            frame_color = QColor("#8A3A40") if is_ng else QColor("#232C39")
            painter.setPen(QPen(frame_color, 2))
            painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
            header = QRect(1, 1, self.width() - 2, self.HEADER_BAR_HEIGHT)
            footer = QRect(1, self.height() - self.FOOTER_BAR_HEIGHT - 1, self.width() - 2, self.FOOTER_BAR_HEIGHT)
            painter.fillRect(header, QColor("#2A161A") if is_ng else QColor("#1A212C"))
            painter.fillRect(footer, QColor("#201216") if is_ng else QColor("#131A24"))
            painter.setPen(QPen(frame_color, 1))
            painter.drawLine(header.left(), header.bottom() + 1, header.right(), header.bottom() + 1)
            painter.drawLine(footer.left(), footer.top() - 1, footer.right(), footer.top() - 1)
            content = QRect(
                1,
                header.bottom() + 2,
                self.width() - 2,
                self.height() - self.HEADER_BAR_HEIGHT - self.FOOTER_BAR_HEIGHT - 4,
            )
        else:
            painter.fillRect(self.rect(), QColor("#0d1119"))
            content = self.rect().adjusted(10, 10, -10, -10)
            painter.setPen(QPen(QColor("#2a3850"), 2))
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
                # Copy the rect: aliasing `content` here used to anchor the image to the
                # tile's top-left and corrupt every later overlay that reads `content`.
                image_rect = QRect(content)
                image_rect.setSize(scaled_size)
                image_rect.moveCenter(content.center())
                painter.drawImage(image_rect, display_frame)
        else:
            image_rect = content
            painter.setPen(QPen(QColor("#94a3b8"), 1))
            painter.drawLine(center_x, content.top() + 24, center_x, content.bottom() - 24)
            painter.drawLine(content.left() + 36, center_y, content.right() - 36, center_y)

        target_rect, target_state, target_progress, target_caption = self._target_box
        if target_rect is not None and self.display_mode != "cover":
            x0, y0, x1, y1 = target_rect
            box = QRect(
                image_rect.left() + int(image_rect.width() * x0),
                image_rect.top() + int(image_rect.height() * y0),
                max(int(image_rect.width() * (x1 - x0)), 2),
                max(int(image_rect.height() * (y1 - y0)), 2),
            )
            colors = {"waiting": "#94a3b8", "holding": "#F5A623", "captured": "#3DD68C"}
            box_color = QColor(colors.get(target_state, "#94a3b8"))
            painter.setPen(QPen(box_color, 3, Qt.PenStyle.DashLine if target_state == "waiting" else Qt.PenStyle.SolidLine))
            painter.drawRect(box)
            # Corner ticks make the box readable against a busy scene.
            tick = max(min(box.width(), box.height()) // 8, 8)
            painter.setPen(QPen(box_color, 5))
            for cx, cy, dx, dy in (
                (box.left(), box.top(), 1, 1),
                (box.right(), box.top(), -1, 1),
                (box.left(), box.bottom(), 1, -1),
                (box.right(), box.bottom(), -1, -1),
            ):
                painter.drawLine(cx, cy, cx + tick * dx, cy)
                painter.drawLine(cx, cy, cx, cy + tick * dy)
            if target_progress > 0.0:
                bar = QRect(box.left(), box.bottom() + 8, int(box.width() * min(target_progress, 1.0)), 8)
                painter.fillRect(QRect(box.left(), box.bottom() + 8, box.width(), 8), QColor(20, 26, 34, 180))
                painter.fillRect(bar, box_color)
            if target_caption:
                painter.setPen(QPen(box_color, 1))
                painter.drawText(
                    QRect(box.left(), max(box.top() - 26, image_rect.top()), box.width(), 22),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    target_caption,
                )

        if self._ground_overlay and self.display_mode != "cover":
            painter.setPen(QPen(QColor("#C78BFF"), 1))
            for line in self._ground_overlay:
                points = [
                    QPoint(
                        image_rect.left() + int(x * image_rect.width()),
                        image_rect.top() + int(y * image_rect.height()),
                    )
                    for x, y in line
                ]
                for start, end in zip(points, points[1:]):
                    painter.drawLine(start, end)

        if self._pick_markers and self.display_mode != "cover":
            for x, y, label in self._pick_markers:
                px = image_rect.left() + int(x * image_rect.width())
                py = image_rect.top() + int(y * image_rect.height())
                painter.setPen(QPen(QColor("#3DD68C"), 3))
                painter.drawLine(px - 12, py, px + 12, py)
                painter.drawLine(px, py - 12, px, py + 12)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(px - 7, py - 7, 14, 14)
                if label:
                    painter.setPen(QPen(QColor("#E6EDF5"), 1))
                    painter.drawText(px + 14, py - 8, label)

        if self._marker_points and display_frame is not None and self.display_mode != "cover":
            painter.setPen(QPen(QColor("#F5A623"), 2))
            painter.setBrush(QColor("#F5A623"))
            for nx, ny in self._marker_points:
                px = image_rect.left() + int(nx * image_rect.width())
                py = image_rect.top() + int(ny * image_rect.height())
                painter.drawEllipse(px - 3, py - 3, 6, 6)
            painter.setBrush(Qt.BrushStyle.NoBrush)

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

        if self._guide_overlay is not None:
            bottom_left, bottom_right, top_left, top_right, top_y, stop_y = self._guide_overlay
            guide_color = QColor("#F5A623")

            def guide_point(x_norm: float, y_norm: float) -> tuple[int, int]:
                return (
                    image_rect.left() + int(image_rect.width() * x_norm),
                    image_rect.top() + int(image_rect.height() * y_norm),
                )

            def guide_x_at(y_norm: float) -> tuple[int, int]:
                # Interpolate the two trapezoid sides at height y (top_y..1.0).
                span = max(1e-6, 1.0 - top_y)
                frac = min(1.0, max(0.0, (1.0 - y_norm) / span))
                left = bottom_left + (top_left - bottom_left) * frac
                right = bottom_right + (top_right - bottom_right) * frac
                return (
                    image_rect.left() + int(image_rect.width() * left),
                    image_rect.left() + int(image_rect.width() * right),
                )

            bl = guide_point(bottom_left, 1.0)
            br = guide_point(bottom_right, 1.0)
            tl = guide_point(top_left, top_y)
            tr = guide_point(top_right, top_y)
            painter.setPen(QPen(guide_color, 3, Qt.PenStyle.DashLine))
            painter.drawLine(bl[0], bl[1], tl[0], tl[1])   # left wheel path
            painter.drawLine(br[0], br[1], tr[0], tr[1])   # right wheel path
            painter.drawLine(tl[0], tl[1], tr[0], tr[1])   # far (narrow) edge
            stop_left_x, stop_right_x = guide_x_at(stop_y)
            stop_line_y = image_rect.top() + int(image_rect.height() * stop_y)
            painter.setPen(QPen(guide_color, 3))
            painter.drawLine(stop_left_x, stop_line_y, stop_right_x, stop_line_y)
            painter.setPen(guide_color)
            painter.drawText(
                QRect(stop_left_x + 6, stop_line_y - 26, 160, 22),
                Qt.AlignmentFlag.AlignVCenter,
                "정지선",
            )

        if self._plate_line is not None:
            line_color = QColor("#38bdf8")
            plate_y = image_rect.top() + int(image_rect.height() * self._plate_line)
            painter.setPen(QPen(line_color, 2, Qt.PenStyle.DashDotLine))
            painter.drawLine(image_rect.left(), plate_y, image_rect.right(), plate_y)
            painter.setPen(line_color)
            painter.drawText(
                QRect(image_rect.left() + 8, plate_y + 4, 300, 22),
                Qt.AlignmentFlag.AlignVCenter,
                "차량진입선 (이 선 아래 번호판 = 진입)",
            )

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

        if is_ng:
            painter.fillRect(image_rect, QColor(91, 8, 18, 132))
            painter.setPen(QPen(QColor("#ffffff"), 2))
            fault_text = "영상 수신 불가" if self._frame is None or self._frame.isNull() else "카메라 입력 확인 필요"
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, fault_text)

        if self._instrument:
            title_font = QFont(painter.font())
            title_font.setBold(True)
            painter.setFont(title_font)
            header_text = QRect(13, 1, self.width() - 26, self.HEADER_BAR_HEIGHT)
            painter.setPen(QColor("#F3B0B6") if is_ng else QColor("#E9EDF3"))
            painter.drawText(header_text, Qt.AlignmentFlag.AlignVCenter, self.title)
            if self._frame_size_text:
                painter.setPen(QColor("#667182"))
                painter.drawText(
                    header_text,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    self._frame_size_text,
                )
            footer_text = QRect(13, self.height() - self.FOOTER_BAR_HEIGHT - 1, self.width() - 26, self.FOOTER_BAR_HEIGHT)
            status_color = QColor("#F09A9E") if is_ng else QColor("#3DD68C")
            if not is_ng and self.status == "정상 수신":
                painter.setBrush(status_color)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(footer_text.left(), footer_text.center().y() - 3, 7, 7)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                footer_text = footer_text.adjusted(13, 0, 0, 0)
            painter.setPen(status_color)
            painter.drawText(footer_text, Qt.AlignmentFlag.AlignVCenter, self.status)
        else:
            painter.setPen(QColor("#e5e7eb"))
            painter.drawText(content.adjusted(12, 10, -12, -10), Qt.AlignmentFlag.AlignTop, self.title)
            if self._frame_size_text:
                painter.setPen(QColor("#cbd5e1"))
                painter.drawText(content.adjusted(-140, 10, -12, -10), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, self._frame_size_text)
            painter.setPen(QColor("#f87171" if is_ng else "#86efac"))
            painter.drawText(
                content.adjusted(12, -32, -12, -8 - self.bottom_inset),
                Qt.AlignmentFlag.AlignBottom,
                self.status,
            )


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
        self._gstreamer_fallback_logged = False

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        logger = logging.getLogger("towersightai.camera.capture")
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            logger.error("camera-capture-opencv-missing camera=%s", self.camera.id)
            self.status_changed.emit(self.camera.id, "NG: OpenCV 미설치")
            self.finished.emit()
            return

        last_logged_status = ""

        def log_status(status: str, *, backend: str = "") -> None:
            nonlocal last_logged_status
            if status == last_logged_status:
                return
            last_logged_status = status
            logger.info(
                "camera-capture-status camera=%s status=%s backend=%s",
                self.camera.id,
                status,
                backend or "-",
            )

        while self._running:
            capture, using_gstreamer = self._open_capture(cv2)
            backend = "gstreamer" if using_gstreamer else "ffmpeg-fallback"
            if not capture.isOpened():
                capture.release()
                log_status("open-failed", backend=backend)
                self.status_changed.emit(self.camera.id, "NG: 카메라 연결 이상")
                QThread.sleep(1)
                continue

            log_status("streaming", backend=backend)
            self.status_changed.emit(self.camera.id, "정상 수신")
            missed_frames = 0
            while self._running:
                ok, frame = capture.read()
                if not ok or frame is None:
                    missed_frames += 1
                    log_status("frame-stall", backend=backend)
                    self.status_changed.emit(self.camera.id, "NG: 프레임 지연")
                    if missed_frames >= 10:
                        log_status("reconnecting", backend=backend)
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
                log_status("streaming", backend=backend)
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
            # cv2 builds without GStreamer (pip opencv-python wheels) cannot open the
            # preview pipeline at all. Fall back to a direct FFmpeg RTSP capture for every
            # active camera; run() applies the configured rotation and BGR->RGB on this
            # path. A failed fallback still surfaces as an NG tile and blocks final OK.
            capture.release()
            if not self._gstreamer_fallback_logged:
                self._gstreamer_fallback_logged = True
                logging.getLogger("towersightai.camera.capture").warning(
                    "camera-capture-gstreamer-unavailable camera=%s falling back to FFmpeg RTSP",
                    self.camera.id,
                )
            capture = cv2.VideoCapture(self.camera.rtsp_url)
        return capture, using_gstreamer


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


class PeriodicFrontLprWorker(QObject):
    """Persistent FastALPR loop for the process engine's 1 Hz plate reading.

    Lives on its own QThread for the whole plate phase: the ONNX model is
    initialized once (FastAlprSession) instead of per frame. The GUI thread
    pushes one front-camera frame at a time via the ``process_frame`` slot and
    gates on the returned signals, so a slow recognition simply skips cycles.
    """

    attempt_ready = pyqtSignal(object, int, int)  # payload, frame_width, frame_height
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, output_dir: Path) -> None:
        super().__init__()
        self._output_dir = output_dir
        self._session = None
        self._session_failed = False
        self._event_path = output_dir / "attempts.jsonl"
        self._frame_index = 0

    def process_frame(self, frame: QImage) -> None:
        if self._session_failed:
            return
        if self._session is None:
            try:
                from towersightai.cli.fast_alpr_lpr import FastAlprSession

                self.status_changed.emit("번호판 모델 로드 중")
                self._session = FastAlprSession()
                self.status_changed.emit("번호판 1초 주기 인식 시작")
            except Exception as exc:  # noqa: BLE001 - engine degrades to 미인식
                self._session_failed = True
                self.failed.emit(f"번호판 모델 로드 실패: {exc}")
                return
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            image_path = self._output_dir / f"frame-{stamp}.png"
            if not frame.save(str(image_path), "PNG"):
                self.status_changed.emit("번호판 프레임 저장 실패")
                return
            payload = self._session.recognize_image(image_path, image_index=self._frame_index)
            self._frame_index += 1
            with self._event_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            self.attempt_ready.emit(payload, frame.width(), frame.height())
        except Exception as exc:  # noqa: BLE001
            self.status_changed.emit(f"번호판 인식 오류: {exc}")


def _qimage_to_bgr(frame: QImage):  # noqa: ANN202 - numpy array; numpy imported lazily.
    """Convert a preview QImage to a contiguous BGR numpy array (copy)."""
    import numpy as np

    image = frame.convertToFormat(QImage.Format.Format_RGB888)
    height, width = image.height(), image.width()
    buffer = image.constBits()
    buffer.setsize(height * image.bytesPerLine())
    # Rows are padded to 4 bytes, so slice each row to its real payload before reshaping.
    rows = np.frombuffer(buffer, dtype=np.uint8).reshape((height, image.bytesPerLine()))
    array = rows[:, : width * 3].reshape((height, width, 3))
    return np.ascontiguousarray(array[:, :, ::-1])


def _bgr_to_qimage(array) -> QImage:  # noqa: ANN001 - numpy array; numpy imported lazily.
    """Convert a contiguous BGR numpy array to a QImage that owns its bytes."""
    import numpy as np

    rgb = np.ascontiguousarray(array[:, :, ::-1])
    height, width = rgb.shape[:2]
    image = QImage(rgb.data, width, height, width * 3, QImage.Format.Format_RGB888)
    return image.copy()  # detach from the numpy buffer before it goes out of scope


class CheckerboardDetectWorker(QObject):
    """Find checkerboard corners in preview frames off the UI thread."""

    detect_requested = pyqtSignal(int, object)  # token, bgr frame (queued into the worker thread)
    detected = pyqtSignal(int, object, object)  # token, bgr frame, CheckerboardDetection | None

    def __init__(self, spec: CheckerboardSpec, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.detect_requested.connect(self.detect)

    @pyqtSlot(int, object)
    def detect(self, token: int, bgr) -> None:  # noqa: ANN001 - numpy array.
        detection = None
        try:
            import cv2

            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            detection = detect_checkerboard(gray, self.spec)
        except Exception:  # noqa: BLE001 - a detector failure must not kill the thread.
            logging.getLogger("towersightai.calibration").exception("checkerboard-detect-failed")
        self.detected.emit(token, bgr, detection)


class IntrinsicsCalibrateWorker(QObject):
    """Run cv2.calibrateCamera over the captured samples and save the result files."""

    result_ready = pyqtSignal(object, object, object)  # IntrinsicsResult, session path, latest path
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        store: IntrinsicsSessionStore,
        samples: tuple[IntrinsicsSample, ...],
        spec: CheckerboardSpec,
        *,
        camera_id: str,
        rotation_degrees: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.samples = samples
        self.spec = spec
        self.camera_id = camera_id
        self.rotation_degrees = rotation_degrees

    def run(self) -> None:
        logger = logging.getLogger("towersightai.calibration")
        try:
            result = calibrate_intrinsics(
                self.samples, self.spec, camera_id=self.camera_id, rotation_degrees=self.rotation_degrees
            )
            result_path, latest_path = self.store.save_result(result)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the UI.
            logger.exception("intrinsics-calibration-failed camera=%s", self.camera_id)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            logger.info(
                "intrinsics-calibration-end camera=%s samples=%s rms=%.4f quality=%s path=%s",
                self.camera_id, result.sample_count, result.rms_reprojection_error, result.quality, latest_path,
            )
            self.result_ready.emit(result, result_path, latest_path)
        self.finished.emit()


class NasFileTransferWorker(QObject):
    """Upload operator-selected files into the NAS transfer folder off the UI thread.

    Relay only: the result never changes safety state, calibration state, or PLC output.
    """

    status_changed = pyqtSignal(str)
    result_ready = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(
        self,
        config,  # noqa: ANN001 - RawStorageConfig, kept untyped to avoid a settings import cycle.
        files: tuple[Path, ...],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.files = tuple(Path(item) for item in files)
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        logger = logging.getLogger("towersightai.storage.nas_transfer")
        if self._stop_requested:
            self.finished.emit()
            return

        def progress(index: int, total: int, name: str) -> None:
            self.status_changed.emit(f"NAS 파일 전송: ({index}/{total}) {name} 업로드 중")

        result = upload_files_to_nas(self.config, self.files, progress=progress)
        logger.info(
            "nas-transfer-end ok=%s remote_dir=%s files=%s bytes=%s error=%s",
            result.ok,
            result.remote_dir,
            len(result.artifacts),
            result.total_bytes,
            result.error,
        )
        self.result_ready.emit(result)
        self.finished.emit()


class CalibrationShareWorker(QObject):
    """Publish or fetch calibration measurements over SFTP, off the UI thread.

    Sharing a measurement never changes safety state: the files stay `reviewed: false` and
    `safe_to_operate: false` wherever they land.
    """

    status_changed = pyqtSignal(str)
    result_ready = pyqtSignal(object)
    listing_ready = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(
        self,
        mode: str,  # "publish" | "list" | "fetch"
        config,  # noqa: ANN001 - RawStorageConfig
        root: Path,
        entries: tuple = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.config = config
        self.root = Path(root)
        self.entries = tuple(entries)

    def run(self) -> None:
        logger = logging.getLogger("towersightai.calibration.share")
        try:
            if self.mode == "publish":
                def progress(index: int, total: int, name: str) -> None:
                    self.status_changed.emit(f"NAS 공유: ({index}/{total}) {name}")

                result = publish_calibration(self.config, self.root, progress=progress)
                self.result_ready.emit(result)
            elif self.mode == "list":
                self.status_changed.emit("NAS에서 측정 파일 목록을 읽는 중")
                self.listing_ready.emit(list_remote_calibration(self.config))
            else:
                def progress(index: int, total: int, name: str) -> None:
                    self.status_changed.emit(f"NAS에서 가져오는 중: ({index}/{total}) {name}")

                result = fetch_calibration(self.config, self.entries, self.root, progress=progress)
                self.result_ready.emit(result)
        except Exception as exc:  # noqa: BLE001 - never let a network error kill the console
            logger.exception("calibration-share-failed mode=%s", self.mode)
            self.result_ready.emit(CalibrationShareResult(False, "공유 작업 실패", error=str(exc)))
        finally:
            self.finished.emit()


class NasConnectionTestWorker(QObject):
    """Encode the collected preview frames and write one test payload to the NAS.

    Diagnostic only: the result never changes safety state, calibration state, or PLC output.
    """

    status_changed = pyqtSignal(str)
    result_ready = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(
        self,
        config,  # noqa: ANN001 - RawStorageConfig, kept untyped to avoid a settings import cycle.
        frames: tuple[QImage, ...] = (),
        *,
        camera_id: str = "",
        work_dir: Path = NAS_TEST_DIR,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.frames = tuple(frame.copy() for frame in frames)
        self.camera_id = camera_id
        self.work_dir = Path(work_dir)
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        logger = logging.getLogger("towersightai.storage.nas_check")
        if self._stop_requested:
            self.finished.emit()
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        video_path: Path | None = None
        video_error = ""
        if self.frames:
            self.status_changed.emit(f"NAS 연결 확인: {self.camera_id or 'camera'} 영상 {len(self.frames)}프레임 인코딩 중")
            try:
                video_path = self._encode_clip()
            except Exception as exc:  # noqa: BLE001 - a clip failure must not hide the NAS result.
                video_error = f"{type(exc).__name__}: {exc}"
                logger.exception("nas-check-clip-encode-failed camera=%s", self.camera_id)

        metadata = {
            "camera_id": self.camera_id,
            "frame_count": len(self.frames),
            "clip_seconds": round(len(self.frames) / NAS_TEST_CLIP_FPS, 2) if self.frames else 0.0,
            "clip_fps": NAS_TEST_CLIP_FPS if self.frames else 0,
            "video_error": video_error,
        }
        self.status_changed.emit("NAS 연결 확인: 업로드 중")
        result = run_nas_connection_test(
            self.config,
            work_dir=self.work_dir,
            metadata=metadata,
            video_path=video_path,
        )
        logger.info(
            "nas-check-end ok=%s remote_dir=%s files=%s bytes=%s error=%s",
            result.ok,
            result.remote_dir,
            len(result.artifacts),
            result.total_bytes,
            result.error,
        )
        self.result_ready.emit(result)
        self.finished.emit()

    def _encode_clip(self) -> Path | None:
        import cv2
        import numpy as np

        first = self.frames[0]
        width, height = first.width(), first.height()
        if width <= 0 or height <= 0:
            return None
        path = self.work_dir / f"camera-clip-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ')}.mp4"
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            float(NAS_TEST_CLIP_FPS),
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError("cv2.VideoWriter could not open the clip file")
        try:
            for frame in self.frames:
                bgr = _qimage_to_bgr(frame)
                if (frame.width(), frame.height()) != (width, height):
                    bgr = cv2.resize(bgr, (width, height))
                writer.write(np.ascontiguousarray(bgr))
        finally:
            writer.release()
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("encoded clip is empty")
        return path


class HailoHealthWorker(QObject):
    """Collect Hailo device health every HAILO_HEALTH_INTERVAL_SECONDS off the UI thread.

    Diagnostic telemetry only — snapshots inform the operator and the runtime log,
    never the safety gate.
    """

    snapshot_ready = pyqtSignal(object)
    incident_reported = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(
        self,
        settings: Settings,
        *,
        interval_seconds: int = HAILO_HEALTH_INTERVAL_SECONDS,
        reporter: HailoIncidentReporter | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.interval_seconds = max(5, int(interval_seconds))
        self.reporter = reporter
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        temp_probe = make_subprocess_temp_probe(self.settings.hailo_apps_python)
        reporter = self.reporter or HailoIncidentReporter(self.settings.raw_storage)
        if not reporter.enabled:
            logging.getLogger("towersightai.hailo.incident").info(
                "hailo-incident-upload disabled (RAW_DATA_ENABLED/HAILO_INCIDENT_UPLOAD_ENABLED/NAS host)"
            )
        previous: HailoHealthSnapshot | None = None
        while self._running:
            snapshot = collect_hailo_health(
                temp_probe=temp_probe,
                previous_rxerr=previous.rxerr_count if previous is not None else None,
            )
            log_hailo_health(snapshot, previous=previous)
            self.snapshot_ready.emit(snapshot)
            previous = snapshot
            # Evidence upload runs on this worker thread (never the UI thread) and never raises.
            # A worker asked to stop skips the upload so shutdown is never held by an SFTP call.
            try:
                report = reporter.observe(snapshot) if self._running else IncidentReport(False, "stopping")
            except Exception:  # noqa: BLE001 - diagnostics must not stop health monitoring.
                logging.getLogger("towersightai.hailo.incident").exception("hailo-incident-observe-failed")
            else:
                if report.reported:
                    self.incident_reported.emit(report)
            for _ in range(self.interval_seconds):
                if not self._running:
                    break
                QThread.sleep(1)
        self.finished.emit()


class DiagnosticsWorker(QObject):
    """Run one DiagnosticsService test off the UI thread. Results never authorize OK."""

    result_ready = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, settings: Settings, test_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.test_id = test_id

    def run(self) -> None:
        try:
            service = DiagnosticsService(self.settings)
            result = service.run(self.test_id, timeout_seconds=30)
        except Exception as exc:  # noqa: BLE001 - diagnostics must report, not crash the UI.
            logging.getLogger("towersightai.diagnostics").exception(
                "diagnostics-worker-crashed test=%s", self.test_id
            )
            result = DiagnosticResult(
                test_id=self.test_id,
                label=self.test_id,
                status=DiagnosticStatus.FAIL,
                summary=f"진단 실행 실패: {exc}",
            )
        self.result_ready.emit(result)
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
    ld2410_frame_ready = pyqtSignal(object, str)
    ld2410_status_ready = pyqtSignal(str, object)
    periodic_lpr_frame = pyqtSignal(object)

    def __init__(self, model: OperatorDisplayModel, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.settings = settings
        self.camera_widgets: dict[CameraRole, CameraSurface] = {}
        self.driver_preview_camera_widgets: dict[CameraRole, CameraSurface] = {}
        self._runtime_camera_status: dict[str, str] = {}
        self._threads: list[QThread] = []
        self._workers: list[CameraCaptureWorker] = []
        self._purpose_threads: list[QThread] = []
        self._purpose_workers: list[PurposeInferenceWorker] = []
        self._front_lpr_threads: list[QThread] = []
        self._front_lpr_workers: list[FrontCameraLprWorker] = []
        self._nas_test_threads: list[QThread] = []
        self._nas_test_workers: list[NasConnectionTestWorker] = []
        self._nas_test_running = False
        self._nas_test_frames: list[QImage] = []
        self._nas_test_camera_id = ""
        self._nas_test_timer: QTimer | None = None
        self._nas_test_ticks = 0
        self._nas_test_widget: CameraSurface | None = None
        self._nas_transfer_threads: list[QThread] = []
        self._nas_transfer_workers: list[NasFileTransferWorker] = []
        self._nas_transfer_running = False
        self._nas_transfer_files: list[Path] = []
        self._calib_spec = CheckerboardSpec()
        self._calib_store: IntrinsicsSessionStore | None = None
        self._calib_samples: list[IntrinsicsSample] = []
        self._calib_pose_index = 0
        self._calib_camera_id = ""
        self._calib_running = False
        self._calib_calibrating = False
        self._calib_detect_busy = False
        self._calib_token = 0
        self._calib_dwell_start: float | None = None
        self._calib_hold_until = 0.0
        self._calib_capture_requested = False
        self._calib_timer: QTimer | None = None
        self._calib_detect_thread: QThread | None = None
        self._calib_detect_worker: CheckerboardDetectWorker | None = None
        self._calib_sharing = False
        self._calib_share_threads: list[QThread] = []
        self._calib_share_workers: list[object] = []
        self._ground_points: list[tuple[float, float]] = []
        self._ground_result: GroundPoseResult | None = None
        self._ground_picking = False
        self._calib_threads: list[QThread] = []
        self._calib_workers: list[IntrinsicsCalibrateWorker] = []
        self._detection_camera_ids: tuple[str, ...] = ()
        self._detection_event_counts: dict[str, int] = {}
        self._detection_failed = False
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
        self._ld2410_service: LD2410TCPService | None = None
        self._ld2410_console_lines: deque[str] = deque(maxlen=LD2410_CONSOLE_MAX_LINES)
        self._ld2410_console_paused = False
        self._ld2410_connection_state = "disabled" if settings is None or not settings.ld2410.enabled else "listening"
        self._ld2410_status_details: dict[str, object] = {}
        self._ld2410_last_frame_at: datetime | None = None
        self._ld2410_client_ip = ""
        self._camera_layout_mode = "all"
        self._camera_rotations: dict[str, int] = {
            camera.id: camera.rotation_degrees
            for camera in settings.cameras
        } if settings is not None else {}
        self._selected_hailo_model_path: Path | None = None
        self.sidebar_buttons: dict[str, QPushButton] = {}
        self.purpose_task_buttons: dict[str, QPushButton] = {}
        # Extra run/stop buttons for the same task on other pages (e.g. 전체 카메라).
        self.purpose_task_extra_buttons: dict[str, list[QPushButton]] = {}
        self.camera_rotation_buttons: dict[str, QPushButton] = {}
        self._system_test_threads: list[QThread] = []
        self._system_test_workers: list[QObject] = []
        self._system_test_running = False
        self._hailo_health_threads: list[QThread] = []
        self._hailo_health_workers: list[HailoHealthWorker] = []
        self._hailo_health_snapshot: HailoHealthSnapshot | None = None
        self._hailo_health_recorded_status = ""
        self._hailo_health_recorded_at: float | None = None
        # --- continuous parking-process engine (auto-started; PLC is simulated) ---
        self.operator_settings: OperatorRuntimeSettings = load_operator_settings()
        self.plc_adapter = FakePLCAdapter()
        self.process_engine: ParkingProcessEngine | None = (
            ParkingProcessEngine(self.operator_settings) if settings is not None else None
        )
        self._engine_enabled = settings is not None
        self._engine_owns_display = False
        self._engine_copy_key: str | None = None
        self._engine_last_phase = "idle_monitoring"
        self._engine_last_start_attempt = 0.0
        self._monitoring_consecutive_failures = 0
        self._monitoring_streaming_set: tuple[str, ...] = ()
        self._monitoring_streaming_since = 0.0
        self._engine_stopped_by_operator = False
        self._audio_player = None  # lazy AudioAlertPlayer (QtMultimedia optional)
        self._periodic_lpr_threads: list[QThread] = []
        self._periodic_lpr_workers: list[PeriodicFrontLprWorker] = []
        self._periodic_lpr_active = False
        self._periodic_lpr_inflight = False
        self._periodic_lpr_last_sent = 0.0
        self.process_settings_inputs: dict[str, QWidget] = {}
        if self.process_engine is not None and settings is not None:
            for camera in settings.active_cameras:
                self.process_engine.observe_camera_health(camera.id, camera.role, False)
        self.clock_label = QLabel()
        self.setWindowTitle("TowerSightAI Operator Console")
        self.setStyleSheet(_stylesheet())
        self.ld2410_frame_ready.connect(self._append_ld2410_frame)
        self.ld2410_status_ready.connect(self._apply_ld2410_status)
        self._build()
        self.apply_model(model)
        if self.settings is not None:
            self._start_camera_capture()
            self._start_raw_data_collection()
            self._start_hailo_health_monitor()
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
        self._stop_purpose_inference()
        self._stop_front_camera_lpr()
        self._shutdown_periodic_lpr()
        self._stop_nas_connection_test()
        self._stop_nas_file_transfer()
        self._stop_calibration_session()
        self._stop_hailo_health_monitor()
        for thread in self._purpose_threads:
            thread.quit()
            thread.wait(5000)
        for thread in self._front_lpr_threads:
            thread.quit()
            thread.wait(10000)
        for thread in self._nas_test_threads:
            thread.quit()
            thread.wait(10000)
        for thread in self._nas_transfer_threads:
            thread.quit()
            thread.wait(10000)
        if self._calib_detect_thread is not None:
            self._calib_detect_thread.quit()
            self._calib_detect_thread.wait(5000)
        for thread in self._calib_threads:
            thread.quit()
            thread.wait(30000)
        for thread in self._system_test_threads:
            thread.quit()
            thread.wait(35000)
        for thread in self._hailo_health_threads:
            thread.quit()
            thread.wait(15000)
        if self._ld2410_service is not None:
            self._ld2410_service.stop()
            self._ld2410_service = None
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
            if self.settings.ld2410.enabled:
                self._ld2410_service = LD2410TCPService(self.settings.ld2410)
            self._raw_data_manager = RawDataManager(
                self.settings.raw_storage,
                (camera.id for camera in self.settings.active_cameras),
                ld2410_snapshot_provider=(
                    self._ld2410_service.snapshot_at if self._ld2410_service is not None else None
                ),
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
                    "ld2410_tcp_enabled": self.settings.ld2410.enabled,
                }
            )
            if self._ld2410_service is not None:
                self._ld2410_service.set_status_callback(self._record_ld2410_status)
                self._ld2410_service.set_frame_callback(self._receive_ld2410_frame)
                try:
                    self._ld2410_service.start()
                except Exception as exc:  # noqa: BLE001 - raw-only sensor failure must not stop the safety UI.
                    logging.getLogger(__name__).exception("LD2410 TCP server startup failed")
                    self._record_ld2410_status(
                        "error",
                        {"reason": type(exc).__name__, "phase": "startup"},
                    )
            self._raw_data_manager.start_background_sync()
        except Exception:  # noqa: BLE001 - raw logging must not crash the safety UI.
            logging.getLogger(__name__).exception("raw-data collection startup failed")
            if self._evidence_coordinator is not None:
                self._evidence_coordinator.close()
                self._evidence_coordinator = None
            if self._ld2410_service is not None:
                self._ld2410_service.stop()
                self._ld2410_service = None
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
        manager = self._raw_data_manager
        if manager is not None and getattr(manager, "_closed", False):
            # Late worker signals during shutdown: the audit file is already sealed.
            return
        try:
            callback(*args, **kwargs)
        except Exception:  # noqa: BLE001 - persistence failure must be visible in logs, not crash UI.
            logging.getLogger(__name__).exception("raw-data record failed")

    def _record_ld2410_status(self, state: str, details) -> None:  # noqa: ANN001 - worker callback boundary.
        if self._raw_data_manager is not None:
            self._record_raw(self._raw_data_manager.record_ld2410_status, state, details)
            close_window = getattr(self._raw_data_manager, "close_radar_window", None)
            if state == "stopped" and close_window is not None:
                # Analysis-only: the radar window must not stay open forever without its source.
                self._record_raw(close_window, reason="service_stopped")
        self.ld2410_status_ready.emit(state, dict(details))

    def _receive_ld2410_frame(self, frame: LD2410Frame, client_ip: str) -> None:
        self.ld2410_frame_ready.emit(frame, client_ip)

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
        if self.process_engine is not None:
            self.process_engine.observe_camera_health(
                camera_id, tile.role, status.startswith("정상 수신")
            )
        self._refresh_runtime_health_labels()
        self._refresh_driver_display()

    def _set_camera_detections(self, camera_id: str, detections: tuple[DetectionEvent, ...]) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        if self._raw_data_manager is not None and detections:
            task_id = self._purpose_task_id or "unknown"
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
        if self._purpose_task_enabled:
            self.ai_detection_label.setText(
                _purpose_detection_label(
                    self._purpose_task_label,
                    self._detection_camera_ids,
                    self._detection_event_counts,
                    first_inference_seconds=self._purpose_task_first_inference_seconds,
                )
            )
        if self.process_engine is not None and self._purpose_task_id == PURPOSE_PROCESS_MONITORING:
            self.process_engine.observe_detections(
                camera_id, tile.role, detections, datetime.now(timezone.utc)
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
            if preserve_notice:
                return
            warning = "카메라 입력 차단: " + ", ".join(tile.title for tile in blocked_tiles)
            if not self.model.birdview_available:
                warning = f"{warning} · 버드뷰 OFF · 최종 OK 차단"
            self.warning_label.setText(warning)
            return

        self.camera_summary_label.setText(f"카메라 {camera_total}/{camera_total} 정상{birdview_suffix}")
        if preserve_notice:
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
            copy_key=self._engine_copy_key if self._engine_owns_display else None,
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

        # Shared camera grid. Exactly one workspace page hosts it at a time; page
        # activation adopts it with the layout mode that fits the page's purpose.
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
        self.ld2410_console_view = self._build_ld2410_console_view()

        self.operator_workspace_stack = QStackedWidget()
        self.operator_pages: dict[str, QWidget] = {}
        self._camera_page_layouts: dict[str, str] = {}
        self._register_operator_page("전체 카메라", self._build_cameras_page(), camera_layout="all")
        self._register_operator_page("감시 설정", self._build_process_settings_page(), camera_layout="front")
        self._register_operator_page("차량 감지", self._build_vehicle_page(), camera_layout="front")
        self._register_operator_page("사람 감지", self._build_person_page(), camera_layout="all")
        self._register_operator_page("번호판 인식", self._build_lpr_page(), camera_layout="front")
        self._register_operator_page("레이더 (LD2410)", self.ld2410_console_view)
        self._register_operator_page("NAS 연결 확인", self._build_nas_page())
        self._register_operator_page("NAS 파일 전송", self._build_nas_transfer_page())
        self._register_operator_page("카메라 캘리브레이션", self._build_calibration_page(), camera_layout="single:front")
        self._register_operator_page("지면 기준점", self._build_ground_page(), camera_layout="single:front")
        self._register_operator_page("시스템 점검", self._build_system_page())
        self._register_operator_page("실행 로그", self._build_log_page())
        self._register_operator_page("주차 프로세스 테스트", self._build_driver_test_page())
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
        self.hailo_status_label = QLabel("HAILO 확인 중")
        self.evidence_status_label = QLabel("증거 OFF")
        self.process_status_label = QLabel("프로세스 대기")
        for label in (self.state_label, self.plc_label, self.camera_summary_label, self.model_status_label, self.ai_detection_label, self.hailo_status_label, self.process_status_label, self.evidence_status_label, self.clock_label):
            label.setObjectName("telemetryLabel")
            # Runtime inference text can become very long. Ignore its natural
            # width so the hidden operator page cannot enlarge the shared
            # stacked window after returning to the driver display.
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            status_layout.addWidget(label)
        main_layout.addWidget(status_bar)

        # The sidebar navigates to pages, so it is built after they are registered.
        outer.insertWidget(0, self._build_operator_sidebar())
        self._show_operator_page("전체 카메라")
        return root

    def _build_operator_sidebar(self) -> QWidget:
        self.operator_sidebar = QWidget()
        self.operator_sidebar.setObjectName("sidePanel")
        self.operator_sidebar.setFixedWidth(OPERATOR_SIDEBAR_WIDTH)
        self.operator_sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        sidebar_frame = QVBoxLayout(self.operator_sidebar)
        sidebar_frame.setContentsMargins(14, 14, 14, 14)
        sidebar_frame.setSpacing(8)
        title = QLabel("운영 메뉴")
        title.setObjectName("testTitleLabel")
        sidebar_frame.addWidget(title)

        # The menu grows as features land. Keep it scrollable so a short display can
        # never hide an operator action below the sidebar edge.
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("sidebarScroll")
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_content = QWidget()
        sidebar_content.setObjectName("sidebarScrollContent")
        sidebar_layout = QVBoxLayout(sidebar_content)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(6)
        self.sidebar_scroll.setWidget(sidebar_content)
        sidebar_frame.addWidget(self.sidebar_scroll, 1)
        self._add_sidebar_buttons(sidebar_layout)
        sidebar_layout.addStretch(1)
        self.operator_sidebar.setVisible(False)
        return self.operator_sidebar

    def _register_operator_page(self, label: str, page: QWidget, *, camera_layout: str | None = None) -> None:
        self.operator_pages[label] = page
        if camera_layout is not None:
            self._camera_page_layouts[label] = camera_layout
        self.operator_workspace_stack.addWidget(page)

    @staticmethod
    def _page_scaffold(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout, QHBoxLayout]:
        """Common page shell: a proposal-B panel with title row, control-bar row, and body."""
        page = QWidget()
        page.setObjectName("operatorPage")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body = QVBoxLayout(page)
        body.setContentsMargins(16, 14, 16, 16)
        body.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitleLabel")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("pageSubtitleLabel")
        subtitle_label.setWordWrap(True)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(title_label)
        head.addWidget(subtitle_label, 1)
        body.addLayout(head)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        body.addLayout(controls)
        return page, body, controls

    def _build_cameras_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "전체 카메라", "활성 카메라 실시간 스트리밍 · 최종 OK는 항상 차단됩니다."
        )
        # Same two tasks as their own pages; starting one stops whatever inference is running
        # (a manual task or the automatic process monitoring) and then launches the clicked one.
        for task_id in (PURPOSE_PERSON_PRESENCE, PURPOSE_VEHICLE_DETECTION):
            button = QPushButton(f"{PURPOSE_TASK_SPECS[task_id].label} 시작")
            button.setProperty("primary", "true")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, task_id=task_id: self._toggle_purpose_inference(task_id))
            self.purpose_task_extra_buttons.setdefault(task_id, []).append(button)
            controls.addWidget(button)
        controls.addStretch(1)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        return page

    def _build_vehicle_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "차량 감지", "front 카메라에서 Hailo 검출 모델을 실행하고 차량 라벨만 표시합니다."
        )
        button = QPushButton("차량 감지 시작")
        button.setProperty("primary", "true")
        button.setCheckable(True)
        self.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION] = button
        button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_VEHICLE_DETECTION))
        controls.addWidget(button)
        controls.addStretch(1)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        return page

    def _build_person_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "사람 감지", "정상 수신 중인 카메라에서 person 라벨만 판단합니다. 감지 박스는 각 카메라 타일에 표시됩니다."
        )
        button = QPushButton("사람 감지 시작")
        button.setProperty("primary", "true")
        button.setCheckable(True)
        self.purpose_task_buttons[PURPOSE_PERSON_PRESENCE] = button
        button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_PERSON_PRESENCE))
        controls.addWidget(button)
        controls.addStretch(1)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        return page

    def _build_lpr_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "번호판 인식", "FastALPR(CPU)로 번호판을 인식합니다. 결과는 상단 안내와 아래 결과줄에 표시됩니다."
        )
        self.front_lpr_button = QPushButton("정면 카메라 인식")
        self.front_lpr_button.setProperty("primary", "true")
        self.front_lpr_button.setCheckable(True)
        self.front_lpr_button.clicked.connect(self._toggle_front_camera_lpr)
        controls.addWidget(self.front_lpr_button)
        image_button = QPushButton("번호판 이미지 인식 시작")
        image_button.setProperty("primary", "true")
        image_button.setCheckable(True)
        self.purpose_task_buttons[PURPOSE_LPR_IMAGE] = image_button
        image_button.clicked.connect(lambda _checked=False: self._toggle_purpose_inference(PURPOSE_LPR_IMAGE))
        controls.addWidget(image_button)
        controls.addStretch(1)
        self.lpr_result_label = QLabel("LPR 결과 없음")
        self.lpr_result_label.setObjectName("pageStatusLabel")
        self.lpr_result_label.setWordWrap(True)
        body.addWidget(self.lpr_result_label)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        return page

    def _build_nas_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "NAS 연결 확인",
            "connectiontest 폴더에 검증 페이로드를 기록하고 SHA-256으로 재확인합니다. 카메라 수신 중이면 2초 영상을 함께 올립니다.",
        )
        self.nas_test_button = QPushButton("NAS 연결 확인 실행")
        self.nas_test_button.setProperty("primary", "true")
        self.nas_test_button.clicked.connect(self._start_nas_connection_test)
        controls.addWidget(self.nas_test_button)
        controls.addStretch(1)
        self.nas_result_label = QLabel("아직 실행하지 않았습니다.")
        self.nas_result_label.setObjectName("pageStatusLabel")
        self.nas_result_label.setWordWrap(True)
        body.addWidget(self.nas_result_label)
        self.nas_history = QPlainTextEdit()
        self.nas_history.setObjectName("testLog")
        self.nas_history.setReadOnly(True)
        self.nas_history.setPlaceholderText("실행 이력이 여기에 기록됩니다. 진단 전용이며 최종 OK를 허용하지 않습니다.")
        body.addWidget(self.nas_history, 1)
        return page

    def _build_nas_transfer_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "NAS 파일 전송",
            "선택한 파일을 NAS의 transfer 폴더 한 곳에 저장합니다. 원격 접속으로 파일을 주고받을 수 없을 때 NAS를 중계로 씁니다.",
        )
        self.nas_transfer_pick_button = QPushButton("파일 선택")
        self.nas_transfer_pick_button.clicked.connect(self._pick_nas_transfer_files)
        controls.addWidget(self.nas_transfer_pick_button)
        self.nas_transfer_clear_button = QPushButton("목록 비우기")
        self.nas_transfer_clear_button.clicked.connect(self._clear_nas_transfer_files)
        controls.addWidget(self.nas_transfer_clear_button)
        self.nas_transfer_send_button = QPushButton("NAS로 보내기")
        self.nas_transfer_send_button.setProperty("primary", "true")
        self.nas_transfer_send_button.clicked.connect(self._start_nas_file_transfer)
        controls.addWidget(self.nas_transfer_send_button)
        controls.addStretch(1)
        self.nas_transfer_target_label = QLabel(self._nas_transfer_target_text())
        self.nas_transfer_target_label.setObjectName("pageSubtitleLabel")
        self.nas_transfer_target_label.setWordWrap(True)
        body.addWidget(self.nas_transfer_target_label)
        self.nas_transfer_list = QListWidget()
        self.nas_transfer_list.setObjectName("testLog")
        body.addWidget(self.nas_transfer_list, 1)
        self.nas_transfer_result_label = QLabel("보낼 파일을 선택해 주세요.")
        self.nas_transfer_result_label.setObjectName("pageStatusLabel")
        self.nas_transfer_result_label.setWordWrap(True)
        body.addWidget(self.nas_transfer_result_label)
        self.nas_transfer_history = QPlainTextEdit()
        self.nas_transfer_history.setObjectName("testLog")
        self.nas_transfer_history.setReadOnly(True)
        self.nas_transfer_history.setPlaceholderText("전송 이력이 여기에 기록됩니다. 파일 중계 전용이며 최종 OK를 허용하지 않습니다.")
        body.addWidget(self.nas_transfer_history, 1)
        self._refresh_nas_transfer_buttons()
        return page

    def _build_calibration_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "카메라 캘리브레이션",
            "체커보드를 지시대로 들면 자동으로 촬영해 렌즈 내부 파라미터(초점거리·왜곡)를 측정합니다. "
            "결과는 측정 파일일 뿐이며 검토 전까지 캘리브레이션 유효로 취급되지 않습니다.",
        )
        self.calibration_camera_box = QComboBox()
        self.calibration_camera_box.currentIndexChanged.connect(self._on_calibration_camera_changed)
        controls.addWidget(QLabel("카메라"))
        controls.addWidget(self.calibration_camera_box)
        self.calibration_start_button = QPushButton("촬영 시작")
        self.calibration_start_button.setProperty("primary", "true")
        self.calibration_start_button.clicked.connect(self._start_calibration_session)
        controls.addWidget(self.calibration_start_button)
        self.calibration_capture_button = QPushButton("지금 촬영")
        self.calibration_capture_button.clicked.connect(self._request_calibration_capture)
        controls.addWidget(self.calibration_capture_button)
        self.calibration_skip_button = QPushButton("이 자세 건너뛰기")
        self.calibration_skip_button.clicked.connect(self._skip_calibration_pose)
        controls.addWidget(self.calibration_skip_button)
        self.calibration_run_button = QPushButton("측정 실행")
        self.calibration_run_button.setProperty("primary", "true")
        self.calibration_run_button.clicked.connect(self._run_calibration)
        controls.addWidget(self.calibration_run_button)
        self.calibration_stop_button = QPushButton("세션 종료")
        self.calibration_stop_button.clicked.connect(self._stop_calibration_session)
        controls.addWidget(self.calibration_stop_button)
        self.calibration_share_button = QPushButton("NAS로 공유")
        self.calibration_share_button.setToolTip(
            "이 장비에서 측정한 렌즈·지면 값을 NAS에 올려 현장 장비가 가져갈 수 있게 합니다. 측정 파일 전송일 뿐입니다."
        )
        self.calibration_share_button.clicked.connect(self._publish_calibration)
        controls.addWidget(self.calibration_share_button)
        self.calibration_fetch_button = QPushButton("NAS에서 가져오기")
        self.calibration_fetch_button.setToolTip(
            "다른 장비가 올린 측정값을 내려받습니다. 체커보드를 들 수 없는 현장 장비에서 씁니다."
        )
        self.calibration_fetch_button.clicked.connect(self._fetch_calibration)
        controls.addWidget(self.calibration_fetch_button)
        self.calibration_verify_button = QPushButton("결과 확인")
        self.calibration_verify_button.setToolTip(
            "저장된 측정값으로 현재 화면의 왜곡을 보정해 원본과 나란히 보여줍니다. 확인 전용이며 최종 OK와 무관합니다."
        )
        self.calibration_verify_button.clicked.connect(self._verify_calibration)
        controls.addWidget(self.calibration_verify_button)
        self.calibration_auto_box = QCheckBox("자동 촬영")
        self.calibration_auto_box.setChecked(True)
        controls.addWidget(self.calibration_auto_box)
        controls.addStretch(1)
        cols, rows = self._calib_spec.inner_corners
        board = QLabel(
            f"체커보드: {self._calib_spec.columns}x{self._calib_spec.rows}칸 {self._calib_spec.square_mm:g}mm "
            f"(내부 코너 {cols}x{rows}) · 인쇄: data/calibration/checkerboard/ · 필요 샘플 {CALIBRATION_MIN_SAMPLES}장 이상"
        )
        board.setObjectName("pageSubtitleLabel")
        board.setWordWrap(True)
        body.addWidget(board)
        self.calibration_instruction_label = QLabel("촬영 시작을 누르면 첫 번째 자세를 안내합니다.")
        self.calibration_instruction_label.setObjectName("pageTitleLabel")
        self.calibration_instruction_label.setWordWrap(True)
        body.addWidget(self.calibration_instruction_label)
        self.calibration_status_label = QLabel("대기 중")
        self.calibration_status_label.setObjectName("pageStatusLabel")
        self.calibration_status_label.setWordWrap(True)
        body.addWidget(self.calibration_status_label)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        self.calibration_quality_label = QLabel("측정 결과가 아직 없습니다. 촬영 후 측정 실행을 누르거나 결과 확인을 눌러 주세요.")
        self.calibration_quality_label.setObjectName("pageSubtitleLabel")
        self.calibration_quality_label.setWordWrap(True)
        body.addWidget(self.calibration_quality_label)
        self.calibration_verify_view = QLabel()
        self.calibration_verify_view.setObjectName("calibrationVerifyView")
        self.calibration_verify_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.calibration_verify_view.setMinimumHeight(0)
        self.calibration_verify_view.setVisible(False)
        body.addWidget(self.calibration_verify_view)
        self.calibration_verify_caption = QLabel(
            "왼쪽: 원본 · 오른쪽: 왜곡 보정 후. 노란 격자는 완벽한 직선입니다 — "
            "보정 후 화면의 실제 직선(레일·기둥·바닥 이음매)이 격자와 나란하면 잘 된 것입니다."
        )
        self.calibration_verify_caption.setObjectName("pageSubtitleLabel")
        self.calibration_verify_caption.setWordWrap(True)
        self.calibration_verify_caption.setVisible(False)
        body.addWidget(self.calibration_verify_caption)
        self.calibration_history = QPlainTextEdit()
        self.calibration_history.setObjectName("testLog")
        self.calibration_history.setReadOnly(True)
        self.calibration_history.setMaximumHeight(140)
        self.calibration_history.setPlaceholderText("촬영·측정 이력이 여기에 기록됩니다. 측정 결과는 최종 OK를 허용하지 않습니다.")
        body.addWidget(self.calibration_history)
        self._populate_calibration_cameras()
        self._refresh_calibration_buttons()
        return page


    # ---- 지면 기준점 (외부 파라미터) -----------------------------------------------------
    #
    # 렌즈 내부 파라미터(체커보드)만으로는 mm를 못 만든다. 카메라가 팔레트를 기준으로 어디에
    # 어느 각도로 있는지가 있어야 한다. 팔레트 데크의 끝단에는 자동으로 잡을 만한 특징이 없어서
    # (긴 변만 노란 테두리, 끝은 주황 스토퍼 프레임) 운영자가 네 모서리를 찍는다.

    GROUND_PICK_STEPS = (
        "팔레트 데크의 **네 모서리**를 차례로 클릭하세요 (순서는 상관없습니다)",
        "마지막으로 **주황색 스토퍼 프레임의 바닥**을 클릭하세요 — 이 쪽이 주차기 안쪽입니다",
    )

    def _build_ground_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "지면 기준점",
            "팔레트 데크 네 모서리와 주황 스토퍼 위치를 찍으면 카메라가 팔레트 기준으로 어디에 "
            "어느 각도로 있는지를 계산합니다. 측정 파일일 뿐이며 검토 전까지 캘리브레이션 유효로 "
            "취급되지 않습니다.",
        )
        self.ground_camera_box = QComboBox()
        self.ground_camera_box.currentIndexChanged.connect(self._on_ground_camera_changed)
        controls.addWidget(QLabel("카메라"))
        controls.addWidget(self.ground_camera_box)
        self.ground_start_button = QPushButton("찍기 시작")
        self.ground_start_button.setProperty("primary", "true")
        self.ground_start_button.clicked.connect(self._start_ground_picking)
        controls.addWidget(self.ground_start_button)
        self.ground_undo_button = QPushButton("한 점 취소")
        self.ground_undo_button.clicked.connect(self._undo_ground_point)
        controls.addWidget(self.ground_undo_button)
        self.ground_reset_button = QPushButton("다시 찍기")
        self.ground_reset_button.clicked.connect(self._reset_ground_points)
        controls.addWidget(self.ground_reset_button)
        self.ground_save_button = QPushButton("저장")
        self.ground_save_button.setProperty("primary", "true")
        self.ground_save_button.clicked.connect(self._save_ground_pose)
        controls.addWidget(self.ground_save_button)
        controls.addStretch(1)

        self.ground_instruction_label = QLabel("찍기 시작을 누르면 안내가 나옵니다.")
        self.ground_instruction_label.setObjectName("pageTitleLabel")
        self.ground_instruction_label.setWordWrap(True)
        body.addWidget(self.ground_instruction_label)
        self.ground_status_label = QLabel("대기 중")
        self.ground_status_label.setObjectName("pageStatusLabel")
        self.ground_status_label.setWordWrap(True)
        body.addWidget(self.ground_status_label)
        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        self.ground_quality_label = QLabel(
            f"팔레트 {self._ground_pallet()[0]:.0f}×{self._ground_pallet()[1]:.0f} mm 기준 · "
            "저장된 기준점이 없습니다."
        )
        self.ground_quality_label.setObjectName("pageSubtitleLabel")
        self.ground_quality_label.setWordWrap(True)
        body.addWidget(self.ground_quality_label)
        self.ground_history = QPlainTextEdit()
        self.ground_history.setObjectName("testLog")
        self.ground_history.setReadOnly(True)
        self.ground_history.setMaximumHeight(120)
        self.ground_history.setPlaceholderText("기준점 기록이 여기에 남습니다. 측정 전용이며 최종 OK를 허용하지 않습니다.")
        body.addWidget(self.ground_history)
        self._populate_ground_cameras()
        self._refresh_ground_buttons()
        return page

    def _refresh_ground_page(self) -> None:
        """Show the selected camera's tile and whatever was measured for it before."""
        camera = self._ground_camera()
        page = self.operator_pages.get("지면 기준점")
        if camera is not None:
            self._camera_page_layouts["지면 기준점"] = f"single:{camera.role.value}"
        if page is not None:
            self._adopt_camera_area(page, self._camera_page_layouts["지면 기준점"])
        if camera is not None:
            self._show_ground_saved(camera.id)
        self._refresh_ground_buttons()

    def _ground_pallet(self) -> tuple[float, float]:
        envelope = self.settings.vehicle_envelope if self.settings is not None else None
        if envelope is None:
            return (5350.0, 2200.0)
        return (float(envelope.pallet_length_mm), float(envelope.pallet_width_mm))

    def _populate_ground_cameras(self) -> None:
        box = getattr(self, "ground_camera_box", None)
        if box is None or self.settings is None:
            return
        box.blockSignals(True)
        box.clear()
        for camera in self.settings.active_cameras:
            box.addItem(f"{camera.id} ({camera.role.value})", camera.id)
        # Start on the same camera the calibration page prefers, so the two steps line up.
        preferred = self.settings.vehicle_envelope.front_left_role
        for camera in self.settings.active_cameras:
            if camera.role is preferred:
                index = box.findData(camera.id)
                if index >= 0:
                    box.setCurrentIndex(index)
                break
        box.blockSignals(False)
        camera = self._ground_camera()
        if camera is not None:
            self._camera_page_layouts["지면 기준점"] = f"single:{camera.role.value}"

    def _ground_camera(self):  # noqa: ANN201 - CameraConfig | None
        box = getattr(self, "ground_camera_box", None)
        if box is None or self.settings is None:
            return None
        camera_id = box.currentData()
        for camera in self.settings.active_cameras:
            if camera.id == camera_id:
                return camera
        return None

    def _ground_camera_widget(self):  # noqa: ANN201
        camera = self._ground_camera()
        return self.camera_widgets.get(camera.role) if camera is not None else None

    def _on_ground_camera_changed(self, index: int = -1) -> None:
        del index
        self._reset_ground_points()
        camera = self._ground_camera()
        if camera is not None:
            self._camera_page_layouts["지면 기준점"] = f"single:{camera.role.value}"
            page = self.operator_pages.get("지면 기준점")
            if page is not None and self.operator_workspace_stack.currentWidget() is page:
                self._adopt_camera_area(page, self._camera_page_layouts["지면 기준점"])
            self._show_ground_saved(camera.id)

    def _refresh_ground_buttons(self) -> None:
        picking = bool(self._ground_picking)
        points = len(self._ground_points)
        for name, enabled in (
            ("ground_start_button", not picking),
            ("ground_undo_button", picking and points > 0),
            ("ground_reset_button", points > 0),
            ("ground_save_button", self._ground_result is not None),
            ("ground_camera_box", not picking),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(enabled)

    def _set_ground_status(self, message: str) -> None:
        if hasattr(self, "ground_status_label"):
            self.ground_status_label.setText(message)
        self.warning_label.setText(f"{message}. 측정 전용이며 최종 OK는 차단됩니다.")

    def _log_ground(self, message: str) -> None:
        if hasattr(self, "ground_history"):
            self.ground_history.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _show_ground_instruction(self) -> None:
        label = getattr(self, "ground_instruction_label", None)
        if label is None:
            return
        count = len(self._ground_points)
        if not self._ground_picking:
            label.setText("찍기 시작을 누르면 안내가 나옵니다.")
        elif count < REQUIRED_CORNERS:
            label.setText(f"[{count + 1}/{REQUIRED_CORNERS + 1}] {self.GROUND_PICK_STEPS[0]}")
        elif count == REQUIRED_CORNERS:
            label.setText(f"[{REQUIRED_CORNERS + 1}/{REQUIRED_CORNERS + 1}] {self.GROUND_PICK_STEPS[1]}")
        else:
            label.setText("계산 완료. 투영된 팔레트와 격자가 실제와 맞는지 보고 저장하세요.")

    def _start_ground_picking(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked:
            return
        camera = self._ground_camera()
        widget = self._ground_camera_widget()
        if camera is None or widget is None:
            self._set_ground_status("설정된 카메라가 없습니다")
            return
        if widget.current_frame() is None:
            self._set_ground_status(f"{camera.id} 화면이 아직 수신되지 않았습니다")
            return
        self._reset_ground_points()
        self._ground_picking = True
        widget.set_picking(True)
        try:
            widget.picked.disconnect(self._on_ground_point_picked)
        except TypeError:
            pass
        widget.picked.connect(self._on_ground_point_picked)
        self._show_ground_instruction()
        self._set_ground_status(f"{camera.id} 화면을 클릭해 기준점을 찍으세요")
        self._log_ground(f"찍기 시작 camera={camera.id}")
        self._refresh_ground_buttons()

    def _stop_ground_picking(self) -> None:
        self._ground_picking = False
        widget = self._ground_camera_widget()
        if widget is not None:
            widget.set_picking(False)
            try:
                widget.picked.disconnect(self._on_ground_point_picked)
            except TypeError:
                pass

    def _ground_marker_labels(self) -> list[tuple[float, float, str]]:
        markers: list[tuple[float, float, str]] = []
        for index, (x, y) in enumerate(self._ground_points):
            label = f"모서리 {index + 1}" if index < REQUIRED_CORNERS else "스토퍼"
            markers.append((x, y, label))
        return markers

    def _on_ground_point_picked(self, x: float, y: float) -> None:
        if not self._ground_picking:
            return
        self._ground_points.append((float(x), float(y)))
        widget = self._ground_camera_widget()
        if widget is not None:
            widget.set_pick_markers(self._ground_marker_labels())
        if len(self._ground_points) > REQUIRED_CORNERS:
            self._solve_ground_pose()
        else:
            self._show_ground_instruction()
            self._set_ground_status(f"{len(self._ground_points)}개 찍음")
        self._refresh_ground_buttons()

    def _undo_ground_point(self, checked: bool = False) -> None:
        del checked
        if not self._ground_points:
            return
        self._ground_points.pop()
        self._ground_result = None
        widget = self._ground_camera_widget()
        if widget is not None:
            widget.set_pick_markers(self._ground_marker_labels())
            widget.set_ground_overlay(())
        self._show_ground_instruction()
        self._set_ground_status(f"{len(self._ground_points)}개 남음")
        self._refresh_ground_buttons()

    def _reset_ground_points(self, checked: bool = False) -> None:
        del checked
        self._ground_points = []
        self._ground_result = None
        self._stop_ground_picking()
        widget = self._ground_camera_widget()
        if widget is not None:
            widget.set_pick_markers(())
            widget.set_ground_overlay(())
        self._show_ground_instruction()
        self._refresh_ground_buttons()

    def _ground_intrinsics(self, camera_id: str):  # noqa: ANN201
        """(IntrinsicsResult, source_camera_id, borrowed) — 없으면 같은 기종의 다른 측정값을 빌린다."""
        root = (
            self.settings.calibration_path.parent / "intrinsics"
            if self.settings is not None
            else DEFAULT_INTRINSICS_ROOT
        )
        host = socket.gethostname()
        own = Path(root) / f"{camera_id}.json"
        if own.is_file():
            try:
                result = result_from_dict(load_intrinsics(own))
            except (OSError, ValueError, KeyError):
                result = None
            if result is not None:
                # A file shared from another machine keeps its original source_host. Saying
                # "자체 측정값" for it would hide that it came from a different camera unit.
                if result.source_host and result.source_host != host:
                    return result, result.source_host, True
                return result, camera_id, False
        for path in sorted(Path(root).glob("*.json")):
            try:
                result = result_from_dict(load_intrinsics(path))
            except (OSError, ValueError, KeyError):
                continue
            origin = path.stem
            if result.source_host and result.source_host != host:
                origin = f"{path.stem}@{result.source_host}"
            return result, origin, True
        return None, "", False

    def _solve_ground_pose(self) -> None:
        camera = self._ground_camera()
        widget = self._ground_camera_widget()
        frame = widget.current_frame() if widget is not None else None
        if camera is None or frame is None:
            self._set_ground_status("화면이 없어 계산할 수 없습니다")
            return
        intrinsics, source, borrowed = self._ground_intrinsics(camera.id)
        if intrinsics is None:
            self._set_ground_status(
                "렌즈 내부 파라미터가 없습니다. 카메라 캘리브레이션에서 체커보드를 측정하거나, "
                "그 페이지의 'NAS에서 가져오기'로 다른 장비 측정값을 받아 오세요"
            )
            self._stop_ground_picking()
            return

        size = (frame.width(), frame.height())
        matrix = scale_camera_matrix(intrinsics.camera_matrix, (intrinsics.image_width, intrinsics.image_height), size)
        length_mm, width_mm = self._ground_pallet()
        try:
            result = solve_ground_pose(
                self._ground_points[:REQUIRED_CORNERS],
                self._ground_points[REQUIRED_CORNERS],
                camera_id=camera.id,
                camera_matrix=matrix,
                distortion=intrinsics.distortion,
                image_size=size,
                pallet_length_mm=length_mm,
                pallet_width_mm=width_mm,
                rotation_degrees=camera.rotation_degrees,
                intrinsics_camera_id=source,
                intrinsics_borrowed=borrowed,
            )
        except (ValueError, ImportError) as exc:
            self._set_ground_status(f"계산 실패: {exc}")
            self._log_ground(f"계산 실패 {exc}")
            self._stop_ground_picking()
            self._refresh_ground_buttons()
            return

        self._ground_result = result
        self._stop_ground_picking()
        self._show_ground_overlay(result, intrinsics, size)
        self._show_ground_quality(result)
        self._show_ground_instruction()
        self._set_ground_status(f"{camera.id} 자세 계산 완료 — {result.summary()}")
        self._log_ground(f"계산 완료 {result.summary()}")
        self._refresh_ground_buttons()

    def _show_ground_overlay(self, result: GroundPoseResult, intrinsics, size: tuple[int, int]) -> None:
        widget = self._ground_camera_widget()
        if widget is None:
            return
        matrix = scale_camera_matrix(
            intrinsics.camera_matrix, (intrinsics.image_width, intrinsics.image_height), size
        )
        polylines: list[list[tuple[float, float]]] = []
        for line in pallet_grid(result):
            polylines.append(
                project_ground_points(
                    result, line, camera_matrix=matrix, distortion=intrinsics.distortion, image_size=size
                )
            )
        outline = project_ground_points(
            result, pallet_outline(result), camera_matrix=matrix, distortion=intrinsics.distortion, image_size=size
        )
        polylines.append(outline + [outline[0]])
        widget.set_ground_overlay(polylines)

    def _show_ground_quality(self, result: GroundPoseResult) -> None:
        label = getattr(self, "ground_quality_label", None)
        if label is None:
            return
        grade, lines = result.quality_report()
        head = f"[{self.CALIBRATION_GRADE_TEXT.get(grade, grade)}] {result.summary()}"
        label.setText(head + "\n" + "\n".join(lines))

    def _show_ground_saved(self, camera_id: str) -> None:
        label = getattr(self, "ground_quality_label", None)
        if label is None:
            return
        saved = self._ground_store().load(camera_id)
        length_mm, width_mm = self._ground_pallet()
        if saved is None:
            label.setText(f"팔레트 {length_mm:.0f}×{width_mm:.0f} mm 기준 · {camera_id} 저장된 기준점이 없습니다.")
            return
        self._show_ground_quality(saved)

    def _ground_store(self) -> GroundCalibrationStore:
        root = (
            self.settings.calibration_path.parent / "ground"
            if self.settings is not None
            else Path("data/calibration/ground")
        )
        return GroundCalibrationStore(root=Path(root))

    def _save_ground_pose(self, checked: bool = False) -> None:
        del checked
        if self._ground_result is None:
            return
        try:
            path = self._ground_store().save(self._ground_result)
        except OSError as exc:
            self._set_ground_status(f"저장 실패: {exc}")
            return
        self._set_ground_status(f"저장 완료: {path} (reviewed=false — 최종 OK는 계속 차단)")
        self._log_ground(f"저장 {path}")
        self._refresh_ground_buttons()

    def _build_system_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "시스템 점검", "설정·Hailo·카메라·PLC 진단을 실행합니다. 모든 결과는 safe_to_operate=False 입니다."
        )
        self.system_test_buttons: dict[str, QPushButton] = {}
        for test_id, label in (
            ("settings", "설정 검증"),
            ("hailo_installation", "Hailo 설치 점검"),
            ("hailo_image_smoke", "Hailo 샘플 이미지"),
            ("plc_simulator", "PLC 시뮬레이터"),
            ("full_hardware_smoke", "전체 스모크"),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, test_id=test_id: self._run_system_test(test_id))
            self.system_test_buttons[test_id] = button
            controls.addWidget(button)
        controls.addStretch(1)
        camera_controls = QHBoxLayout()
        camera_controls.setSpacing(8)
        for index, tile in enumerate(self.model.camera_tiles, start=1):
            button = QPushButton(f"카메라 {index} ({tile.title})")
            button.clicked.connect(lambda _checked=False, test_id=f"camera_{index}": self._run_system_test(test_id))
            self.system_test_buttons[f"camera_{index}"] = button
            camera_controls.addWidget(button)
        camera_controls.addStretch(1)
        body.addLayout(camera_controls)
        self.hailo_health_label = QLabel("Hailo 장치 상태: 수집 대기 중 (60초 주기 자동 갱신)")
        self.hailo_health_label.setObjectName("pageStatusLabel")
        self.hailo_health_label.setWordWrap(True)
        self.hailo_health_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.hailo_health_label)
        hailo_controls = QHBoxLayout()
        hailo_controls.setSpacing(8)
        self.hailo_holder_kill_button = QPushButton("고아 프로세스 종료")
        self.hailo_holder_kill_button.setProperty("danger", "true")
        self.hailo_holder_kill_button.setEnabled(False)
        self.hailo_holder_kill_button.setToolTip(
            "이 앱의 자식이 아닌 추론(Hailo)·증거 녹화(RTSP) 프로세스를 종료합니다. 죽은 UI가 남긴 고아를 정리합니다."
        )
        self.hailo_holder_kill_button.clicked.connect(self._terminate_hailo_holders)
        hailo_controls.addWidget(self.hailo_holder_kill_button)
        hailo_controls.addStretch(1)
        body.addLayout(hailo_controls)
        self.system_test_log = QPlainTextEdit()
        self.system_test_log.setObjectName("testLog")
        self.system_test_log.setReadOnly(True)
        self.system_test_log.setPlaceholderText("진단 결과가 여기에 기록됩니다. 통과해도 안전 승인이 아닙니다.")
        body.addWidget(self.system_test_log, 1)
        return page

    def _build_log_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "실행 로그", f"{DEFAULT_RUNTIME_LOG} 마지막 {LOG_VIEW_TAIL_BYTES // 1024}KB를 표시합니다. RTSP 자격증명은 기록 시 마스킹됩니다."
        )
        self.log_filter_input = QLineEdit()
        self.log_filter_input.setObjectName("logFilterInput")
        self.log_filter_input.setPlaceholderText("필터 (예: camera-capture, ERROR, nas)")
        self.log_filter_input.textChanged.connect(lambda _text="": self._refresh_log_view())
        controls.addWidget(self.log_filter_input, 1)
        self.log_follow_checkbox = QCheckBox("맨 아래 따라가기")
        self.log_follow_checkbox.setChecked(True)
        controls.addWidget(self.log_follow_checkbox)
        refresh = QPushButton("새로고침")
        refresh.setFixedWidth(110)
        refresh.clicked.connect(lambda _checked=False: self._refresh_log_view())
        controls.addWidget(refresh)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("testLog")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.document().setMaximumBlockCount(LOG_VIEW_MAX_LINES)
        self.log_view.setPlaceholderText("로그 파일이 아직 없습니다.")
        body.addWidget(self.log_view, 1)
        return page

    def _build_driver_test_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "주차 프로세스 테스트", "주차기 실행 프로세스를 단계별로 재현합니다. 모든 단계는 테스트 전용이며 PLC OK를 허용하지 않습니다."
        )
        self.driver_test_buttons: dict[str, QPushButton] = {}
        for label, handler in (
            ("실제 상태", self._clear_user_test_state),
            ("IDLE", self._user_idle),
            ("진입", self._user_entry),
            ("진입완료", self._user_entry_complete),
            ("번호판인식", self._user_plate_recognition),
            ("주차시작", self._user_parking_started),
        ):
            button = QPushButton(label)
            button.setObjectName("smallModeButton")
            button.clicked.connect(handler)
            self.driver_test_buttons[label] = button
            controls.addWidget(button)
        self.vehicle_sim_button = QPushButton("차량 진입 시뮬레이션")
        self.vehicle_sim_button.setObjectName("smallModeButton")
        self.vehicle_sim_button.clicked.connect(self._simulate_vehicle_entry)
        controls.addWidget(self.vehicle_sim_button)
        controls.addStretch(1)
        # Kept for compatibility with existing show/hide expectations.
        self.driver_test_panel = QFrame()
        self.driver_test_panel.setVisible(False)
        body.addWidget(self.driver_preview_host, 1)
        return page

    def _build_ld2410_console_view(self) -> QWidget:
        root = QWidget()
        root.setObjectName("ld2410ConsoleView")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("LD2410 RAW 모니터")
        title.setObjectName("testTitleLabel")
        header.addWidget(title)
        self.ld2410_connection_label = QLabel()
        self.ld2410_connection_label.setObjectName("ld2410ConnectionLabel")
        self.ld2410_connection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.ld2410_connection_label)
        header.addStretch(1)
        self.ld2410_endpoint_label = QLabel()
        self.ld2410_endpoint_label.setObjectName("telemetryLabel")
        header.addWidget(self.ld2410_endpoint_label)
        layout.addLayout(header)

        note = QLabel("0.5초 ESP32 수신값 표시 · RAW 기록 보조 전용 · AI/PLC 안전판정에는 사용하지 않음")
        note.setObjectName("ld2410SafetyNote")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.ld2410_console = QPlainTextEdit()
        self.ld2410_console.setObjectName("testLog")
        self.ld2410_console.setReadOnly(True)
        self.ld2410_console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.ld2410_console.document().setMaximumBlockCount(LD2410_CONSOLE_MAX_LINES)
        self.ld2410_console.setPlaceholderText("LD2410 프레임 수신을 기다리는 중입니다.")
        layout.addWidget(self.ld2410_console, 1)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.ld2410_pause_button = QPushButton("화면 일시정지")
        self.ld2410_pause_button.setObjectName("smallModeButton")
        self.ld2410_pause_button.clicked.connect(self._toggle_ld2410_console_pause)
        controls.addWidget(self.ld2410_pause_button)
        self.ld2410_clear_button = QPushButton("화면 지우기")
        self.ld2410_clear_button.setObjectName("smallModeButton")
        self.ld2410_clear_button.clicked.connect(self._clear_ld2410_console)
        controls.addWidget(self.ld2410_clear_button)
        layout.addLayout(controls)

        self._refresh_ld2410_console_status()
        return root

    def _add_sidebar_buttons(self, layout: QVBoxLayout) -> None:
        page_labels = set(self.operator_pages)
        for section_title, labels in SIDEBAR_SECTIONS:
            section = QLabel(section_title)
            section.setObjectName("sidebarSectionLabel")
            layout.addWidget(section)
            for label in labels:
                button = QPushButton(label)
                button.setObjectName("sidebarButton")
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                if label in page_labels:
                    button.setCheckable(True)
                    button.clicked.connect(lambda _checked=False, label=label: self._show_operator_page(label))
                elif label == "사용자 화면":
                    button.clicked.connect(self._show_user_mode)
                elif label == "카메라 설정":
                    button.clicked.connect(self._show_camera_settings)
                elif label == "프로그램 종료":
                    button.setProperty("danger", "true")
                    button.clicked.connect(self._request_shutdown)
                else:
                    raise ValueError(f"Unsupported sidebar action: {label}")
                self.sidebar_buttons[label] = button
                layout.addWidget(button)
        # Compatibility alias used by tests and driver-test flows.
        self.driver_test_toggle = self.sidebar_buttons["주차 프로세스 테스트"]

    def _build_process_settings_page(self) -> QWidget:
        page, body, controls = self._page_scaffold(
            "감시 설정",
            "주차 프로세스 엔진의 임계값·기준선·유도선·NAS 업로드 방식을 설정합니다. 저장 즉시 반영되며 최종 OK 조건은 바뀌지 않습니다.",
        )
        save_button = QPushButton("설정 저장")
        save_button.setProperty("primary", "true")
        save_button.clicked.connect(self._save_process_settings)
        controls.addWidget(save_button)
        revert_button = QPushButton("되돌리기")
        revert_button.clicked.connect(self._load_process_settings_inputs)
        controls.addWidget(revert_button)
        self.engine_toggle_button = QPushButton("프로세스 감시 일시중지")
        self.engine_toggle_button.setCheckable(True)
        self.engine_toggle_button.clicked.connect(self._toggle_process_engine)
        controls.addWidget(self.engine_toggle_button)
        controls.addStretch(1)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        def add_double(row: int, column: int, key: str, label: str, minimum: float, maximum: float, step: float) -> None:
            box = QDoubleSpinBox()
            box.setRange(minimum, maximum)
            box.setSingleStep(step)
            box.setDecimals(2)
            box.valueChanged.connect(self._update_settings_preview_overlays)
            self.process_settings_inputs[key] = box
            form.addWidget(QLabel(label), row, column * 2)
            form.addWidget(box, row, column * 2 + 1)

        def add_int(row: int, column: int, key: str, label: str, minimum: int, maximum: int) -> None:
            box = QSpinBox()
            box.setRange(minimum, maximum)
            self.process_settings_inputs[key] = box
            form.addWidget(QLabel(label), row, column * 2)
            form.addWidget(box, row, column * 2 + 1)

        add_double(0, 0, "vehicle_trigger.min_confidence", "차량 감지 임계값", 0.05, 1.0, 0.05)
        add_int(0, 1, "vehicle_trigger.consecutive_frames", "차량 연속 프레임 수", 1, 30)
        add_double(0, 2, "vehicle_trigger.release_seconds", "진입 해제(초)", 1.0, 60.0, 1.0)
        add_int(1, 0, "person_debounce.idle_frames", "사람 연속 감지(대기)", 1, 20)
        add_int(1, 1, "person_debounce.parked_frames", "사람 연속 감지(하차)", 1, 20)
        add_double(1, 2, "person_debounce.stale_seconds", "사람 해제 대기(초)", 0.5, 30.0, 0.5)
        add_double(2, 0, "plate_zone.line_y_norm", "차량진입선 위치", 0.0, 1.0, 0.01)
        add_int(2, 1, "plate_zone.min_reads_for_vote", "번호판 최소 인식 횟수", 1, 20)
        add_double(2, 2, "plate_zone.read_interval_seconds", "번호판 인식 주기(초)", 0.2, 10.0, 0.2)
        add_double(2, 3, "plate_zone.read_timeout_seconds", "번호판 판독 시간(초)", 5.0, 120.0, 1.0)
        add_double(6, 2, "plate_zone.min_read_seconds", "정차 후 최소 판독(초)", 0.0, 60.0, 1.0)
        add_double(7, 0, "vehicle_direction.exit_min_width_norm", "출고 판정 최소 폭", 0.1, 1.0, 0.01)
        add_double(7, 1, "vehicle_direction.exit_min_aspect", "출고 판정 가로세로비", 1.0, 10.0, 0.1)
        add_double(7, 2, "vehicle_direction.entry_max_aspect", "입고 판정 가로세로비 상한", 0.5, 5.0, 0.1)
        add_int(7, 3, "vehicle_direction.consecutive_frames", "방향 연속 프레임 수", 1, 30)
        add_double(7, 4, "vehicle_direction.classify_timeout_seconds", "방향 판별 한도(초)", 5.0, 120.0, 1.0)
        add_double(6, 3, "plate_zone.arrival_timeout_seconds", "전면 도착 대기 한도(초)", 5.0, 300.0, 5.0)
        add_double(3, 0, "wheel_guides.left_x_norm", "유도선 아래 왼쪽", 0.0, 1.0, 0.01)
        add_double(3, 1, "wheel_guides.right_x_norm", "유도선 아래 오른쪽", 0.0, 1.0, 0.01)
        add_double(3, 2, "wheel_guides.top_left_x_norm", "유도선 위 왼쪽", 0.0, 1.0, 0.01)
        add_double(4, 0, "wheel_guides.top_right_x_norm", "유도선 위 오른쪽", 0.0, 1.0, 0.01)
        add_double(4, 1, "wheel_guides.top_y_norm", "유도선 위 높이", 0.0, 1.0, 0.01)
        add_double(4, 2, "wheel_guides.stop_y_norm", "정지선 위치", 0.0, 1.0, 0.01)
        add_double(5, 0, "timers.exit_clear_seconds", "무인 확인 시간(초)", 1.0, 600.0, 1.0)
        add_double(5, 1, "timers.machine_operation_seconds", "주차기 작동 가정(초)", 5.0, 3600.0, 5.0)
        upload_box = QComboBox()
        upload_box.addItem("예약 (일 단위)", "scheduled")
        upload_box.addItem("즉시 (이벤트 종료 시)", "immediate")
        self.process_settings_inputs["nas_upload_mode"] = upload_box
        form.addWidget(QLabel("NAS 업로드 방식"), 5, 4)
        form.addWidget(upload_box, 5, 5)
        audio_box = QCheckBox("경고음 사용 (사람 감지 시)")
        self.process_settings_inputs["audio_enabled"] = audio_box
        form.addWidget(audio_box, 6, 0, 1, 2)
        body.addLayout(form)

        self.process_settings_status = QLabel(f"설정 파일: {OPERATOR_SETTINGS_PATH}")
        self.process_settings_status.setObjectName("pageSubtitleLabel")
        self.process_settings_status.setWordWrap(True)
        body.addWidget(self.process_settings_status)

        page.camera_slot = QVBoxLayout()  # type: ignore[attr-defined]
        page.camera_slot.setContentsMargins(0, 0, 0, 0)
        body.addLayout(page.camera_slot, 1)
        self._settings_preview_active = False
        self._load_process_settings_inputs()
        return page

    def _load_process_settings_inputs(self) -> None:
        settings = self.operator_settings
        values: dict[str, object] = {
            "vehicle_trigger.min_confidence": settings.vehicle_trigger.min_confidence,
            "vehicle_trigger.consecutive_frames": settings.vehicle_trigger.consecutive_frames,
            "vehicle_trigger.release_seconds": settings.vehicle_trigger.release_seconds,
            "person_debounce.idle_frames": settings.person_debounce.idle_frames,
            "person_debounce.parked_frames": settings.person_debounce.parked_frames,
            "person_debounce.stale_seconds": settings.person_debounce.stale_seconds,
            "plate_zone.line_y_norm": settings.plate_zone.line_y_norm,
            "plate_zone.min_reads_for_vote": settings.plate_zone.min_reads_for_vote,
            "plate_zone.read_timeout_seconds": settings.plate_zone.read_timeout_seconds,
            "plate_zone.min_read_seconds": settings.plate_zone.min_read_seconds,
            "vehicle_direction.exit_min_width_norm": settings.vehicle_direction.exit_min_width_norm,
            "vehicle_direction.exit_min_aspect": settings.vehicle_direction.exit_min_aspect,
            "vehicle_direction.entry_max_aspect": settings.vehicle_direction.entry_max_aspect,
            "vehicle_direction.consecutive_frames": settings.vehicle_direction.consecutive_frames,
            "vehicle_direction.classify_timeout_seconds": settings.vehicle_direction.classify_timeout_seconds,
            "plate_zone.arrival_timeout_seconds": settings.plate_zone.arrival_timeout_seconds,
            "plate_zone.read_interval_seconds": settings.plate_zone.read_interval_seconds,
            "wheel_guides.left_x_norm": settings.wheel_guides.left_x_norm,
            "wheel_guides.right_x_norm": settings.wheel_guides.right_x_norm,
            "wheel_guides.top_left_x_norm": settings.wheel_guides.top_left_x_norm,
            "wheel_guides.top_right_x_norm": settings.wheel_guides.top_right_x_norm,
            "wheel_guides.top_y_norm": settings.wheel_guides.top_y_norm,
            "wheel_guides.stop_y_norm": settings.wheel_guides.stop_y_norm,
            "timers.exit_clear_seconds": settings.timers.exit_clear_seconds,
            "timers.machine_operation_seconds": settings.timers.machine_operation_seconds,
        }
        for key, value in values.items():
            widget = self.process_settings_inputs.get(key)
            if widget is not None:
                widget.setValue(value)  # type: ignore[union-attr]
        upload_box = self.process_settings_inputs.get("nas_upload_mode")
        if isinstance(upload_box, QComboBox):
            index = upload_box.findData(settings.nas_upload_mode)
            upload_box.setCurrentIndex(max(0, index))
        audio_box = self.process_settings_inputs.get("audio_enabled")
        if isinstance(audio_box, QCheckBox):
            audio_box.setChecked(settings.audio_enabled)
        self._update_settings_preview_overlays()

    def _process_settings_payload(self) -> dict[str, object]:
        payload: dict[str, dict[str, object]] = {}
        for key, widget in self.process_settings_inputs.items():
            if key in ("nas_upload_mode", "audio_enabled"):
                continue
            section, field = key.split(".", 1)
            payload.setdefault(section, {})[field] = widget.value()  # type: ignore[union-attr]
        result: dict[str, object] = dict(payload)
        upload_box = self.process_settings_inputs.get("nas_upload_mode")
        if isinstance(upload_box, QComboBox):
            result["nas_upload_mode"] = upload_box.currentData()
        audio_box = self.process_settings_inputs.get("audio_enabled")
        if isinstance(audio_box, QCheckBox):
            result["audio_enabled"] = audio_box.isChecked()
        return result

    def _save_process_settings(self) -> None:
        try:
            settings = settings_from_payload(self._process_settings_payload())
        except (ValueError, TypeError) as exc:
            self.process_settings_status.setText(f"저장 실패 (값 검증 오류): {exc}")
            return
        try:
            save_operator_settings(settings, OPERATOR_SETTINGS_PATH)
        except OSError as exc:
            self.process_settings_status.setText(f"저장 실패 (파일 오류): {exc}")
            return
        self.operator_settings = settings
        if self.process_engine is not None:
            self.process_engine.apply_settings(settings)
        self.process_settings_status.setText(
            f"저장 완료: {OPERATOR_SETTINGS_PATH} · 실행 중인 감시에 즉시 적용되었습니다."
        )

    def _toggle_process_engine(self) -> None:
        self._engine_stopped_by_operator = not self._engine_stopped_by_operator
        button = self.engine_toggle_button
        button.setChecked(self._engine_stopped_by_operator)
        button.setText(
            "프로세스 감시 재개" if self._engine_stopped_by_operator else "프로세스 감시 일시중지"
        )
        if self._engine_stopped_by_operator:
            if self._purpose_task_enabled and self._purpose_task_id == PURPOSE_PROCESS_MONITORING:
                self._stop_purpose_inference()
            self.warning_label.setText("프로세스 감시를 일시중지했습니다. 최종 OK는 차단됩니다.")
        else:
            self._engine_last_start_attempt = 0.0
            self.warning_label.setText("프로세스 감시를 재개합니다. 최종 OK는 차단됩니다.")

    def _update_settings_preview_overlays(self) -> None:
        if not getattr(self, "_settings_preview_active", False):
            return
        front = self.camera_widgets.get(CameraRole.front)
        if front is None:
            return
        try:
            value = lambda key: float(self.process_settings_inputs[key].value())  # type: ignore[union-attr] # noqa: E731
            line_y = value("plate_zone.line_y_norm")
            guides = (
                value("wheel_guides.left_x_norm"),
                value("wheel_guides.right_x_norm"),
                value("wheel_guides.top_left_x_norm"),
                value("wheel_guides.top_right_x_norm"),
                value("wheel_guides.top_y_norm"),
                value("wheel_guides.stop_y_norm"),
            )
        except KeyError:
            return
        front.set_plate_line(line_y)
        front.set_guide_overlay(guides)

    def _set_settings_preview_active(self, active: bool) -> None:
        self._settings_preview_active = active
        front = self.camera_widgets.get(CameraRole.front)
        if active:
            self._update_settings_preview_overlays()
        elif front is not None:
            front.set_plate_line(None)
            front.set_guide_overlay(None)

    def _show_operator_page(self, label: str) -> None:
        page = self.operator_pages[label]
        camera_layout = self._camera_page_layouts.get(label)
        self.setUpdatesEnabled(False)
        try:
            if hasattr(self, "operator_view"):  # not yet assigned during _build
                self.stack.setCurrentWidget(self.operator_view)
            if camera_layout is not None:
                self._adopt_camera_area(page, camera_layout)
            self.operator_workspace_stack.setCurrentWidget(page)
        finally:
            self.setUpdatesEnabled(True)
        for nav_label, button in self.sidebar_buttons.items():
            if button.isCheckable():
                button.setChecked(nav_label == label)
        self._set_settings_preview_active(label == "감시 설정")
        if label == "레이더 (LD2410)":
            self._render_ld2410_console()
            self._refresh_ld2410_console_status()
        elif label == "실행 로그":
            self._refresh_log_view()
        elif label == "주차 프로세스 테스트":
            self._refresh_driver_display(apply_layout=False)
            self.driver_preview.restore_presentation()
        elif label == "카메라 캘리브레이션":
            self._refresh_calibration_page()
        elif label == "지면 기준점":
            self._refresh_ground_page()
        self.update()

    def _adopt_camera_area(self, page: QWidget, layout_mode: str) -> None:
        """Move the shared camera grid into the page and apply its layout mode."""
        slot = getattr(page, "camera_slot", None)
        if slot is None:
            return
        if self.operator_camera_area.parentWidget() is not page:
            slot.addWidget(self.operator_camera_area)
        self._set_camera_layout(layout_mode)
        self.operator_camera_area.show()
        self.grid.invalidate()
        self.grid.activate()

    def _start_hailo_health_monitor(self) -> None:
        if self.settings is None:
            return
        thread = QThread(self)
        worker = HailoHealthWorker(self.settings)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.snapshot_ready.connect(self._set_hailo_health)
        worker.incident_reported.connect(self._set_hailo_incident)
        worker.finished.connect(thread.quit)
        self._hailo_health_threads.append(thread)
        self._hailo_health_workers.append(worker)
        thread.start()

    def _stop_hailo_health_monitor(self) -> None:
        for worker in tuple(self._hailo_health_workers):
            worker.stop()

    def _set_hailo_health(self, snapshot: HailoHealthSnapshot) -> None:
        self._hailo_health_snapshot = snapshot
        if hasattr(self, "hailo_status_label"):
            self.hailo_status_label.setText(snapshot.pill_text)
            if self.hailo_status_label.property("hailo") != snapshot.status:
                self.hailo_status_label.setProperty("hailo", snapshot.status)
                self.hailo_status_label.style().unpolish(self.hailo_status_label)
                self.hailo_status_label.style().polish(self.hailo_status_label)
        if hasattr(self, "hailo_health_label"):
            stamp = snapshot.checked_at.astimezone().strftime("%H:%M:%S")
            self.hailo_health_label.setText(
                f"Hailo 장치 상태 ({stamp} 기준)\n" + "\n".join(snapshot.detail_lines())
            )
        if hasattr(self, "hailo_holder_kill_button"):
            self.hailo_holder_kill_button.setEnabled(bool(snapshot.foreign_holders))
        self._record_hailo_health_row(snapshot)

    def _record_hailo_health_row(self, snapshot: HailoHealthSnapshot) -> None:
        """Put the health snapshot in the daily JSONL so the NAS day carries the failure timeline.

        Every status change and every bad sample is kept; healthy samples are thinned to one per
        HAILO_HEALTH_RECORD_INTERVAL_SECONDS so an all-day healthy run stays a few hundred rows.
        """
        if self._raw_data_manager is None:
            return
        now = time.monotonic()
        changed = snapshot.status != self._hailo_health_recorded_status
        bad = snapshot.status != "ok"
        due = (
            self._hailo_health_recorded_at is None
            or now - self._hailo_health_recorded_at >= HAILO_HEALTH_RECORD_INTERVAL_SECONDS
        )
        if not (changed or bad or due):
            return
        self._hailo_health_recorded_status = snapshot.status
        self._hailo_health_recorded_at = now
        self._record_raw(self._raw_data_manager.record_hailo_health, snapshot)

    def _set_hailo_incident(self, report: IncidentReport) -> None:
        """Show the result of an automatic Hailo evidence upload. Diagnostic only."""
        message = report.summary()
        self.warning_label.setText(f"{message}. 진단 전용이며 최종 OK는 차단됩니다.")
        if hasattr(self, "system_test_log"):
            self.system_test_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        if hasattr(self, "hailo_health_label"):
            self.hailo_health_label.setText(self.hailo_health_label.text() + f"\n{message}")

    def _terminate_hailo_holders(self, checked: bool = False) -> None:
        """Kill orphaned Hailo holders (never this app's own children). Diagnostic only."""
        del checked
        if not self._operator_unlocked:
            return
        snapshot = self._hailo_health_snapshot
        holders = snapshot.foreign_holders if snapshot is not None else ()
        if not holders:
            self.warning_label.setText("종료할 고아 프로세스가 없습니다.")
            return
        from towersightai.inference.hailo_health import terminate_foreign_holders

        handled = terminate_foreign_holders(holders)
        listed = ", ".join(str(pid) for pid in handled) or "없음"
        message = f"고아 프로세스 종료 요청: PID {listed}. 다음 상태 갱신에서 확인됩니다"
        self.warning_label.setText(f"{message}. 진단 전용이며 최종 OK는 차단됩니다.")
        if hasattr(self, "system_test_log"):
            self.system_test_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        self.hailo_holder_kill_button.setEnabled(False)

    def _run_system_test(self, test_id: str) -> None:
        if not self._operator_unlocked:
            return
        if self._system_test_running:
            self.warning_label.setText("이미 진단이 실행 중입니다. 완료 후 다시 시도해 주세요.")
            return
        if self.settings is None:
            self.system_test_log.appendPlainText("[SKIP] 설정이 없어 진단을 실행할 수 없습니다.")
            return
        self._system_test_running = True
        for button in self.system_test_buttons.values():
            button.setEnabled(False)
        label = self.system_test_buttons[test_id].text()
        self.system_test_log.appendPlainText(f"[실행] {label} ...")
        thread = QThread(self)
        worker = DiagnosticsWorker(self.settings, test_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_ready.connect(self._set_system_test_result)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_system_test_worker(thread, worker))
        self._system_test_threads.append(thread)
        self._system_test_workers.append(worker)
        thread.start()

    def _set_system_test_result(self, result: DiagnosticResult) -> None:
        line = (
            f"[{result.status.value}] {result.label} — {result.summary}"
            f" ({result.duration_ms}ms) · safe_to_operate={result.safe_to_operate}"
        )
        self.system_test_log.appendPlainText(line)
        if result.detail:
            self.system_test_log.appendPlainText(f"    {result.detail}")

    def _cleanup_system_test_worker(self, thread: QThread, worker: QObject) -> None:
        if thread in self._system_test_threads:
            self._system_test_threads.remove(thread)
        if worker in self._system_test_workers:
            self._system_test_workers.remove(worker)
        if not self._system_test_workers:
            self._system_test_running = False
            for button in self.system_test_buttons.values():
                button.setEnabled(True)

    def _refresh_log_view(self) -> None:
        if not hasattr(self, "log_view"):
            return
        log_path = DEFAULT_RUNTIME_LOG
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as fp:
                if size > LOG_VIEW_TAIL_BYTES:
                    fp.seek(size - LOG_VIEW_TAIL_BYTES)
                    fp.readline()  # drop the partial first line
                text = fp.read().decode("utf-8", errors="replace")
        except OSError:
            self.log_view.setPlainText("")
            return
        needle = self.log_filter_input.text().strip()
        if needle:
            text = "\n".join(line for line in text.splitlines() if needle.lower() in line.lower())
        if text != self.log_view.toPlainText():
            scrollbar = self.log_view.verticalScrollBar()
            keep_position = scrollbar.value()
            self.log_view.setPlainText(text)
            if self.log_follow_checkbox.isChecked():
                scrollbar.setValue(scrollbar.maximum())
            else:
                scrollbar.setValue(min(keep_position, scrollbar.maximum()))

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
        self._refresh_ld2410_console_status()
        if (
            hasattr(self, "log_view")
            and self.stack.currentWidget() is self.operator_view
            and self.operator_workspace_stack.currentWidget() is self.operator_pages.get("실행 로그")
        ):
            self._refresh_log_view()
        if self._evidence_coordinator is not None:
            self.evidence_status_label.setText(self._evidence_coordinator.status_summary)
        if self._last_person_detection_at is not None and time.monotonic() - self._last_person_detection_at >= PERSON_ALERT_STALE_SECONDS:
            self._reset_person_alert()
            if self._driver_state_override is ParkingState.HUMAN_DETECTED and not self._driver_simulated:
                self._driver_state_override = None
                self._refresh_driver_display()
        self._refresh_user_mode_labels()
        if self._purpose_task_enabled and self._purpose_task_started_at is not None and self._purpose_task_first_inference_seconds is None:
            elapsed = time.monotonic() - self._purpose_task_started_at
            self.ai_detection_label.setText(
                _purpose_detection_label(self._purpose_task_label, self._detection_camera_ids, self._detection_event_counts, loading_seconds=elapsed)
            )
        self._tick_process_engine()

    # ------------------------------------------------------------- process engine

    def _tick_process_engine(self) -> None:
        engine = self.process_engine
        if engine is None:
            return
        self._ensure_process_monitoring()
        monitoring_running = (
            self._purpose_task_enabled and self._purpose_task_id == PURPOSE_PROCESS_MONITORING
        )
        engine.observe_monitoring_health(
            running=monitoring_running,
            recovering=monitoring_running and self._purpose_task_first_inference_seconds is None,
        )
        output = engine.tick(datetime.now(timezone.utc))
        self._apply_engine_output(output)
        self._pump_periodic_lpr()

    def _ensure_process_monitoring(self) -> None:
        """Auto-start (and resume after manual tasks) the combined monitoring task."""
        if (
            not self._engine_enabled
            or self._engine_stopped_by_operator
            or self.settings is None
            or self._purpose_workers
            or self._pending_user_purpose_task_id
            or self._purpose_task_enabled
        ):
            return
        now = time.monotonic()
        if now - self._engine_last_start_attempt < self._monitoring_cooldown_seconds():
            return
        streaming = _streaming_camera_ids(self.settings, self._runtime_camera_status)
        if streaming != self._monitoring_streaming_set:
            self._monitoring_streaming_set = streaming
            self._monitoring_streaming_since = now
        if not streaming:
            return
        if now - self._monitoring_streaming_since < MONITORING_CAMERA_SETTLE_SECONDS:
            return
        self._engine_last_start_attempt = now
        self._start_purpose_inference(PURPOSE_PROCESS_MONITORING)

    def _monitoring_cooldown_seconds(self) -> float:
        failures = self._monitoring_consecutive_failures
        if failures <= 0:
            return MONITORING_START_COOLDOWN_SECONDS
        return min(
            MONITORING_FAILURE_COOLDOWN_MAX_SECONDS,
            MONITORING_FAILURE_COOLDOWN_SECONDS * (2 ** (failures - 1)),
        )

    def _engine_display_allowed(self) -> bool:
        """The engine drives the driver display unless a test/simulation owns it."""
        if self._driver_simulated or self._vehicle_entry_simulation:
            return False
        if self._user_mode_state != "idle":
            return False
        return self._driver_state_override is None or self._engine_owns_display

    def _apply_engine_output(self, output: EngineOutput) -> None:
        for request in output.plc_requests:
            try:
                self.plc_adapter.send(request.name, dict(request.payload))
            except Exception:  # noqa: BLE001 - simulated adapter must never break the UI
                logging.getLogger("towersightai.process.engine").exception("plc send failed")
        if self._raw_data_manager is not None:
            for event in output.raw_events:
                if event.kind == "vehicle_entry":
                    self._record_raw(
                        self._raw_data_manager.record_vehicle_entry,
                        camera_id=event.camera_id or "opposite_side",
                        simulated=False,
                        managed=True,
                    )
                elif event.kind == "vehicle_session_end":
                    self._record_raw(
                        self._raw_data_manager.end_vehicle_session, reason=event.reason
                    )
                elif event.kind == "plate":
                    self._record_raw(
                        self._raw_data_manager.record_plate,
                        event.plate_number,
                        confidence=event.confidence,
                        simulated=False,
                        recognized=event.recognized,
                        reads=event.reads,
                        reason=event.reason,
                        # Winning read's frame + box so the evidence layer stores the plate
                        # image and its crop for the engine's automatic entries too.
                        source_image_path=event.source_image_path or None,
                        plate_bbox=dict(event.bbox) if event.bbox else None,
                        plate_text=event.plate_text,
                    )
                elif event.kind == "vehicle_exit_start":
                    self._record_raw(
                        self._raw_data_manager.record_vehicle_exit_start,
                        camera_id=event.camera_id or "opposite_side",
                    )
                elif event.kind == "vehicle_exit_end":
                    self._record_raw(
                        self._raw_data_manager.record_vehicle_exit_end, reason=event.reason
                    )
                elif event.kind == "plate_attempt":
                    self._record_raw(
                        self._raw_data_manager.record_plate_attempt,
                        event.plate_number,
                        confidence=event.confidence,
                        camera_id=event.camera_id or "front",
                        accepted=event.accepted,
                        reason=event.reason,
                        plate_bbox=event.bbox,
                        plate_text=event.plate_text,
                    )
        if output.lpr_control == "start":
            self._start_periodic_lpr()
        elif output.lpr_control == "stop":
            self._stop_periodic_lpr()
        if output.audio_cue and self.operator_settings.audio_enabled:
            self._play_audio_cue(output.audio_cue)

        phase_text = f"프로세스 {output.phase}"
        if output.uncertain_reason:
            phase_text += f" · {output.uncertain_reason}"
        if hasattr(self, "process_status_label"):
            self.process_status_label.setText(phase_text)

        # NAS immediate mode: sync once per cycle, deferred until back in IDLE.
        if (
            self.operator_settings.nas_upload_mode == "immediate"
            and output.phase == "idle_monitoring"
            and self._engine_last_phase not in ("idle_monitoring",)
            and self._raw_data_manager is not None
        ):
            self._record_raw(self._raw_data_manager.request_current_day_sync)
        self._engine_last_phase = output.phase

        if self._engine_display_allowed():
            previous_override = self._driver_state_override
            previous_copy = self._engine_copy_key
            previous_plate = self._driver_masked_plate
            self._driver_state_override = (
                None if output.public_state is ParkingState.IDLE else output.public_state
            )
            self._engine_owns_display = self._driver_state_override is not None
            self._engine_copy_key = output.copy_key
            if output.plate_number:
                self._driver_masked_plate = _masked_plate(output.plate_number)
            elif previous_override is not None and self._driver_state_override is None:
                self._driver_masked_plate = ""
            guides = self.operator_settings.wheel_guides
            self._set_front_guide_overlay(
                (
                    guides.left_x_norm,
                    guides.right_x_norm,
                    guides.top_left_x_norm,
                    guides.top_right_x_norm,
                    guides.top_y_norm,
                    guides.stop_y_norm,
                )
                if output.show_wheel_guides
                else None
            )
            if output.warning_text:
                self.warning_label.setText(f"{output.warning_text} 최종 OK는 차단됩니다.")
            if (
                previous_override is not self._driver_state_override
                or previous_copy != self._engine_copy_key
                or previous_plate != self._driver_masked_plate
            ):
                self._refresh_driver_display()
        elif self._engine_owns_display:
            # A test/simulation took over; drop our claim and overlays.
            self._engine_owns_display = False
            self._engine_copy_key = None
            self._set_front_guide_overlay(None)

    def _set_front_guide_overlay(self, guides: tuple[float, float, float] | None) -> None:
        if getattr(self, "_settings_preview_active", False):
            return  # the 감시 설정 preview owns the front overlays while open
        for widget in self._camera_surfaces(CameraRole.front):
            widget.set_guide_overlay(guides)

    def _play_audio_cue(self, cue_id: str) -> None:
        if self._audio_player is None:
            from towersightai.ui.audio import AudioAlertPlayer

            self._audio_player = AudioAlertPlayer()
        self._audio_player.play(cue_id)

    # ------------------------------------------------------------- periodic LPR

    def _start_periodic_lpr(self) -> None:
        if self._periodic_lpr_active or self.settings is None:
            return
        self._periodic_lpr_active = True
        self._periodic_lpr_inflight = False
        self._periodic_lpr_last_sent = 0.0
        if not self._periodic_lpr_workers:
            output_dir = Path("artifacts/runtime/purpose-ai/process_lpr") / datetime.now(
                timezone.utc
            ).strftime("run-%Y%m%d-%H%M%S")
            thread = QThread(self)
            worker = PeriodicFrontLprWorker(output_dir)
            worker.moveToThread(thread)
            self.periodic_lpr_frame.connect(worker.process_frame)
            worker.attempt_ready.connect(self._on_periodic_lpr_attempt)
            worker.status_changed.connect(self._on_periodic_lpr_status)
            worker.failed.connect(self._on_periodic_lpr_failed)
            self._periodic_lpr_threads.append(thread)
            self._periodic_lpr_workers.append(worker)
            thread.start()

    def _stop_periodic_lpr(self) -> None:
        self._periodic_lpr_active = False
        self._periodic_lpr_inflight = False

    def _shutdown_periodic_lpr(self) -> None:
        self._stop_periodic_lpr()
        for worker in self._periodic_lpr_workers:
            try:
                self.periodic_lpr_frame.disconnect(worker.process_frame)
            except TypeError:
                pass
        self._periodic_lpr_workers.clear()
        for thread in self._periodic_lpr_threads:
            thread.quit()
            thread.wait(10000)
        self._periodic_lpr_threads.clear()

    def _pump_periodic_lpr(self) -> None:
        if not self._periodic_lpr_active or self._periodic_lpr_inflight:
            return
        interval = self.operator_settings.plate_zone.read_interval_seconds
        if time.monotonic() - self._periodic_lpr_last_sent < interval:
            return
        front_widget = self.camera_widgets.get(CameraRole.front)
        frame = front_widget.current_frame() if front_widget is not None else None
        if frame is None:
            return
        self._periodic_lpr_inflight = True
        self._periodic_lpr_last_sent = time.monotonic()
        self.periodic_lpr_frame.emit(frame)

    def _on_periodic_lpr_attempt(self, payload: object, frame_width: int, frame_height: int) -> None:
        self._periodic_lpr_inflight = False
        del frame_width
        if self.process_engine is not None and isinstance(payload, dict):
            self.process_engine.observe_lpr_attempt(payload, frame_height)

    def _on_periodic_lpr_status(self, message: str) -> None:
        if hasattr(self, "process_status_label"):
            self.process_status_label.setText(f"프로세스 LPR: {message}")

    def _on_periodic_lpr_failed(self, message: str) -> None:
        self._periodic_lpr_inflight = False
        self._periodic_lpr_active = False
        self.warning_label.setText(f"{message} 번호판은 미인식으로 진행됩니다. 최종 OK는 차단됩니다.")

    def _show_operator(self) -> None:
        if not self._operator_unlocked:
            return
        self._show_operator_page("전체 카메라")

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
        self._show_operator_page("전체 카메라")

    def _show_all_cameras(self) -> None:
        self._operator_unlocked = True
        self._show_operator_page("전체 카메라")

    def _show_ld2410_console(self) -> None:
        if not self._operator_unlocked:
            return
        self._show_operator_page("레이더 (LD2410)")

    def _show_camera_settings(self) -> None:
        if not self._operator_unlocked:
            return
        self._refresh_rotation_controls()
        self.stack.setCurrentWidget(self.settings_view)

    def _unlock_operator(self) -> None:
        self._operator_unlocked = True
        self._show_operator_dashboard()

    def _request_shutdown(self) -> None:
        """Operator-menu application exit. Safety state is never changed by this path."""
        if not self._operator_unlocked:
            return
        if not self._confirm_shutdown():
            return
        self.close()

    def _confirm_shutdown(self) -> bool:
        answer = QMessageBox.question(
            self,
            "TowerSightAI 종료",
            "감시 화면을 종료합니다.\n종료 중에는 AI 감시와 안내가 중단됩니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    def _toggle_sidebar(self) -> None:
        self.operator_sidebar.setVisible(not self.operator_sidebar.isVisible())

    def _toggle_driver_test_panel(self, checked: bool = False) -> None:
        del checked
        self._show_operator_page("주차 프로세스 테스트")

    def _set_driver_test_preview(self, enabled: bool) -> None:
        if enabled:
            self._show_operator_page("주차 프로세스 테스트")
        elif self.operator_workspace_stack.currentWidget() is self.operator_pages["주차 프로세스 테스트"]:
            self._show_operator_page("전체 카메라")

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

    def _append_ld2410_frame(self, frame: LD2410Frame, client_ip: str) -> None:
        self._ld2410_connection_state = "client_connected"
        self._ld2410_client_ip = client_ip
        self._ld2410_last_frame_at = frame.received_at
        # The radar is verification-only: frames are recorded to raw data (RawDataManager reads
        # the service snapshot) and shown on the operator console, but they never reach the
        # process engine, the driver display, or any safety decision.
        self._append_ld2410_console_line(_format_ld2410_console_line(frame, client_ip))
        self._refresh_ld2410_console_status()

    def _apply_ld2410_status(self, state: str, details: object) -> None:
        status_details = dict(details) if isinstance(details, dict) else {}
        self._ld2410_connection_state = state
        self._ld2410_status_details = status_details
        client_ip = status_details.get("client_ip")
        if client_ip:
            self._ld2410_client_ip = str(client_ip)
        self._append_ld2410_console_line(_format_ld2410_status_line(state, status_details))
        self._refresh_ld2410_console_status()

    def _append_ld2410_console_line(self, line: str) -> None:
        self._ld2410_console_lines.append(line)
        if self._ld2410_console_paused:
            return
        self.ld2410_console.appendPlainText(line)
        scrollbar = self.ld2410_console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _render_ld2410_console(self) -> None:
        if self._ld2410_console_paused:
            return
        self.ld2410_console.setPlainText("\n".join(self._ld2410_console_lines))
        scrollbar = self.ld2410_console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _toggle_ld2410_console_pause(self) -> None:
        self._ld2410_console_paused = not self._ld2410_console_paused
        self.ld2410_pause_button.setText("화면 재개" if self._ld2410_console_paused else "화면 일시정지")
        self.ld2410_pause_button.setProperty("active", self._ld2410_console_paused)
        self.ld2410_pause_button.style().unpolish(self.ld2410_pause_button)
        self.ld2410_pause_button.style().polish(self.ld2410_pause_button)
        if not self._ld2410_console_paused:
            self._render_ld2410_console()

    def _clear_ld2410_console(self) -> None:
        self._ld2410_console_lines.clear()
        self.ld2410_console.clear()

    def _refresh_ld2410_console_status(self) -> None:
        if not hasattr(self, "ld2410_connection_label"):
            return
        if self.settings is None:
            host, port = "-", "-"
            effective_state = "disabled"
        else:
            host, port = self.settings.ld2410.bind_host, str(self.settings.ld2410.port)
            effective_state = self._ld2410_connection_state
            if not self.settings.ld2410.enabled:
                effective_state = "disabled"
            elif effective_state == "client_connected" and self._ld2410_last_frame_at is not None:
                age = (datetime.now(timezone.utc) - self._ld2410_last_frame_at).total_seconds()
                if age > self.settings.ld2410.max_sample_age_seconds:
                    effective_state = "stale"

        labels = {
            "disabled": ("비활성", "disabled"),
            "listening": ("연결 대기", "waiting"),
            "client_connected": ("연결됨", "connected"),
            "stale": ("수신 지연", "waiting"),
            "client_disconnected": ("연결 끊김", "waiting"),
            "error": ("서버 오류", "error"),
            "stopped": ("서버 중지", "disabled"),
        }
        text, style_state = labels.get(effective_state, ("상태 확인 중", "waiting"))
        detail_parts = [text]
        if self._ld2410_client_ip:
            detail_parts.append(f"클라이언트 {self._ld2410_client_ip}")
        if self._ld2410_last_frame_at is not None:
            age = max(0.0, (datetime.now(timezone.utc) - self._ld2410_last_frame_at).total_seconds())
            detail_parts.append(f"마지막 수신 {age:.1f}초 전")
        reason = self._ld2410_status_details.get("reason")
        if reason and effective_state in {"client_disconnected", "error"}:
            detail_parts.append(str(reason))
        self.ld2410_connection_label.setText(" · ".join(detail_parts))
        self.ld2410_connection_label.setProperty("status", style_state)
        self.ld2410_connection_label.style().unpolish(self.ld2410_connection_label)
        self.ld2410_connection_label.style().polish(self.ld2410_connection_label)
        self.ld2410_endpoint_label.setText(f"서버 {host}:{port}")

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
            if hasattr(widget, "set_bottom_inset"):
                widget.set_bottom_inset(0)
        if hasattr(self, "driver_view"):
            self.driver_view.detach_cameras()
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
        if mode == "front":
            layout = ((CameraRole.front, 0, 0, 1, 1),)
        elif mode.startswith("single:"):
            layout = ((CameraRole(mode.split(":", 1)[1]), 0, 0, 1, 1),)
        elif not self.model.birdview_available:
            layout = (
                (CameraRole.front, 0, 0, 2, 1),
                (CameraRole.rear_side, 0, 1, 1, 1),
                (CameraRole.opposite_side, 1, 1, 1, 1),
            )
        else:
            layout = (
                (CameraRole.ceiling, 0, 0, 1, 1),
                (CameraRole.front, 0, 1, 1, 1),
                (CameraRole.rear_side, 1, 0, 1, 1),
                (CameraRole.opposite_side, 1, 1, 1, 1),
            )
        for role, row, col, row_span, col_span in layout:
            widget = self.camera_widgets[role]
            # Keep tile minimums small so a resized window reflows instead of overflowing;
            # the paint path letterboxes each frame centered, so ratios stay honest.
            widget.setMinimumSize(240, 140)
            widget.setMaximumWidth(16777215)
            self.grid.addWidget(widget, row, col, row_span, col_span)
            widget.show()
        if mode == "front" or mode.startswith("single:"):
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
        elif not self.model.birdview_available:
            self.grid.setColumnStretch(0, 3)
            self.grid.setColumnStretch(1, 2)
        else:
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
        self.grid.setRowStretch(0, 1)
        # Single-tile layouts only fill row 0. Leaving row 1 stretched gave the empty row half
        # the height, so the calibration and ground-reference tiles came out ~150 px tall —
        # far too small to click a pallet corner on (found 2026-09-17).
        single = mode == "front" or mode.startswith("single:")
        self.grid.setRowStretch(1, 0 if single else 1)

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
            if hasattr(self, "lpr_result_label"):
                self.lpr_result_label.setText(f"정면 카메라 인식: •••• {last4}" if last4 else "정면 카메라 인식: 결과 없음")
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

    def _start_nas_connection_test(self, checked: bool = False) -> None:
        """Write one diagnostic payload to the NAS. It never authorizes final OK."""
        del checked
        if not self._operator_unlocked:
            return
        if self._nas_test_running:
            self._set_nas_test_status("NAS 연결 확인이 이미 실행 중입니다")
            return
        config = self.settings.raw_storage if self.settings is not None else None
        if config is None or not config.nas_host:
            self.instruction_label.setText("NAS 연결 확인 불가")
            self._set_nas_test_status("NAS 설정이 없습니다. .env의 SYNOLOGY_NAS_* 값을 확인해 주세요")
            return

        self._nas_test_running = True
        self._nas_test_frames = []
        self._nas_test_ticks = 0
        camera_id, widget = self._nas_test_source()
        self._nas_test_camera_id = camera_id
        self._nas_test_widget = widget
        self._refresh_nas_test_button()
        if widget is None:
            self._set_nas_test_status("NAS 연결 확인: 수신 중인 카메라가 없어 영상 없이 진행합니다")
            self._launch_nas_test_worker()
            return

        self._set_nas_test_status(
            f"NAS 연결 확인: {camera_id} 영상 {NAS_TEST_CLIP_SECONDS:.0f}초 수집 중"
        )
        timer = QTimer(self)
        timer.setInterval(max(1, round(1000 / NAS_TEST_CLIP_FPS)))
        timer.timeout.connect(self._collect_nas_test_frame)
        self._nas_test_timer = timer
        timer.start()

    def _nas_test_source(self) -> tuple[str, CameraSurface | None]:
        """Prefer the front camera, then any other camera that is actually streaming."""
        if self.settings is None:
            return "", None
        ordered = sorted(
            self.settings.active_cameras,
            key=lambda camera: 0 if camera.role is CameraRole.front else 1,
        )
        for camera in ordered:
            widget = self.camera_widgets.get(camera.role)
            if widget is None or self._runtime_camera_status.get(camera.id) != "정상 수신":
                continue
            if widget.current_frame() is None:
                continue
            return camera.id, widget
        return "", None

    def _collect_nas_test_frame(self) -> None:
        self._nas_test_ticks += 1
        widget = getattr(self, "_nas_test_widget", None)
        frame = widget.current_frame() if widget is not None else None
        if frame is not None:
            self._nas_test_frames.append(frame)
        if self._nas_test_ticks >= max(1, round(NAS_TEST_CLIP_SECONDS * NAS_TEST_CLIP_FPS)):
            self._finish_nas_test_collection()

    def _finish_nas_test_collection(self) -> None:
        self._stop_nas_test_timer()
        self._launch_nas_test_worker()

    def _stop_nas_test_timer(self) -> None:
        if self._nas_test_timer is not None:
            self._nas_test_timer.stop()
            self._nas_test_timer.deleteLater()
            self._nas_test_timer = None

    def _launch_nas_test_worker(self) -> None:
        if self.settings is None:
            self._nas_test_running = False
            self._refresh_nas_test_button()
            return
        thread = QThread(self)
        worker = NasConnectionTestWorker(
            self.settings.raw_storage,
            tuple(self._nas_test_frames),
            camera_id=self._nas_test_camera_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status_changed.connect(self._set_nas_test_status)
        worker.result_ready.connect(self._set_nas_test_result)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_nas_test_worker(thread, worker))
        self._nas_test_threads.append(thread)
        self._nas_test_workers.append(worker)
        thread.start()

    def _set_nas_test_status(self, message: str) -> None:
        self.warning_label.setText(f"{message}. 진단 전용이며 최종 OK는 차단됩니다.")
        if hasattr(self, "nas_result_label"):
            self.nas_result_label.setText(message)

    def _set_nas_test_result(self, result: NasConnectionTestResult) -> None:
        clip = f" · {self._nas_test_camera_id} 영상 {len(self._nas_test_frames)}프레임" if self._nas_test_frames else ""
        if result.ok:
            self.instruction_label.setText(f"NAS 연결 확인 성공: {result.remote_dir}")
            self._set_nas_test_status(f"{result.summary}{clip}")
        else:
            self.instruction_label.setText("NAS 연결 확인 실패")
            self._set_nas_test_status(f"{result.summary} · {result.error}")
        if hasattr(self, "nas_history"):
            stamp = datetime.now().strftime("%H:%M:%S")
            outcome = f"성공 {result.remote_dir}" if result.ok else f"실패 {result.error}"
            self.nas_history.appendPlainText(f"[{stamp}] {outcome} · {result.summary}{clip}")
        self._nas_test_frames = []
        self._nas_test_widget = None

    def _refresh_nas_test_button(self) -> None:
        button = getattr(self, "nas_test_button", None)
        if button is not None:
            button.setEnabled(not self._nas_test_running)

    def _stop_nas_connection_test(self) -> None:
        self._stop_nas_test_timer()
        for worker in tuple(self._nas_test_workers):
            worker.stop()

    def _cleanup_nas_test_worker(self, thread: QThread, worker: NasConnectionTestWorker) -> None:
        if thread in self._nas_test_threads:
            self._nas_test_threads.remove(thread)
        if worker in self._nas_test_workers:
            self._nas_test_workers.remove(worker)
        if not self._nas_test_workers:
            self._nas_test_running = False
            self._refresh_nas_test_button()

    def _nas_transfer_target_text(self) -> str:
        config = self.settings.raw_storage if self.settings is not None else None
        if config is None or not config.nas_folder:
            return "저장 위치: NAS 설정 없음 (.env의 SYNOLOGY_NAS_* 확인)"
        from towersightai.storage.file_transfer import remote_file_transfer_dir

        return f"저장 위치: {config.nas_host}:{remote_file_transfer_dir(config)}"

    def _pick_nas_transfer_files(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked or self._nas_transfer_running:
            return
        start_dir = str(self._nas_transfer_files[-1].parent) if self._nas_transfer_files else str(Path.home())
        selected, _filter = QFileDialog.getOpenFileNames(self, "NAS로 보낼 파일 선택", start_dir)
        if selected:
            self._add_nas_transfer_files([Path(item) for item in selected])

    def _add_nas_transfer_files(self, files: list[Path]) -> None:
        """Append files to the pending list (duplicates by path are ignored)."""
        existing = {path.resolve(strict=False) for path in self._nas_transfer_files}
        for path in files:
            resolved = Path(path).resolve(strict=False)
            if resolved in existing:
                continue
            existing.add(resolved)
            self._nas_transfer_files.append(Path(path))
        self._render_nas_transfer_files()

    def _clear_nas_transfer_files(self, checked: bool = False) -> None:
        del checked
        if self._nas_transfer_running:
            return
        self._nas_transfer_files = []
        self._render_nas_transfer_files()

    def _render_nas_transfer_files(self) -> None:
        widget = getattr(self, "nas_transfer_list", None)
        if widget is not None:
            widget.clear()
            for path in self._nas_transfer_files:
                size = path.stat().st_size if path.is_file() else 0
                widget.addItem(f"{path.name}  ({size:,}B)  {path}")
        label = getattr(self, "nas_transfer_result_label", None)
        if label is not None and not self._nas_transfer_running:
            count = len(self._nas_transfer_files)
            label.setText(f"선택된 파일 {count}개" if count else "보낼 파일을 선택해 주세요.")
        self._refresh_nas_transfer_buttons()

    def _start_nas_file_transfer(self, checked: bool = False) -> None:
        """Upload the selected files into the NAS transfer folder. It never authorizes final OK."""
        del checked
        if not self._operator_unlocked:
            return
        if self._nas_transfer_running:
            self._set_nas_transfer_status("NAS 파일 전송이 이미 실행 중입니다")
            return
        config = self.settings.raw_storage if self.settings is not None else None
        if config is None or not config.nas_host:
            self.instruction_label.setText("NAS 파일 전송 불가")
            self._set_nas_transfer_status("NAS 설정이 없습니다. .env의 SYNOLOGY_NAS_* 값을 확인해 주세요")
            return
        if not self._nas_transfer_files:
            self._set_nas_transfer_status("보낼 파일이 없습니다. 먼저 파일을 선택해 주세요")
            return

        self._nas_transfer_running = True
        self._refresh_nas_transfer_buttons()
        self._set_nas_transfer_status(f"NAS 파일 전송: {len(self._nas_transfer_files)}개 파일 업로드 시작")
        thread = QThread(self)
        worker = NasFileTransferWorker(config, tuple(self._nas_transfer_files))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status_changed.connect(self._set_nas_transfer_status)
        worker.result_ready.connect(self._set_nas_transfer_result)
        worker.finished.connect(thread.quit)
        thread.finished.connect(
            lambda worker=worker, thread=thread: self._cleanup_nas_transfer_worker(thread, worker)
        )
        self._nas_transfer_threads.append(thread)
        self._nas_transfer_workers.append(worker)
        thread.start()

    def _set_nas_transfer_status(self, message: str) -> None:
        self.warning_label.setText(f"{message}. 파일 중계 전용이며 최종 OK는 차단됩니다.")
        if hasattr(self, "nas_transfer_result_label"):
            self.nas_transfer_result_label.setText(message)

    def _set_nas_transfer_result(self, result: NasFileTransferResult) -> None:
        if result.ok:
            self.instruction_label.setText(f"NAS 파일 전송 완료: {result.remote_dir}")
            self._set_nas_transfer_status(result.summary)
        else:
            self.instruction_label.setText("NAS 파일 전송 실패")
            self._set_nas_transfer_status(f"{result.summary} · {result.error}")
        if hasattr(self, "nas_transfer_history"):
            stamp = datetime.now().strftime("%H:%M:%S")
            if result.ok:
                names = ", ".join(artifact.relative_path for artifact in result.artifacts)
                self.nas_transfer_history.appendPlainText(
                    f"[{stamp}] 성공 {result.remote_dir} · {result.summary} · {names}"
                )
            else:
                self.nas_transfer_history.appendPlainText(f"[{stamp}] 실패 {result.error} · {result.summary}")
        if result.ok:
            # Sent files leave the pending list so a second click does not re-upload them.
            self._nas_transfer_files = []
            widget = getattr(self, "nas_transfer_list", None)
            if widget is not None:
                widget.clear()

    def _refresh_nas_transfer_buttons(self) -> None:
        idle = not self._nas_transfer_running
        for name in ("nas_transfer_pick_button", "nas_transfer_clear_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(idle)
        send = getattr(self, "nas_transfer_send_button", None)
        if send is not None:
            send.setEnabled(idle and bool(self._nas_transfer_files))

    def _stop_nas_file_transfer(self) -> None:
        for worker in tuple(self._nas_transfer_workers):
            worker.stop()

    def _cleanup_nas_transfer_worker(self, thread: QThread, worker: NasFileTransferWorker) -> None:
        if thread in self._nas_transfer_threads:
            self._nas_transfer_threads.remove(thread)
        if worker in self._nas_transfer_workers:
            self._nas_transfer_workers.remove(worker)
        if not self._nas_transfer_workers:
            self._nas_transfer_running = False
            self._refresh_nas_transfer_buttons()

    # ---- camera intrinsics calibration (operator console only) ----------------------------

    def _populate_calibration_cameras(self) -> None:
        box = getattr(self, "calibration_camera_box", None)
        if box is None:
            return
        box.blockSignals(True)
        box.clear()
        if self.settings is not None:
            preferred = self.settings.vehicle_envelope.front_left_role
            for camera in self.settings.active_cameras:
                box.addItem(f"{camera.id} ({camera.role.value})", camera.role.value)
            index = box.findData(preferred.value)
            if index >= 0:
                box.setCurrentIndex(index)
        else:
            box.addItem("front (front)", CameraRole.front.value)
        box.blockSignals(False)
        self._sync_calibration_layout()

    def _calibration_role(self) -> CameraRole:
        box = getattr(self, "calibration_camera_box", None)
        data = box.currentData() if box is not None else None
        try:
            return CameraRole(data) if data else CameraRole.front
        except ValueError:
            return CameraRole.front

    def _calibration_camera(self):  # noqa: ANN202 - CameraConfig | None
        if self.settings is None:
            return None
        role = self._calibration_role()
        for camera in self.settings.active_cameras:
            if camera.role is role:
                return camera
        return None

    def _sync_calibration_layout(self) -> None:
        self._camera_page_layouts["카메라 캘리브레이션"] = f"single:{self._calibration_role().value}"

    def _on_calibration_camera_changed(self, _index: int = 0) -> None:
        if self._calib_running:
            # The session is bound to one camera; switching would mix intrinsics.
            self._stop_calibration_session()
        self._sync_calibration_layout()
        page = self.operator_pages.get("카메라 캘리브레이션")
        if page is not None and self.operator_workspace_stack.currentWidget() is page:
            self._adopt_camera_area(page, self._camera_page_layouts["카메라 캘리브레이션"])

    def _refresh_calibration_page(self) -> None:
        self._sync_calibration_layout()
        page = self.operator_pages.get("카메라 캘리브레이션")
        if page is not None:
            self._adopt_camera_area(page, self._camera_page_layouts["카메라 캘리브레이션"])
        self._refresh_calibration_buttons()

    def _refresh_calibration_buttons(self) -> None:
        running, calibrating = self._calib_running, self._calib_calibrating
        poses_left = self._calib_pose_index < len(CAPTURE_POSES)
        for name, enabled in (
            ("calibration_start_button", not running and not calibrating),
            ("calibration_capture_button", running and poses_left and not calibrating),
            ("calibration_skip_button", running and poses_left and not calibrating),
            ("calibration_run_button", len(self._calib_samples) >= CALIBRATION_MIN_SAMPLES and not calibrating),
            ("calibration_stop_button", running and not calibrating),
            ("calibration_camera_box", not running and not calibrating),
            ("calibration_verify_button", not calibrating),
            ("calibration_share_button", not calibrating and not self._calib_sharing),
            ("calibration_fetch_button", not calibrating and not self._calib_sharing),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(enabled)

    def _set_calibration_status(self, message: str) -> None:
        if hasattr(self, "calibration_status_label"):
            self.calibration_status_label.setText(message)
        self.warning_label.setText(f"{message}. 측정 전용이며 최종 OK는 차단됩니다.")

    def _log_calibration(self, message: str) -> None:
        if hasattr(self, "calibration_history"):
            self.calibration_history.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _calibration_frame_aspect(self) -> float:
        """height/width of the calibration camera's frame; falls back to 16:9."""
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        frame = widget.current_frame() if widget is not None else None
        if frame is not None and frame.width() > 0:
            return frame.height() / float(frame.width())
        return 9.0 / 16.0

    def _paint_calibration_target(self, widget, pose, *, state: str, progress: float) -> None:  # noqa: ANN001
        caption = {"waiting": "여기에 체커판을 맞추세요", "holding": "그대로 유지", "captured": "촬영"}.get(state, "")
        widget.set_target_box(
            pose.target_rect(self._calibration_frame_aspect()),
            state=state,
            progress=progress,
            caption=caption,
        )

    def _clear_calibration_target(self) -> None:
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        if widget is not None:
            widget.set_target_box(None)

    def _show_calibration_pose(self) -> None:
        label = getattr(self, "calibration_instruction_label", None)
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        if self._calib_pose_index >= len(CAPTURE_POSES):
            if widget is not None:
                widget.set_target_box(None)
            if label is not None:
                label.setText(f"모든 자세 완료 ({len(self._calib_samples)}장). 측정 실행을 누르세요.")
            return
        pose = CAPTURE_POSES[self._calib_pose_index]
        if widget is not None:
            self._paint_calibration_target(widget, pose, state="waiting", progress=0.0)
        if label is not None:
            hint = f" — {pose.hint}" if pose.hint else ""
            label.setText(
                f"[{self._calib_pose_index + 1}/{len(CAPTURE_POSES)}] {pose.instruction}{hint} "
                f"(상자 안에서 {CALIBRATION_DWELL_SECONDS:.0f}초 유지하면 자동 촬영)"
            )

    def _start_calibration_session(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked or self._calib_running or self._calib_calibrating:
            return
        camera = self._calibration_camera()
        if camera is None:
            self._set_calibration_status("설정된 카메라가 없어 캘리브레이션을 시작할 수 없습니다")
            return
        widget = self.camera_widgets.get(camera.role)
        if widget is None or self._runtime_camera_status.get(camera.id) != "정상 수신":
            self._set_calibration_status(f"{camera.id} 카메라가 수신 중이 아닙니다. 전체 카메라에서 수신을 확인해 주세요")
            return
        root = (
            self.settings.calibration_path.parent / "intrinsics" if self.settings is not None else DEFAULT_INTRINSICS_ROOT
        )
        self._calib_store = IntrinsicsSessionStore(root, camera.id)
        self._calib_samples = []
        self._calib_pose_index = 0
        self._calib_camera_id = camera.id
        self._calib_dwell_start = None
        self._calib_hold_until = 0.0
        self._calib_capture_requested = False
        self._calib_running = True
        self._ensure_calibration_detect_worker()
        timer = QTimer(self)
        timer.setInterval(CALIBRATION_DETECT_INTERVAL_MS)
        timer.timeout.connect(self._calibration_tick)
        self._calib_timer = timer
        timer.start()
        self._show_calibration_pose()
        self._set_calibration_status(f"{camera.id} 촬영 세션 시작. 체커보드를 지시된 자세로 들어 주세요")
        self._log_calibration(f"세션 시작 camera={camera.id} dir={self._calib_store.session_dir}")
        self._refresh_calibration_buttons()

    def _ensure_calibration_detect_worker(self) -> None:
        if self._calib_detect_worker is not None:
            return
        thread = QThread(self)
        worker = CheckerboardDetectWorker(self._calib_spec)
        worker.moveToThread(thread)
        worker.detected.connect(self._on_checkerboard_detected)
        self._calib_detect_thread = thread
        self._calib_detect_worker = worker
        thread.start()

    def _calibration_tick(self) -> None:
        if not self._calib_running or self._calib_detect_busy or self._calib_detect_worker is None:
            return
        if self._calib_pose_index >= len(CAPTURE_POSES):
            return
        if time.monotonic() < self._calib_hold_until:
            return
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        frame = widget.current_frame() if widget is not None else None
        if frame is None:
            return
        self._calib_detect_busy = True
        self._calib_token += 1
        bgr = _qimage_to_bgr(frame)
        # Signal → queued slot so the detector runs on its own thread.
        self._calib_detect_worker.detect_requested.emit(self._calib_token, bgr)

    def _on_checkerboard_detected(self, token: int, bgr, detection) -> None:  # noqa: ANN001
        self._calib_detect_busy = False
        if not self._calib_running or token != self._calib_token:
            return
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        pose = CAPTURE_POSES[self._calib_pose_index]
        if detection is None:
            self._calib_dwell_start = None
            if widget is not None:
                widget.set_marker_points(())
                self._paint_calibration_target(widget, pose, state="waiting", progress=0.0)
            self._set_calibration_status("체커보드가 보이지 않습니다. 보드 전체가 화면에 들어오게 해 주세요")
            return
        if widget is not None:
            widget.set_marker_points(detection.normalized)

        fit = evaluate_pose_fit(pose, detection, spec=self._calib_spec)
        auto = getattr(self, "calibration_auto_box", None)
        auto_enabled = auto.isChecked() if auto is not None else True

        if self._calib_capture_requested:  # 수동 촬영은 상자 조건을 건너뛴다
            self._accept_calibration_sample(bgr, detection)
            return
        if not auto_enabled:
            self._calib_dwell_start = None
            if widget is not None:
                self._paint_calibration_target(widget, pose, state="waiting", progress=0.0)
            self._set_calibration_status("자동 촬영이 꺼져 있습니다. '지금 촬영'을 눌러 주세요")
            return
        if not fit.ok:
            self._calib_dwell_start = None
            if widget is not None:
                self._paint_calibration_target(widget, pose, state="waiting", progress=0.0)
            self._set_calibration_status(f"{fit.reason} (상자 채움 {fit.fill * 100:.0f}%)")
            return

        now = time.monotonic()
        if self._calib_dwell_start is None:
            self._calib_dwell_start = now
        held = now - self._calib_dwell_start
        if held < CALIBRATION_DWELL_SECONDS:
            remaining = CALIBRATION_DWELL_SECONDS - held
            if widget is not None:
                self._paint_calibration_target(
                    widget, pose, state="holding", progress=held / CALIBRATION_DWELL_SECONDS
                )
            self._set_calibration_status(f"상자 안에 들어왔습니다. {remaining:.1f}초 그대로 유지")
            return
        if widget is not None:
            self._paint_calibration_target(widget, pose, state="captured", progress=1.0)
        self._accept_calibration_sample(bgr, detection)

    def _accept_calibration_sample(self, bgr, detection: CheckerboardDetection) -> None:  # noqa: ANN001
        if self._calib_store is None or self._calib_pose_index >= len(CAPTURE_POSES):
            return
        pose = CAPTURE_POSES[self._calib_pose_index]
        try:
            sample = self._calib_store.save_sample(len(self._calib_samples) + 1, pose.key, bgr, detection)
        except OSError as exc:
            self._set_calibration_status(f"프레임 저장 실패: {exc}")
            return
        self._calib_samples.append(sample)
        self._calib_capture_requested = False
        self._calib_dwell_start = None
        self._calib_hold_until = time.monotonic() + CALIBRATION_HOLD_SECONDS
        self._log_calibration(
            f"촬영 {len(self._calib_samples)} pose={pose.key} coverage={detection.coverage:.2f} {sample.image_path.name}"
        )
        self._calib_pose_index += 1
        self._show_calibration_pose()
        if self._calib_pose_index >= len(CAPTURE_POSES):
            self._set_calibration_status(f"촬영 완료 {len(self._calib_samples)}장. 측정 실행을 누르세요")
        else:
            self._set_calibration_status(f"{len(self._calib_samples)}장 저장. 다음 자세로 이동해 주세요")
        self._refresh_calibration_buttons()

    def _request_calibration_capture(self, checked: bool = False) -> None:
        del checked
        if self._calib_running:
            self._calib_capture_requested = True
            self._calib_hold_until = 0.0
            self._set_calibration_status("다음 인식 프레임을 저장합니다")

    def _skip_calibration_pose(self, checked: bool = False) -> None:
        del checked
        if not self._calib_running or self._calib_pose_index >= len(CAPTURE_POSES):
            return
        skipped = CAPTURE_POSES[self._calib_pose_index]
        self._calib_pose_index += 1
        self._calib_dwell_start = None
        self._log_calibration(f"건너뜀 pose={skipped.key}")
        self._show_calibration_pose()
        self._refresh_calibration_buttons()

    def _run_calibration(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked or self._calib_calibrating or self._calib_store is None:
            return
        if len(self._calib_samples) < CALIBRATION_MIN_SAMPLES:
            self._set_calibration_status(f"샘플 부족: {len(self._calib_samples)}/{CALIBRATION_MIN_SAMPLES}")
            return
        camera = self._calibration_camera()
        rotation = camera.rotation_degrees if camera is not None else 0
        self._calib_calibrating = True
        self._stop_calibration_timer()
        self._refresh_calibration_buttons()
        self._set_calibration_status(f"{len(self._calib_samples)}장으로 측정 중")
        thread = QThread(self)
        worker = IntrinsicsCalibrateWorker(
            self._calib_store,
            tuple(self._calib_samples),
            self._calib_spec,
            camera_id=self._calib_camera_id,
            rotation_degrees=rotation,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_ready.connect(self._on_calibration_result)
        worker.failed.connect(self._on_calibration_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_calibration_worker(thread, worker))
        self._calib_threads.append(thread)
        self._calib_workers.append(worker)
        thread.start()

    def _on_calibration_result(self, result: IntrinsicsResult, result_path, latest_path) -> None:  # noqa: ANN001
        self._set_calibration_status(f"측정 완료: {result.summary()} → {latest_path}")
        self._log_calibration(f"측정 완료 {result.summary()} 저장 {latest_path} (reviewed=false)")
        self.instruction_label.setText(f"카메라 캘리브레이션 측정 완료 ({result.quality})")
        self._clear_calibration_target()
        self._show_calibration_quality(result)

    # --- 결과 확인 -------------------------------------------------------------------
    #
    # 측정이 '잘 됐는지'를 운영자가 판단할 두 가지 근거를 준다: 항목별 점검표(숫자)와
    # 왜곡 보정 전후 비교 이미지(눈). 둘 다 확인 전용이며 캘리브레이션을 유효로 만들지 않는다.

    CALIBRATION_GRADE_TEXT = {
        "good": "양호",
        "acceptable": "사용 가능",
        "poor": "재촬영 권장",
        "suspicious": "의심 — 다시 촬영하세요",
    }

    def _show_calibration_quality(self, result: IntrinsicsResult) -> None:
        label = getattr(self, "calibration_quality_label", None)
        if label is None:
            return
        grade, lines = result.quality_report()
        head = f"[{self.CALIBRATION_GRADE_TEXT.get(grade, grade)}] {result.summary()}"
        label.setText(head + "\n" + "\n".join(lines))
        self._log_calibration(f"품질 {grade}: " + " / ".join(lines))


    # ---- 캘리브레이션 공유 (NAS) -----------------------------------------------------------
    #
    # 체커보드는 현장에서 들 수 없다. 개발기에서 측정한 렌즈 값을 NAS에 올려 현장기가 가져간다.
    # 가져온 파일은 원래 측정 장비 이름을 그대로 유지하므로, 아래 `_intrinsics_for_camera`가
    # 이 장비 것이 아님을 알아보고 '빌려 씀'으로 표시한다.

    def _calibration_root(self) -> Path:
        return Path(
            self.settings.calibration_path.parent if self.settings is not None else Path("data/calibration")
        )

    def _nas_config(self):  # noqa: ANN201 - RawStorageConfig | None
        raw = getattr(self.settings, "raw_storage", None) if self.settings is not None else None
        if raw is None or not getattr(raw, "nas_host", ""):
            return None
        return raw

    def _start_calibration_share(self, mode: str, entries: tuple = ()) -> None:
        config = self._nas_config()
        if config is None:
            self._set_calibration_status("NAS 설정(SYNOLOGY_NAS_*)이 없어 공유할 수 없습니다")
            return
        self._calib_sharing = True
        self._refresh_calibration_buttons()
        thread = QThread(self)
        worker = CalibrationShareWorker(mode, config, self._calibration_root(), entries)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status_changed.connect(self._set_calibration_status)
        worker.result_ready.connect(self._on_calibration_share_result)
        worker.listing_ready.connect(self._on_calibration_listing)
        worker.finished.connect(thread.quit)
        thread.finished.connect(
            lambda worker=worker, thread=thread: self._cleanup_calibration_share(thread, worker)
        )
        self._calib_share_threads.append(thread)
        self._calib_share_workers.append(worker)
        thread.start()

    def _cleanup_calibration_share(self, thread: QThread, worker: object) -> None:
        if thread in self._calib_share_threads:
            self._calib_share_threads.remove(thread)
        if worker in self._calib_share_workers:
            self._calib_share_workers.remove(worker)
        if not self._calib_share_workers:
            self._calib_sharing = False
            self._refresh_calibration_buttons()

    def _publish_calibration(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked or self._calib_sharing:
            return
        entries = local_entries(self._calibration_root())
        if not entries:
            self._set_calibration_status("공유할 측정 파일이 없습니다. 먼저 촬영하고 측정 실행을 누르세요")
            return
        self._log_calibration(f"NAS 공유 시작 {len(entries)}개: " + ", ".join(e.label for e in entries))
        self._start_calibration_share("publish")

    def _fetch_calibration(self, checked: bool = False) -> None:
        del checked
        if not self._operator_unlocked or self._calib_sharing:
            return
        self._start_calibration_share("list")

    def _on_calibration_listing(self, entries) -> None:  # noqa: ANN001 - tuple[CalibrationEntry, ...]
        host = socket.gethostname()
        offered = [entry for entry in entries if entry.source_host != host]
        if not offered:
            self._set_calibration_status("NAS에 다른 장비가 올린 측정 파일이 없습니다")
            self._log_calibration("NAS 가져오기: 받을 것이 없음")
            return
        # 카메라·종류별로 가장 최근 것 하나씩만 가져온다 (목록은 최신순).
        latest: dict[tuple[str, str], object] = {}
        for entry in offered:
            latest.setdefault((entry.kind, entry.camera_id), entry)
        chosen = tuple(latest.values())
        self._log_calibration(
            f"NAS 가져오기 {len(chosen)}개: " + ", ".join(entry.describe(local_host=host) for entry in chosen)
        )
        self._start_calibration_share("fetch", chosen)

    def _on_calibration_share_result(self, result) -> None:  # noqa: ANN001 - CalibrationShareResult
        if result.ok:
            self._set_calibration_status(result.summary)
        else:
            self._set_calibration_status(f"{result.summary}: {result.error}" if result.error else result.summary)
        self._log_calibration(f"공유 결과 ok={result.ok} {result.summary} {result.error}".strip())
        camera = self._calibration_camera()
        if camera is not None:
            self._show_calibration_saved(camera.id)

    def _show_calibration_saved(self, camera_id: str) -> None:
        """Refresh the quality panel from whatever measurement is on disk for this camera."""
        result = self._latest_intrinsics_result(camera_id)
        if result is not None:
            self._show_calibration_quality(result)

    def _latest_intrinsics_result(self, camera_id: str) -> IntrinsicsResult | None:
        root = (
            self.settings.calibration_path.parent / "intrinsics"
            if self.settings is not None
            else DEFAULT_INTRINSICS_ROOT
        )
        path = Path(root) / f"{camera_id}.json"
        if not path.is_file():
            return None
        try:
            return result_from_dict(load_intrinsics(path))
        except (OSError, ValueError, KeyError) as exc:
            self._log_calibration(f"측정 파일을 읽지 못했습니다 {path}: {exc}")
            return None

    def _verify_calibration(self, checked: bool = False) -> None:
        del checked
        camera = self._calibration_camera()
        if camera is None:
            self._set_calibration_status("설정된 카메라가 없습니다")
            return
        result = self._latest_intrinsics_result(camera.id)
        if result is None:
            self._set_calibration_status(f"{camera.id} 측정 파일이 없습니다. 먼저 촬영하고 측정 실행을 눌러 주세요")
            return
        self._show_calibration_quality(result)

        widget = self.camera_widgets.get(camera.role)
        frame = widget.current_frame() if widget is not None else None
        if frame is None:
            self._set_calibration_status(f"{camera.id} 화면이 수신 중이 아니라 보정 비교 이미지를 만들 수 없습니다")
            return
        try:
            comparison = build_verification_image(_qimage_to_bgr(frame), result)
        except (ValueError, ImportError) as exc:
            self._set_calibration_status(f"보정 비교 이미지를 만들지 못했습니다: {exc}")
            return

        view = getattr(self, "calibration_verify_view", None)
        if view is not None:
            pixmap = QPixmap.fromImage(_bgr_to_qimage(comparison))
            width = max(view.width() or 0, 640)
            view.setPixmap(pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))
            view.setVisible(True)
        caption = getattr(self, "calibration_verify_caption", None)
        if caption is not None:
            caption.setVisible(True)

        try:
            import cv2

            root = Path(
                self.settings.calibration_path.parent / "intrinsics"
                if self.settings is not None
                else DEFAULT_INTRINSICS_ROOT
            )
            root.mkdir(parents=True, exist_ok=True)
            saved = root / f"{camera.id}-verify.png"
            cv2.imwrite(str(saved), comparison)
            self._log_calibration(f"보정 비교 이미지 저장 {saved}")
        except (OSError, ImportError) as exc:
            self._log_calibration(f"보정 비교 이미지 저장 실패: {exc}")
        self._set_calibration_status(f"{camera.id} 결과 확인 — 격자와 실제 직선이 나란한지 보세요 (확인 전용)")

    def _on_calibration_failed(self, error: str) -> None:
        self._set_calibration_status(f"측정 실패: {error}")
        self._log_calibration(f"측정 실패 {error}")

    def _cleanup_calibration_worker(self, thread: QThread, worker: IntrinsicsCalibrateWorker) -> None:
        if thread in self._calib_threads:
            self._calib_threads.remove(thread)
        if worker in self._calib_workers:
            self._calib_workers.remove(worker)
        if not self._calib_workers:
            self._calib_calibrating = False
            self._calib_running = False
            self._refresh_calibration_buttons()

    def _stop_calibration_timer(self) -> None:
        if self._calib_timer is not None:
            self._calib_timer.stop()
            self._calib_timer.deleteLater()
            self._calib_timer = None

    def _stop_calibration_session(self, checked: bool = False) -> None:
        del checked
        self._stop_calibration_timer()
        if self._calib_running:
            self._log_calibration(f"세션 종료 samples={len(self._calib_samples)}")
        self._calib_running = False
        self._calib_detect_busy = False
        camera = self._calibration_camera()
        widget = self.camera_widgets.get(camera.role) if camera is not None else None
        if widget is not None:
            widget.set_marker_points(())
            widget.set_target_box(None)
        if hasattr(self, "calibration_instruction_label") and not self._calib_calibrating:
            self.calibration_instruction_label.setText("촬영 시작을 누르면 첫 번째 자세를 안내합니다.")
        self._refresh_calibration_buttons()

    def _start_purpose_inference(self, task_id: str) -> None:
        if self.settings is None:
            self._set_purpose_buttons_checked(False)
            self.warning_label.setText("설정이 없어 목적별 AI 추론을 시작할 수 없습니다.")
            return
        if self._purpose_workers:
            # 다른 추론이 실행(또는 종료) 중이면 자동으로 중지시키고, 종료가 완료되는 즉시
            # 요청된 추론을 시작한다(_cleanup_purpose_worker가 대기 작업을 이어받는다).
            previous = self._purpose_task_label or "기존 AI 추론"
            requested = PURPOSE_TASK_SPECS[task_id]
            self._pending_user_purpose_task_id = task_id
            self._stop_purpose_inference()
            self._set_purpose_buttons_checked(True, task_id=task_id)
            self.warning_label.setText(
                f"{previous} 중지 중입니다. 종료되면 {requested.label} 추론을 자동 시작합니다. 최종 OK는 차단됩니다."
            )
            return
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
        if task_id in (PURPOSE_PERSON_PRESENCE, PURPOSE_PROCESS_MONITORING):
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
        if self._purpose_task_id == PURPOSE_PROCESS_MONITORING:
            self._monitoring_consecutive_failures = 0
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
            self.warning_label.setText("번호판 이미지 인식 결과가 없습니다. 최종 OK는 차단됩니다.")
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
        self.warning_label.setText("번호판 이미지 인식 결과입니다. 최종 OK는 차단됩니다.")
        if hasattr(self, "lpr_result_label"):
            self.lpr_result_label.setText(
                f"이미지 인식: {latest.plate_number} (conf {latest.confidence:.2f}){suffix}"
            )

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
            if raw_task_id == PURPOSE_PROCESS_MONITORING:
                # Count the cooldown from the end of the run; escalate on consecutive failures.
                self._engine_last_start_attempt = time.monotonic()
                if failed:
                    self._monitoring_consecutive_failures += 1
                elif self._purpose_task_first_inference_seconds is not None:
                    self._monitoring_consecutive_failures = 0
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

    def _purpose_buttons(self) -> tuple[tuple[str, QPushButton], ...]:
        pairs = list(self.purpose_task_buttons.items())
        for task_id, buttons in self.purpose_task_extra_buttons.items():
            pairs.extend((task_id, button) for button in buttons)
        return tuple(pairs)

    def _set_purpose_buttons_checked(self, checked: bool, *, task_id: str = "") -> None:
        for current_task_id, button in self._purpose_buttons():
            button.setChecked(checked and current_task_id == task_id)

    def _set_purpose_button_texts(self) -> None:
        for task_id, button in self._purpose_buttons():
            label = PURPOSE_TASK_SPECS[task_id].label
            running = self._purpose_task_enabled and self._purpose_task_id == task_id
            button.setText(f"{label} 중지" if running else f"{label} 시작")

    def _set_front_lpr_button_text(self) -> None:
        button = getattr(self, "front_lpr_button", None)
        if button is None:
            return
        button.setChecked(self._front_lpr_enabled)
        button.setText("정면 카메라 인식 중…" if self._front_lpr_enabled else "정면 카메라 인식")

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


def _format_ld2410_console_line(frame: LD2410Frame, client_ip: str) -> str:
    timestamp = frame.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3]
    data_type = {1: "ENGINEERING", 2: "BASIC"}.get(frame.data_type, f"TYPE-{frame.data_type}")
    target = {
        0: "대상 없음",
        1: "이동",
        2: "정지",
        3: "이동+정지",
    }.get(frame.target_status, f"알 수 없음({frame.target_status})")
    fields = [
        f"[{timestamp}] RX {client_ip}",
        data_type,
        f"대상={target}",
        f"이동={frame.moving_distance_cm}cm/E{frame.moving_energy}",
        f"정지={frame.motionless_distance_cm}cm/E{frame.motionless_energy}",
        f"감지거리={frame.detection_distance_cm}cm",
    ]
    if frame.data_type == 1:
        moving = ",".join(str(value) for value in frame.moving_gate_energy)
        motionless = ",".join(str(value) for value in frame.motionless_gate_energy)
        fields.extend((f"MG=[{moving}]", f"SG=[{motionless}]", f"조도={frame.light}", f"OUT={frame.out_pin}"))
    fields.append(f"HEX={frame.raw_hex}")
    return " | ".join(fields)


def _format_ld2410_status_line(state: str, details: dict[str, object]) -> str:
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
    labels = {
        "listening": "서버 연결 대기",
        "client_connected": "클라이언트 연결",
        "client_disconnected": "클라이언트 연결 끊김",
        "error": "서버 오류",
        "stopped": "서버 중지",
    }
    message = labels.get(state, state)
    fields = [f"[{timestamp}] STATUS {message}"]
    for key in ("bind_host", "port", "client_ip", "reason", "parse_error_count"):
        if key in details:
            fields.append(f"{key}={details[key]}")
    return " | ".join(fields)


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
    # 시안 B「패널 HMI」 토큰:
    #   바탕 #0C0F14 · 패널 #151B24 · 경계 #232C39 · 본문 #E9EDF3 · 보조 #8792A3
    #   액센트(앰버) #F5A623 · NG #E5484D · 수신중 #3DD68C
    # 사용자(드라이버) 화면의 시안/네이비 언어는 DESIGN.md에 따라 그대로 유지한다.
    return """
    QMainWindow, QWidget {
        background: #0C0F14;
        color: #E9EDF3;
        font-family: "Noto Sans CJK KR", "Noto Sans", sans-serif;
        letter-spacing: 0px;
    }
    #sidePanel {
        background: #151B24;
        border: 1px solid #232C39;
        border-radius: 12px;
    }
    #operatorPage {
        background: #151B24;
        border: 1px solid #232C39;
        border-radius: 12px;
    }
    #safetyLabel {
        min-height: 96px;
        font-size: 52px;
        font-weight: 800;
        color: #FFD9DB;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3A1418, stop:1 #2A0F13);
        border: 1px solid #B93A44;
        border-radius: 12px;
    }
    #safetyLabel[status="ready"] {
        color: #D8F7E4;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #123726, stop:1 #0D2A1D);
        border-color: #2E9E6B;
    }
    #safetyLabel[status="wait"] {
        color: #FBEFC9;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3A2F12, stop:1 #2B230E);
        border-color: #B98A2C;
    }
    #instructionLabel {
        font-size: 28px;
        font-weight: 700;
        line-height: 1.25;
        padding: 10px 16px;
        background: #151B24;
        border: 1px solid #232C39;
        border-radius: 12px;
    }
    #warningLabel {
        font-size: 19px;
        font-weight: 600;
        color: #F09A9E;
        padding: 7px 16px;
        background: #1C1216;
        border: 1px solid #4A2229;
        border-radius: 10px;
    }
    #userInstructionLabel {
        font-size: 34px;
        font-weight: 800;
        line-height: 1.25;
        padding: 10px;
        border: 1px solid #232C39;
    }
    #userWarningLabel {
        font-size: 22px;
        font-weight: 700;
        color: #F09A9E;
        padding: 8px;
        border: 1px solid #4A2229;
        background: #1C1216;
    }
    #userPlateLabel {
        min-width: 190px;
        font-size: 30px;
        font-weight: 800;
        padding: 10px;
        border: 1px solid #232C39;
        background: #10161F;
    }
    #telemetryLabel {
        font-size: 15px;
        font-family: "DejaVu Sans Mono", monospace;
        font-weight: 600;
        color: #C9D2DE;
        padding: 7px 12px;
        background: #10161F;
        border: 1px solid #232C39;
        border-radius: 8px;
    }
    #statusStrip {
        background: transparent;
    }
    #telemetryLabel[hailo="ok"] {
        color: #A7E9C9;
        border-color: #2E9E6B;
        background: #0D2A1D;
    }
    #telemetryLabel[hailo="degraded"] {
        color: #FBEFC9;
        border-color: #B98A2C;
        background: #2B230E;
    }
    #telemetryLabel[hailo="error"] {
        color: #FFD9DB;
        border-color: #B93A44;
        background: #2A0F13;
    }
    QPushButton {
        min-height: 38px;
        font-size: 16px;
        font-weight: 700;
        color: #C7D0DD;
        background: #1A212C;
        border: 1px solid #2B3646;
        border-radius: 9px;
        padding: 8px 14px;
    }
    QPushButton:hover {
        background: #212A38;
        border-color: #39465C;
        color: #E9EDF3;
    }
    QPushButton:pressed {
        background: #161D28;
    }
    QPushButton:checked {
        background: rgba(245, 166, 35, 0.14);
        border-color: rgba(245, 166, 35, 0.55);
        color: #FFD27E;
    }
    QPushButton:disabled {
        color: #5B6575;
        background: #131922;
        border-color: #1D2632;
    }
    QPushButton[primary="true"] {
        color: #14181F;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFC163, stop:1 #F5A623);
        border: 1px solid #D18A15;
    }
    QPushButton[primary="true"]:hover {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFCE82, stop:1 #FFB23B);
    }
    QPushButton[primary="true"]:checked {
        color: #FFD27E;
        background: rgba(245, 166, 35, 0.16);
        border: 1px solid rgba(245, 166, 35, 0.65);
    }
    QPushButton[primary="true"]:disabled {
        color: #5B6575;
        background: #131922;
        border-color: #1D2632;
    }
    #smallModeButton {
        min-height: 30px;
        font-size: 14px;
        padding: 4px 10px;
    }
    #smallModeButton[active="true"] {
        background: rgba(245, 166, 35, 0.14);
        border-color: rgba(245, 166, 35, 0.55);
        color: #FFD27E;
    }
    #sidebarButton {
        min-height: 34px;
        font-size: 15px;
        font-weight: 500;
        text-align: left;
        color: #8792A3;
        background: transparent;
        border: 1px solid transparent;
        border-radius: 9px;
        padding: 6px 12px;
    }
    #sidebarButton:hover {
        background: #1B2330;
        border-color: transparent;
        color: #E9EDF3;
    }
    #sidebarButton:checked {
        background: rgba(245, 166, 35, 0.12);
        border: 1px solid rgba(245, 166, 35, 0.4);
        color: #FFD27E;
        font-weight: 700;
    }
    #sidebarButton[danger="true"] {
        color: #F09A9E;
        background: transparent;
        border: 1px solid transparent;
    }
    #sidebarButton[danger="true"]:hover {
        background: #241318;
        border-color: #4A2229;
        color: #FDC7CB;
    }
    #sidebarSectionLabel {
        color: #5B6575;
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 2px;
        padding: 10px 6px 2px 6px;
        border-top: 1px solid #1B2330;
    }
    #pageTitleLabel {
        font-size: 21px;
        font-weight: 800;
        color: #F1F5F9;
    }
    #pageSubtitleLabel {
        font-size: 13px;
        color: #8792A3;
    }
    #pageStatusLabel {
        font-size: 15px;
        color: #C9D2DE;
        background: #10161F;
        border: 1px solid #232C39;
        border-radius: 8px;
        padding: 6px 12px;
    }
    #logFilterInput {
        min-height: 30px;
        font-size: 14px;
        padding: 3px 10px;
        color: #E9EDF3;
        background: #10161F;
        border: 1px solid #232C39;
        border-radius: 8px;
    }
    QCheckBox {
        font-size: 14px;
        color: #C7D0DD;
    }
    #sidebarScroll, #sidebarScrollContent {
        background: transparent;
        border: 0;
    }
    #sidebarScroll QScrollBar:vertical {
        background: transparent;
        width: 8px;
        margin: 0;
    }
    #sidebarScroll QScrollBar::handle:vertical {
        background: #2B3646;
        min-height: 28px;
        border-radius: 4px;
    }
    #sidebarScroll QScrollBar::add-line:vertical,
    #sidebarScroll QScrollBar::sub-line:vertical {
        height: 0;
    }
    #ld2410ConnectionLabel {
        min-width: 190px;
        font-size: 17px;
        font-weight: 800;
        padding: 8px 12px;
        border: 1px solid #232C39;
        border-radius: 8px;
        background: #10161F;
    }
    #ld2410ConnectionLabel[status="connected"] {
        color: #FFD27E;
        border-color: rgba(245, 166, 35, 0.5);
        background: rgba(245, 166, 35, 0.1);
    }
    #ld2410ConnectionLabel[status="waiting"] {
        color: #FBEFC9;
        border-color: #B98A2C;
        background: #2B230E;
    }
    #ld2410ConnectionLabel[status="error"] {
        color: #FFD9DB;
        border-color: #B93A44;
        background: #2A0F13;
    }
    #ld2410ConnectionLabel[status="disabled"] {
        color: #8792A3;
        border-color: #232C39;
        background: #10161F;
    }
    #ld2410SafetyNote {
        font-size: 16px;
        font-weight: 700;
        color: #FBEFC9;
        padding: 8px 12px;
        border: 1px solid #6B5119;
        border-radius: 8px;
        background: #241D0C;
    }
    #testTitleLabel {
        font-size: 24px;
        font-weight: 800;
        padding: 6px;
    }
    #testLog {
        font-size: 14px;
        font-family: "DejaVu Sans Mono", monospace;
        background: #0B1017;
        color: #C9D2DE;
        border: 1px solid #232C39;
        border-radius: 8px;
    }
    #testListScroll {
        background: transparent;
        border: 0;
    }
    """ + DRIVER_STYLESHEET


def _masked_plate(plate_number: str) -> str:
    text = (plate_number or "").strip()
    if not text or text == "미인식":
        return text
    return f"•••• {text[-4:]}"
