from __future__ import annotations

import sys
from datetime import datetime

from towersightai.camera.pipeline import build_preview_pipeline
from towersightai.config.settings import CameraRole, Settings
from towersightai.ui.model import GlobalSafetyStatus, OperatorDisplayModel

try:
    from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen
    from PyQt6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QSizePolicy,
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
        self.setMinimumSize(360, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def set_status(self, status: str) -> None:
        self.status = status
        self.update()

    def set_frame(self, frame: QImage) -> None:
        self._frame = frame
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
        if self._frame is not None and not self._frame.isNull():
            scaled_size = self._frame.size()
            scaled_size.scale(content.size(), Qt.AspectRatioMode.KeepAspectRatio)
            image_rect = content
            image_rect.setSize(scaled_size)
            image_rect.moveCenter(content.center())
            painter.drawImage(image_rect, self._frame)
        else:
            painter.setPen(QPen(QColor("#94a3b8"), 1))
            painter.drawLine(center_x, content.top() + 24, center_x, content.bottom() - 24)
            painter.drawLine(content.left() + 36, center_y, content.right() - 36, center_y)

        if "버드뷰" in self.title:
            painter.setPen(QPen(QColor("#facc15"), 3))
            painter.drawLine(center_x, content.top() + 40, center_x, content.bottom() - 40)
            painter.setPen(QPen(QColor("#22c55e"), 2))
            painter.drawRect(
                content.left() + width // 3,
                content.top() + int(height * 0.68),
                width // 3,
                int(height * 0.18),
            )

        painter.setPen(QColor("#e5e7eb"))
        painter.drawText(content.adjusted(12, 10, -12, -10), Qt.AlignmentFlag.AlignTop, self.title)
        painter.setPen(QColor("#f87171" if self.status.startswith("NG") else "#86efac"))
        painter.drawText(content.adjusted(12, -32, -12, -8), Qt.AlignmentFlag.AlignBottom, self.status)


class CameraCaptureWorker(QObject):
    frame_ready = pyqtSignal(str, object)
    status_changed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, settings: Settings, camera_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.camera = next(camera for camera in settings.cameras if camera.id == camera_id)
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
        pipeline = build_preview_pipeline(self.camera, resolution=self.settings.ui_camera_resolution)
        capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        using_gstreamer = capture.isOpened()
        if not using_gstreamer:
            capture.release()
            capture = cv2.VideoCapture(self.camera.rtsp_url)
        return capture, using_gstreamer


class OperatorWindow(QMainWindow):
    def __init__(self, model: OperatorDisplayModel, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.settings = settings
        self.camera_widgets: dict[CameraRole, CameraSurface] = {}
        self._runtime_camera_status: dict[str, str] = {}
        self._threads: list[QThread] = []
        self._workers: list[CameraCaptureWorker] = []
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
        super().closeEvent(event)

    def _start_camera_capture(self) -> None:
        if self.settings is None:
            return
        for camera in self.settings.cameras:
            thread = QThread(self)
            worker = CameraCaptureWorker(self.settings, camera.id)
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
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        camera_area = QWidget()
        camera_area.setLayout(self.grid)
        outer.addWidget(camera_area, 5)

        roles = [CameraRole.ceiling, CameraRole.front, CameraRole.rear_side, CameraRole.opposite_side]
        positions = [(0, 0), (0, 1), (1, 0), (1, 1)]
        for role, (row, col) in zip(roles, positions, strict=True):
            title = next((tile.title for tile in self.model.camera_tiles if tile.role is role), role.value)
            surface = CameraSurface(title)
            self.camera_widgets[role] = surface
            self.grid.addWidget(surface, row, col)

        panel = QWidget()
        panel.setObjectName("sidePanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)
        outer.addWidget(panel, 2)

        self.safety_label = QLabel("NG")
        self.safety_label.setObjectName("safetyLabel")
        self.safety_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.safety_label)

        self.instruction_label = QLabel()
        self.instruction_label.setObjectName("instructionLabel")
        self.instruction_label.setWordWrap(True)
        panel_layout.addWidget(self.instruction_label)

        self.warning_label = QLabel()
        self.warning_label.setObjectName("warningLabel")
        self.warning_label.setWordWrap(True)
        panel_layout.addWidget(self.warning_label)

        self.state_label = QLabel()
        self.plc_label = QLabel()
        self.camera_summary_label = QLabel()
        for label in (self.state_label, self.plc_label, self.camera_summary_label, self.clock_label):
            label.setObjectName("telemetryLabel")
            panel_layout.addWidget(label)
        panel_layout.addStretch(1)

        self.setCentralWidget(root)

    def _tick(self) -> None:
        self.clock_label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


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
    """
