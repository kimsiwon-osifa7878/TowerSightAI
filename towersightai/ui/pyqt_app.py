from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from towersightai.camera.pipeline import build_preview_pipeline, normalize_rotation_degrees
from towersightai.config.settings import CameraRole, Settings
from towersightai.diagnostics import DiagnosticResult, DiagnosticStatus, DiagnosticsService
from towersightai.inference.events import DetectionEvent
from towersightai.inference.live_detection import LiveDetectionRunner, latest_events, live_multistream_detection_process
from towersightai.ui.model import GlobalSafetyStatus, OperatorDisplayModel

TEST_LIST_PANEL_WIDTH = 320
OPERATOR_PANEL_WIDTH = 400
OPERATOR_SIDEBAR_WIDTH = 300
TEST_STATUS_HEIGHT = 34
TEST_SUMMARY_MAX_HEIGHT = 112
DETECTION_TTL_SECONDS = 1.0
SIDEBAR_ACTION_LABELS = (
    "운영 대시보드",
    "전체 카메라",
    "카메라 설정",
    "테스트",
    "AI Detection",
    "차량 진입 시뮬레이션",
    "EMPTY",
    "EMPTY",
    "EMPTY",
    "EMPTY",
    "EMPTY",
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
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - exercised only on GUI runtimes.
    raise RuntimeError("PyQt6 is required to launch the TowerSightAI operator UI.") from exc


class CameraSurface(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
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

    def set_detections(self, detections: tuple[DetectionEvent, ...]) -> None:
        self._detections = detections
        self.update()

    def clear_detections(self) -> None:
        self._detections = ()
        self.update()

    def set_vehicle_simulation(self, enabled: bool) -> None:
        self._vehicle_simulation = enabled
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
        if display_frame is not None and not display_frame.isNull():
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
            box = _bbox_to_rect(detection, image_rect, source_size=frame_size)
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
            capture = cv2.VideoCapture(self.camera.rtsp_url)
        return capture, using_gstreamer


class DiagnosticWorker(QObject):
    started = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(self, service: DiagnosticsService, test_id: str, timeout_seconds: int = 10) -> None:
        super().__init__()
        self.service = service
        self.test_id = test_id
        self.timeout_seconds = timeout_seconds

    def run(self) -> None:
        self.started.emit(self.test_id)
        result = self.service.run(self.test_id, timeout_seconds=self.timeout_seconds)
        self.service.write_result(result)
        self.finished.emit(result)


class LiveDetectionWorker(QObject):
    detections_ready = pyqtSignal(str, object)
    status_changed = pyqtSignal(str, str)
    finished = pyqtSignal(str)

    def __init__(
        self,
        settings: Settings,
        camera_ids: tuple[str, ...],
        camera_rotations: dict[str, int] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.camera_ids = camera_ids
        self.camera_rotations = dict(camera_rotations or {})
        self.cameras = tuple(camera for camera in settings.cameras if camera.id in set(camera_ids))
        self._runner: LiveDetectionRunner | None = None
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        if self._runner is not None:
            self._runner.stop()

    def run(self) -> None:
        for attempt in range(2):
            if self._stop_requested:
                break
            process = live_multistream_detection_process(
                self.settings,
                self.cameras,
                camera_rotations=self.camera_rotations,
            )
            for camera_id in self.camera_ids:
                self.status_changed.emit(camera_id, "AI Detection 실행 중" if attempt == 0 else "AI Detection 재시도 중")

            def on_events(events: tuple[DetectionEvent, ...]) -> None:
                grouped: dict[str, list[DetectionEvent]] = {}
                for event in events:
                    grouped.setdefault(event.camera_id, []).append(event)
                for camera_id, camera_events in grouped.items():
                    self.detections_ready.emit(camera_id, latest_events(camera_events))

            def on_error(message: str) -> None:
                for camera_id in self.camera_ids:
                    self.status_changed.emit(camera_id, message)

            self._runner = LiveDetectionRunner(process, on_events=on_events, on_error=on_error)
            self._runner.run()
            if self._stop_requested:
                break
            QThread.msleep(500)
        for camera_id in self.camera_ids:
            self.finished.emit(camera_id)


class OperatorWindow(QMainWindow):
    def __init__(self, model: OperatorDisplayModel, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.settings = settings
        self.diagnostics = DiagnosticsService(settings) if settings is not None else None
        self.camera_widgets: dict[CameraRole, CameraSurface] = {}
        self._runtime_camera_status: dict[str, str] = {}
        self._threads: list[QThread] = []
        self._workers: list[CameraCaptureWorker] = []
        self._diagnostic_threads: list[QThread] = []
        self._diagnostic_workers: list[DiagnosticWorker] = []
        self._detection_threads: list[QThread] = []
        self._detection_workers: list[LiveDetectionWorker] = []
        self._test_buttons: dict[str, QPushButton] = {}
        self._test_rows: dict[str, QLabel] = {}
        self._detection_enabled = False
        self._detection_camera_ids: tuple[str, ...] = ()
        self._detection_event_counts: dict[str, int] = {}
        self._operator_unlocked = True
        self._vehicle_entry_simulation = False
        self._camera_layout_mode = "dashboard"
        self._camera_rotations: dict[str, int] = {
            camera.id: camera.rotation_degrees
            for camera in settings.cameras
        } if settings is not None else {}
        self.sidebar_buttons: dict[str, QPushButton] = {}
        self.camera_rotation_buttons: dict[str, QPushButton] = {}
        self.clock_label = QLabel()
        self.setWindowTitle("TowerSightAI Operator Console")
        self.setStyleSheet(_stylesheet())
        self._build()
        self.apply_model(model)
        if self.settings is not None:
            self._start_camera_capture()
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
            widget = self.camera_widgets.get(tile.role)
            if widget:
                widget.set_status(tile.status_text)
        self._refresh_runtime_health_labels()

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature.
        for worker in self._workers:
            worker.stop()
        for thread in self._threads:
            thread.quit()
            thread.wait(1500)
        for thread in self._diagnostic_threads:
            thread.quit()
            thread.wait(1500)
        self._stop_ai_detection()
        for thread in self._detection_threads:
            thread.quit()
            thread.wait(1500)
        super().closeEvent(event)

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
        for camera in self.settings.cameras:
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
        widget = self.camera_widgets.get(tile.role)
        if widget:
            widget.set_frame(frame)

    def _set_camera_status(self, camera_id: str, status: str) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        widget = self.camera_widgets.get(tile.role)
        if widget:
            widget.set_status(status)
        self._runtime_camera_status[camera_id] = status
        self._refresh_runtime_health_labels()

    def _set_camera_detections(self, camera_id: str, detections: tuple[DetectionEvent, ...]) -> None:
        tile = next((tile for tile in self.model.camera_tiles if tile.camera_id == camera_id), None)
        if tile is None:
            return
        widget = self.camera_widgets.get(tile.role)
        if widget is None:
            return
        if self._runtime_camera_status.get(camera_id, tile.status_text).startswith("NG"):
            widget.clear_detections()
            return
        self._detection_event_counts[camera_id] = self._detection_event_counts.get(camera_id, 0) + len(detections)
        if self._detection_enabled:
            self.ai_detection_label.setText(_ai_detection_label(self._detection_camera_ids, self._detection_event_counts))
        widget.set_detections(detections)

    def _set_detection_status(self, camera_id: str, message: str) -> None:
        if "실행 중" in message or "재시도 중" in message:
            self.ai_detection_label.setText(_ai_detection_label(self._detection_camera_ids, self._detection_event_counts))
            return
        self.ai_detection_label.setText(f"AI Detection 오류: {camera_id}")
        self.warning_label.setText(f"Hailo detection 문제: {message[:120]}")

    def _refresh_runtime_health_labels(self) -> None:
        blocked_tiles = tuple(
            tile
            for tile in self.model.camera_tiles
            if self._runtime_camera_status.get(tile.camera_id, tile.status_text).startswith("NG")
        )
        blocked_count = len(blocked_tiles)
        if blocked_count:
            self.camera_summary_label.setText(f"카메라 {blocked_count}/4 차단")
            self.warning_label.setText("카메라 입력 차단: " + ", ".join(tile.title for tile in blocked_tiles))
            return

        self.camera_summary_label.setText("카메라 4/4 정상")
        if self.model.plc_state.value != "CONNECTED":
            self.warning_label.setText("PLC 상태 미확인: 최종 OK 차단")
        else:
            self.warning_label.setText(self.model.warning)

    def _build(self) -> None:
        self.stack = QStackedWidget()
        self.operator_view = self._build_operator_view()
        self.settings_view = self._build_settings_view()
        self.test_view = self._build_test_view()
        self.stack.addWidget(self.operator_view)
        self.stack.addWidget(self.settings_view)
        self.stack.addWidget(self.test_view)
        self.setCentralWidget(self.stack)
        self._show_operator_dashboard()

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
        camera_area = QWidget()
        camera_area.setLayout(self.grid)
        main_layout.addWidget(camera_area, 1)

        roles = [CameraRole.ceiling, CameraRole.front, CameraRole.rear_side, CameraRole.opposite_side]
        for role in roles:
            title = next((tile.title for tile in self.model.camera_tiles if tile.role is role), role.value)
            surface = CameraSurface(title)
            self.camera_widgets[role] = surface

        status_bar = QWidget()
        status_bar.setObjectName("statusStrip")
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(8, 4, 8, 4)
        status_layout.setSpacing(8)
        self.state_label = QLabel()
        self.plc_label = QLabel()
        self.camera_summary_label = QLabel()
        self.ai_detection_label = QLabel("AI Detection OFF")
        for label in (self.state_label, self.plc_label, self.camera_summary_label, self.ai_detection_label, self.clock_label):
            label.setObjectName("telemetryLabel")
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            status_layout.addWidget(label)
        main_layout.addWidget(status_bar)

        self._set_camera_layout("dashboard")
        return root

    def _add_sidebar_buttons(self, layout: QVBoxLayout) -> None:
        empty_count = 0
        for label in SIDEBAR_ACTION_LABELS:
            button = QPushButton(label)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if label == "운영 대시보드":
                button.clicked.connect(self._show_operator_dashboard)
            elif label == "전체 카메라":
                button.clicked.connect(self._show_all_cameras)
            elif label == "카메라 설정":
                button.clicked.connect(self._show_camera_settings)
            elif label == "테스트":
                self.to_tests_button = button
                button.clicked.connect(self._show_tests)
            elif label == "AI Detection":
                self.ai_detection_button = button
                button.setCheckable(True)
                button.clicked.connect(self._toggle_ai_detection)
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
        rotation_note = QLabel("회전값은 카메라 preview 파이프라인과 AI Detection 파이프라인에 동일하게 적용됩니다.")
        rotation_note.setObjectName("warningLabel")
        rotation_note.setWordWrap(True)
        detail_layout.addWidget(rotation_note)
        detail_layout.addStretch(1)
        outer.addWidget(detail, 1)
        self._refresh_rotation_controls()
        return root

    def _build_test_view(self) -> QWidget:
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        list_panel = QWidget()
        list_panel.setObjectName("sidePanel")
        list_panel.setFixedWidth(TEST_LIST_PANEL_WIDTH)
        list_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(14, 14, 14, 14)
        list_layout.setSpacing(8)
        outer.addWidget(list_panel)

        title = QLabel("운영자 테스트")
        title.setObjectName("testTitleLabel")
        list_layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setObjectName("testListScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(8)

        if self.diagnostics is not None:
            for test in self.diagnostics.available_tests():
                button = QPushButton(test.label)
                button.setToolTip(test.description)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                button.clicked.connect(lambda _checked=False, test_id=test.test_id: self._start_diagnostic(test_id))
                status = QLabel("IDLE")
                status.setObjectName("telemetryLabel")
                status.setFixedHeight(TEST_STATUS_HEIGHT)
                status.setWordWrap(False)
                status.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                scroll_layout.addWidget(button)
                scroll_layout.addWidget(status)
                self._test_buttons[test.test_id] = button
                self._test_rows[test.test_id] = status
        else:
            disabled = QLabel("설정이 없어 테스트를 실행할 수 없습니다.")
            disabled.setObjectName("warningLabel")
            scroll_layout.addWidget(disabled)

        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        list_layout.addWidget(scroll, 1)
        back = QPushButton("운영자 화면")
        back.clicked.connect(self._show_operator)
        list_layout.addWidget(back)

        log_panel = QWidget()
        log_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.test_summary_label = QLabel("테스트 대기 중")
        self.test_summary_label.setObjectName("instructionLabel")
        self.test_summary_label.setWordWrap(True)
        self.test_summary_label.setMaximumHeight(TEST_SUMMARY_MAX_HEIGHT)
        self.test_summary_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        log_layout.addWidget(self.test_summary_label)
        self.test_log = QTextEdit()
        self.test_log.setReadOnly(True)
        self.test_log.setObjectName("testLog")
        self.test_log.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.test_log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_layout.addWidget(self.test_log, 1)
        outer.addWidget(log_panel, 1)
        return root

    def _tick(self) -> None:
        text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.clock_label.setText(text)

    def _show_operator(self) -> None:
        if not self._operator_unlocked:
            return
        self._set_camera_layout(self._camera_layout_mode)
        self.stack.setCurrentWidget(self.operator_view)

    def _show_operator_dashboard(self) -> None:
        self._operator_unlocked = True
        self._set_camera_layout("dashboard")
        self.stack.setCurrentWidget(self.operator_view)

    def _show_all_cameras(self) -> None:
        self._operator_unlocked = True
        self._set_camera_layout("all")
        self.stack.setCurrentWidget(self.operator_view)

    def _show_tests(self) -> None:
        if not self._operator_unlocked:
            return
        self.stack.setCurrentWidget(self.test_view)

    def _show_camera_settings(self) -> None:
        if not self._operator_unlocked:
            return
        self._refresh_rotation_controls()
        self.stack.setCurrentWidget(self.settings_view)

    def _unlock_operator(self) -> None:
        self._operator_unlocked = True
        if hasattr(self, "to_tests_button"):
            self.to_tests_button.setEnabled(True)
        self._show_operator_dashboard()

    def _toggle_sidebar(self) -> None:
        self.operator_sidebar.setVisible(not self.operator_sidebar.isVisible())

    def _empty_action(self, name: str) -> None:
        self.warning_label.setText(f"{name}: 아직 기능이 연결되지 않았습니다. 최종 OK 차단 상태를 유지합니다.")

    def _simulate_vehicle_entry(self) -> None:
        self._vehicle_entry_simulation = True
        for role in (CameraRole.ceiling, CameraRole.front):
            widget = self.camera_widgets.get(role)
            if widget is not None:
                widget.set_vehicle_simulation(True)
        self._show_operator_dashboard()
        self.instruction_label.setText("진입 차량 감지: 버드뷰와 정면 영상을 확인 중입니다.")
        self.warning_label.setText("차량 진입 시뮬레이션: UI 확인 전용이며 PLC OK는 차단됩니다.")

    def _rotate_camera(self, camera_id: str) -> None:
        next_rotation = _next_rotation(self._camera_rotations.get(camera_id, 0))
        self._camera_rotations[camera_id] = next_rotation
        self._refresh_rotation_controls()
        self._restart_camera_capture()
        if self._detection_enabled:
            self._stop_ai_detection()
            self.ai_detection_label.setText("AI Detection OFF: 회전 변경")
        self.warning_label.setText(
            f"{camera_id} 회전 {_rotation_label(next_rotation)} 적용. AI Detection은 다음 시작부터 같은 회전 스트림을 사용합니다."
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
            thread.wait(1500)
        self._threads.clear()
        self._workers.clear()
        self._start_camera_capture()

    def _set_camera_layout(self, mode: str) -> None:
        self._camera_layout_mode = mode
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if mode == "all":
            layout = (
                (CameraRole.ceiling, 0, 0),
                (CameraRole.front, 0, 1),
                (CameraRole.rear_side, 1, 0),
                (CameraRole.opposite_side, 1, 1),
            )
        else:
            layout = (
                (CameraRole.ceiling, 0, 0),
                (CameraRole.front, 0, 1),
            )
        for role, row, col in layout:
            widget = self.camera_widgets[role]
            if mode == "dashboard" and role is CameraRole.ceiling:
                widget.setMinimumSize(320, 520)
                widget.setMaximumWidth(520)
            else:
                widget.setMinimumSize(360, 220)
                widget.setMaximumWidth(16777215)
            self.grid.addWidget(widget, row, col)
        self.grid.setColumnStretch(0, 2 if mode == "dashboard" else 1)
        self.grid.setColumnStretch(1, 5 if mode == "dashboard" else 1)
        for row in range(2):
            self.grid.setRowStretch(row, 1 if mode == "all" or row == 0 else 0)

    def _toggle_ai_detection(self, checked: bool = False) -> None:
        del checked
        if self._detection_enabled:
            self._stop_ai_detection()
            return
        self._start_ai_detection()

    def _start_ai_detection(self) -> None:
        if self.settings is None:
            self.ai_detection_button.setChecked(False)
            self.warning_label.setText("설정이 없어 AI Detection을 시작할 수 없습니다.")
            return
        if self._detection_workers:
            self.ai_detection_button.setChecked(False)
            self.warning_label.setText("AI Detection 종료 처리 중입니다. 잠시 후 다시 시도해 주세요.")
            return
        streaming_camera_ids = _streaming_camera_ids(self.settings, self._runtime_camera_status)
        if not streaming_camera_ids:
            self.ai_detection_button.setChecked(False)
            self.ai_detection_button.setText("AI Detection")
            self.ai_detection_label.setText("AI Detection OFF")
            self.warning_label.setText("정상 스트리밍 중인 카메라가 없어 AI Detection을 시작하지 않습니다.")
            return
        self._detection_enabled = True
        self._detection_camera_ids = streaming_camera_ids
        self._detection_event_counts = {camera_id: 0 for camera_id in streaming_camera_ids}
        self.ai_detection_button.setChecked(True)
        self.ai_detection_button.setText("AI Detection ON")
        self.ai_detection_label.setText(_ai_detection_label(streaming_camera_ids, self._detection_event_counts))
        self.warning_label.setText("AI Detection 멀티스트림 실행 중: " + ", ".join(streaming_camera_ids))
        thread = QThread(self)
        worker = LiveDetectionWorker(
            self.settings,
            streaming_camera_ids,
            camera_rotations={camera_id: self._camera_rotations.get(camera_id, 0) for camera_id in streaming_camera_ids},
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.detections_ready.connect(self._set_camera_detections)
        worker.status_changed.connect(self._set_detection_status)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda worker=worker, thread=thread: self._cleanup_detection_worker(thread, worker))
        self._detection_threads.append(thread)
        self._detection_workers.append(worker)
        thread.start()

    def _stop_ai_detection(self) -> None:
        self._detection_enabled = False
        self._detection_camera_ids = ()
        self._detection_event_counts = {}
        if hasattr(self, "ai_detection_button"):
            self.ai_detection_button.setChecked(False)
            self.ai_detection_button.setText("AI Detection")
        if hasattr(self, "ai_detection_label"):
            self.ai_detection_label.setText("AI Detection OFF")
        for worker in tuple(self._detection_workers):
            worker.stop()
        for widget in self.camera_widgets.values():
            widget.clear_detections()

    def _cleanup_detection_worker(self, thread: QThread, worker: LiveDetectionWorker) -> None:
        if thread in self._detection_threads:
            self._detection_threads.remove(thread)
        if worker in self._detection_workers:
            self._detection_workers.remove(worker)

    def _start_diagnostic(self, test_id: str) -> None:
        if self.diagnostics is None:
            return
        button = self._test_buttons.get(test_id)
        if button:
            button.setEnabled(False)
        row = self._test_rows.get(test_id)
        if row:
            row.setText("RUNNING")
            row.setToolTip("테스트 실행 중")
        self.test_summary_label.setText("테스트 실행 중: " + test_id)
        self.test_log.append(f"[RUNNING] {test_id}")

        thread = QThread(self)
        worker = DiagnosticWorker(self.diagnostics, test_id, timeout_seconds=30 if test_id in {"hailo_image_smoke", "full_hardware_smoke"} else 10)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._finish_diagnostic)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._cleanup_diagnostic_worker(thread, worker))
        self._diagnostic_threads.append(thread)
        self._diagnostic_workers.append(worker)
        thread.start()

    def _finish_diagnostic(self, result: DiagnosticResult) -> None:
        status_text = result.status.value
        row = self._test_rows.get(result.test_id)
        if row:
            row.setText(_diagnostic_row_text(result))
            row.setToolTip(f"{status_text}: {result.summary}")
        button = self._test_buttons.get(result.test_id)
        if button:
            button.setEnabled(True)
        self.test_summary_label.setText(f"{result.label}: {result.summary}")
        self.test_log.append(f"[{status_text}] {result.label} ({result.duration_ms} ms)")
        self.test_log.append(result.summary)
        if result.detail:
            lines = result.detail.splitlines()
            tail = "\n".join(lines[-100:])
            self.test_log.append(tail)
        if result.status is not DiagnosticStatus.PASS:
            self.warning_label.setText(f"테스트 실패: {result.label}")

    def _cleanup_diagnostic_worker(self, thread: QThread, worker: DiagnosticWorker) -> None:
        if thread in self._diagnostic_threads:
            self._diagnostic_threads.remove(thread)
        if worker in self._diagnostic_workers:
            self._diagnostic_workers.remove(worker)


def _diagnostic_row_text(result: DiagnosticResult) -> str:
    if result.status is DiagnosticStatus.PASS:
        return f"PASS ({result.duration_ms} ms)"
    if result.status is DiagnosticStatus.FAIL:
        return f"FAIL ({result.duration_ms} ms)"
    if result.status is DiagnosticStatus.SKIP:
        return "SKIP"
    return result.status.value


def _fresh_detections(detections: tuple[DetectionEvent, ...]) -> tuple[DetectionEvent, ...]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DETECTION_TTL_SECONDS)
    return tuple(event for event in detections if event.timestamp >= cutoff)


def _streaming_camera_ids(settings: Settings, runtime_status: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        camera.id
        for camera in settings.cameras
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


def _rotate_cv_frame(cv2, frame, rotation_degrees: int):  # noqa: ANN001, ANN201 - cv2/numpy are optional runtime deps.
    rotation = normalize_rotation_degrees(rotation_degrees)
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    return frame


def _bbox_to_rect(event: DetectionEvent, image_rect: QRect, *, source_size: QSize | None = None) -> QRect | None:
    bbox = event.bbox
    if bbox.w <= 0 or bbox.h <= 0:
        return None
    x_norm, y_norm, w_norm, h_norm = _network_bbox_to_source_bbox(bbox.x, bbox.y, bbox.w, bbox.h, source_size)
    x = image_rect.left() + int(x_norm * image_rect.width())
    y = image_rect.top() + int(y_norm * image_rect.height())
    width = max(2, int(w_norm * image_rect.width()))
    height = max(2, int(h_norm * image_rect.height()))
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


def _ai_detection_label(camera_ids: tuple[str, ...], event_counts: dict[str, int] | None = None) -> str:
    if not camera_ids:
        return "AI Detection OFF"
    if event_counts is None:
        return "AI Detection ON: " + ", ".join(camera_ids)
    parts = [f"{camera_id}({event_counts.get(camera_id, 0)})" for camera_id in camera_ids]
    return "AI Detection ON: " + ", ".join(parts)


def launch_operator_ui(model: OperatorDisplayModel, settings: Settings | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = OperatorWindow(model, settings=settings)
    if model.fullscreen:
        window.showFullScreen()
    else:
        window.resize(1440, 900)
        window.show()
    return app.exec()


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
    """
