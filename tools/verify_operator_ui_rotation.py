#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from towersightai.camera.pipeline import build_preview_pipeline
from towersightai.config.settings import CameraRole, Settings
from towersightai.inference.live_detection import build_live_multistream_detection_pipeline
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import build_operator_display
from towersightai.ui.pyqt_app import OperatorWindow


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp/operator-ui-verification")
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    _set_demo_frames(window)
    _grab(window, out_dir / "rotation-01-dashboard.png")

    window.sidebar_toggle_button.click()
    app.processEvents()
    _grab(window, out_dir / "rotation-02-sidebar.png")

    window.sidebar_buttons["카메라 설정"].click()
    app.processEvents()
    _grab(window, out_dir / "rotation-03-settings.png")

    window.camera_rotation_buttons["ceiling"].click()
    app.processEvents()
    _grab(window, out_dir / "rotation-04-ceiling-ccw90.png")

    rotation = window._camera_rotations["ceiling"]
    preview_pipeline = build_preview_pipeline(settings.camera_1, rotation_degrees=rotation)
    if "videoflip method=counterclockwise ! videoscale" not in preview_pipeline:
        raise AssertionError("preview pipeline did not receive the UI CCW 90 rotation")
    if "video/x-raw,width=720,height=1280" not in preview_pipeline:
        raise AssertionError("preview pipeline did not swap display resolution after CCW 90 rotation")

    with tempfile.TemporaryDirectory() as tmp:
        callbacks = {
            "ceiling": Path(tmp) / "callback_ceiling.py",
            "front": Path(tmp) / "callback_front.py",
        }
        detection_pipeline = build_live_multistream_detection_pipeline(
            settings,
            (settings.camera_1, settings.camera_2),
            callback_modules=callbacks,
            camera_rotations={"ceiling": rotation, "front": 0},
        )
    if "videoflip method=counterclockwise ! videoscale add-borders=true" not in detection_pipeline:
        raise AssertionError("AI Detection pipeline did not receive the UI CCW 90 rotation")
    if "hailo_preprocess_q_1 leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! videoflip" in detection_pipeline:
        raise AssertionError("front camera received an unexpected rotation")

    window.close()
    print(out_dir / "rotation-04-ceiling-ccw90.png")
    print("preview and AI Detection pipelines use the clicked UI rotation")
    return 0


def _settings() -> Settings:
    return Settings(
        tappas_workspace=Path("/tmp/tappas"),
        hailo_hef_path=Path("/tmp/model.hef"),
        hailo_postprocess_so=Path("/tmp/post.so"),
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://example.invalid/ceiling"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://example.invalid/front"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://example.invalid/rear"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://example.invalid/opposite"},
        calibration_path=Path("/tmp/calibration.json"),
        plc_endpoint="tcp://127.0.0.1:502",
    )


def _set_demo_frames(window: OperatorWindow) -> None:
    for role, color in (
        (CameraRole.ceiling, QColor("#1d4ed8")),
        (CameraRole.front, QColor("#166534")),
    ):
        image = QImage(640, 360, QImage.Format.Format_RGB888)
        image.fill(QColor("#0f172a"))
        painter = QPainter(image)
        painter.fillRect(40, 40, 220, 110, color)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, role.value)
        painter.end()
        window.camera_widgets[role].set_frame(image)
        window.camera_widgets[role].set_status("정상 수신")


def _grab(window: OperatorWindow, path: Path) -> None:
    pixmap = window.grab()
    if not pixmap.save(str(path)):
        raise RuntimeError(f"failed to save screenshot: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
