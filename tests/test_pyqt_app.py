import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, QSize
from PyQt6.QtWidgets import QApplication

from towersightai.config.settings import Settings
from towersightai.diagnostics import DiagnosticResult, DiagnosticStatus
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import build_operator_display
from towersightai.ui.pyqt_app import (
    OPERATOR_PANEL_WIDTH,
    SIDEBAR_ACTION_LABELS,
    LiveDetectionWorker,
    OperatorWindow,
    _bbox_to_rect,
    _ai_detection_label,
    _detection_label,
    _diagnostic_row_text,
    _fresh_detections,
    _network_bbox_to_source_bbox,
    _rotate_cv_frame,
    _rotation_label,
    _streaming_camera_ids,
)

_APP: QApplication | None = None


def _settings() -> Settings:
    return Settings(
        tappas_workspace="/tmp/tappas",
        hailo_hef_path="/tmp/model.hef",
        hailo_postprocess_so="/tmp/post.so",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path="/tmp/calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def _qt_app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_diagnostic_row_text_stays_compact_for_failures():
    result = DiagnosticResult(
        test_id="camera_3",
        label="카메라 3 프레임 수신",
        status=DiagnosticStatus.FAIL,
        summary="timed out waiting for a frame from an unavailable camera",
        duration_ms=10027,
    )

    assert _diagnostic_row_text(result) == "FAIL (10027 ms)"
    assert result.summary not in _diagnostic_row_text(result)


def test_diagnostic_row_text_stays_compact_for_passes():
    result = DiagnosticResult(
        test_id="camera_1",
        label="카메라 1 프레임 수신",
        status=DiagnosticStatus.PASS,
        summary="프레임 수신 성공",
        duration_ms=42,
    )

    assert _diagnostic_row_text(result) == "PASS (42 ms)"


def test_detection_bbox_maps_to_image_rect():
    event = DetectionEvent(
        camera_id="front",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.25, 0.5, 0.5, 0.25),
        timestamp=datetime.now(timezone.utc),
    )

    rect = _bbox_to_rect(event, QRect(10, 20, 200, 100))

    assert rect == QRect(60, 70, 100, 25)
    assert _detection_label(event) == "car 0.84"


def test_detection_bbox_removes_square_yolo_letterbox_for_wide_source():
    event = DetectionEvent(
        camera_id="front",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.25, 0.359375, 0.5, 0.28125),
        timestamp=datetime.now(timezone.utc),
    )

    rect = _bbox_to_rect(event, QRect(10, 20, 1280, 720), source_size=QSize(1280, 720))

    assert rect == QRect(330, 200, 640, 360)
    assert _network_bbox_to_source_bbox(0.25, 0.359375, 0.5, 0.28125, QSize(1280, 720)) == (0.25, 0.25, 0.5, 0.5)


def test_detection_bbox_removes_square_yolo_letterbox_for_rotated_ceiling_source():
    event = DetectionEvent(
        camera_id="ceiling",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.359375, 0.25, 0.28125, 0.5),
        timestamp=datetime.now(timezone.utc),
    )

    rect = _bbox_to_rect(event, QRect(10, 20, 720, 1280), source_size=QSize(720, 1280))

    assert rect == QRect(190, 340, 360, 640)
    assert _network_bbox_to_source_bbox(0.359375, 0.25, 0.28125, 0.5, QSize(720, 1280)) == (0.25, 0.25, 0.5, 0.5)


def test_fresh_detections_drops_stale_events():
    fresh = DetectionEvent(
        camera_id="front",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        timestamp=datetime.now(timezone.utc),
    )
    stale = DetectionEvent(
        camera_id="front",
        label="person",
        confidence=0.91,
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    assert _fresh_detections((fresh, stale)) == (fresh,)


def test_streaming_camera_ids_include_only_live_camera_statuses():
    statuses = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "NG: 카메라 연결 이상",
        "opposite_side": "NG: 프레임 지연",
    }

    assert _streaming_camera_ids(_settings(), statuses) == ("ceiling", "front")


def test_cv_fallback_frame_rotation_matches_ui_setting():
    import cv2  # type: ignore[import-not-found]
    import numpy as np

    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    rotated = _rotate_cv_frame(cv2, frame, 90)

    assert rotated.shape == (1920, 1080, 3)


def test_ai_detection_label_shows_all_target_cameras_and_counts():
    assert _ai_detection_label(("ceiling", "front"), {"ceiling": 0, "front": 7}) == "AI Detection ON: ceiling(0), front(7)"


def test_operator_side_panel_width_is_fixed_for_long_detection_status():
    assert OPERATOR_PANEL_WIDTH == 400


def test_operator_ui_starts_on_dashboard_with_sidebar_closed():
    app = _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    assert app is not None
    assert window.stack.currentWidget() is window.operator_view
    assert window.stack.count() == 3
    assert window._camera_layout_mode == "dashboard"
    assert window.operator_sidebar.isHidden() is True
    assert tuple(button.text() for button in window.sidebar_buttons.values()) == SIDEBAR_ACTION_LABELS
    assert "운전자 화면" not in {button.text() for button in window.sidebar_buttons.values()}
    assert sum(1 for button in window.sidebar_buttons.values() if button.text() == "EMPTY") == 7

    window.sidebar_toggle_button.click()
    assert window.operator_sidebar.isHidden() is False
    window.close()


def test_camera_settings_rotation_button_updates_runtime_rotation_state():
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    window.sidebar_buttons["카메라 설정"].click()
    assert window.stack.currentWidget() is window.settings_view
    assert window._camera_rotations.get("ceiling", 0) == 0
    assert _rotation_label(0) == "0도"

    window.camera_rotation_buttons["ceiling"].click()

    assert window._camera_rotations["ceiling"] == 90
    assert "천장 버드뷰: CCW 90도 회전" in window.camera_rotation_buttons["ceiling"].text()
    assert "AI Detection은 다음 시작부터 같은 회전 스트림" in window.warning_label.text()
    window.close()


def test_operator_window_initializes_camera_rotation_from_settings(monkeypatch):
    _qt_app()
    settings = Settings(
        tappas_workspace="/tmp/tappas",
        hailo_hef_path="/tmp/model.hef",
        hailo_postprocess_so="/tmp/post.so",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a", "rotation_degrees": 90},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b", "rotation_degrees": 0},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c", "rotation_degrees": 0},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d", "rotation_degrees": 0},
        calibration_path="/tmp/calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(model, settings=settings)

    assert window._camera_rotations == {"ceiling": 90, "front": 0, "rear_side": 0, "opposite_side": 0}
    assert "천장 버드뷰: CCW 90도 회전" in window.camera_rotation_buttons["ceiling"].text()
    window.close()


def test_live_detection_worker_keeps_ui_rotation_map_for_pipeline_start():
    settings = _settings()

    worker = LiveDetectionWorker(settings, ("ceiling", "front"), camera_rotations={"ceiling": 90, "front": 0})

    assert worker.camera_rotations == {"ceiling": 90, "front": 0}


def test_vehicle_entry_simulation_is_ui_only_and_keeps_final_ok_blocked():
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    window.sidebar_buttons["차량 진입 시뮬레이션"].click()

    assert window._vehicle_entry_simulation is True
    assert "진입 차량 감지" in window.instruction_label.text()
    assert "PLC OK는 차단" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()
