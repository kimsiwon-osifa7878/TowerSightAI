from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QRect, QSize

from towersightai.config.settings import Settings
from towersightai.diagnostics import DiagnosticResult, DiagnosticStatus
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.ui.pyqt_app import (
    OPERATOR_PANEL_WIDTH,
    _bbox_to_rect,
    _ai_detection_label,
    _detection_label,
    _diagnostic_row_text,
    _fresh_detections,
    _network_bbox_to_source_bbox,
    _streaming_camera_ids,
)


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


def test_ai_detection_label_shows_all_target_cameras_and_counts():
    assert _ai_detection_label(("ceiling", "front"), {"ceiling": 0, "front": 7}) == "AI Detection ON: ceiling(0), front(7)"


def test_operator_side_panel_width_is_fixed_for_long_detection_status():
    assert OPERATOR_PANEL_WIDTH == 400
