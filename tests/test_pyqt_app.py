import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, QSize, QThread
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from towersightai.config.settings import CameraRole, Settings
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.inference.purpose_tasks import PlateOcrEvent, PURPOSE_LPR_IMAGE
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import build_operator_display
from towersightai.ui.pyqt_app import (
    OPERATOR_PANEL_WIDTH,
    SIDEBAR_ACTION_LABELS,
    LiveDetectionWorker,
    OperatorWindow,
    PurposeInferenceWorker,
    _bbox_to_rect,
    _ai_detection_label,
    _detection_label,
    _fresh_detections,
    _legacy_ai_detection_label,
    _network_bbox_to_source_bbox,
    _purpose_detection_label,
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
    assert _ai_detection_label(("ceiling", "front"), {"ceiling": 0, "front": 7}) == "AI 추론 ON: ceiling(0), front(7)"
    assert "loading 1.2s" in _ai_detection_label(("ceiling",), {"ceiling": 0}, loading_seconds=1.2)
    assert "first inference 3.4s" in _ai_detection_label(("ceiling",), {"ceiling": 2}, first_inference_seconds=3.4)


def test_legacy_ai_detection_label_shows_counts_without_model_load_state():
    assert _legacy_ai_detection_label(()) == "이전 AI Detection OFF"
    assert _legacy_ai_detection_label(("ceiling", "front"), {"ceiling": 3, "front": 9}) == (
        "이전 AI Detection ON: ceiling(3), front(9)"
    )


def test_purpose_detection_label_shows_task_counts_and_load_time():
    assert _purpose_detection_label("차량 전용 검출", ("front",), {"front": 2}) == "차량 전용 검출 ON: front(2)"
    assert "loading 1.5s" in _purpose_detection_label("번호판 이미지 LPR", (), loading_seconds=1.5)
    assert "first inference 2.4s" in _purpose_detection_label("사람 존재 감지", ("front",), first_inference_seconds=2.4)


def test_operator_side_panel_width_is_fixed_for_long_detection_status():
    assert OPERATOR_PANEL_WIDTH == 400


def test_operator_ui_starts_on_dashboard_with_sidebar_closed():
    app = _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    assert app is not None
    assert window.stack.currentWidget() is window.operator_view
    assert window.stack.count() == 2
    assert window._camera_layout_mode == "dashboard"
    assert window.operator_sidebar.isHidden() is True
    assert tuple(button.text() for button in window.sidebar_buttons.values()) == SIDEBAR_ACTION_LABELS
    assert "운전자 화면" not in {button.text() for button in window.sidebar_buttons.values()}
    assert "이전 AI Detection" in {button.text() for button in window.sidebar_buttons.values()}
    assert "차량 전용 검출" in {button.text() for button in window.sidebar_buttons.values()}
    assert "번호판 이미지 LPR" in {button.text() for button in window.sidebar_buttons.values()}
    assert "정면카메라LPR" in {button.text() for button in window.sidebar_buttons.values()}
    assert "사람 존재 감지" in {button.text() for button in window.sidebar_buttons.values()}
    assert "AI모델 선택" not in {button.text() for button in window.sidebar_buttons.values()}
    assert "테스트" not in {button.text() for button in window.sidebar_buttons.values()}
    assert "AI Detection" not in {button.text() for button in window.sidebar_buttons.values()}
    assert "Multi-Camera Re-ID" not in {button.text() for button in window.sidebar_buttons.values()}
    assert sum(1 for button in window.sidebar_buttons.values() if button.text() == "EMPTY") == 2

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
    assert "AI 추론은 다음 시작부터 같은 회전 스트림" in window.warning_label.text()
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


def test_ai_detection_timeout_marks_selected_model_unconfirmed(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._detection_enabled = True
    window._detection_camera_ids = ("ceiling",)
    window._detection_event_counts = {"ceiling": 0}
    window._detection_model_label = "selected.hef"
    window._detection_load_started_at = 0.0
    window._detection_first_inference_seconds = None
    monkeypatch.setattr("towersightai.ui.pyqt_app.time.monotonic", lambda: 31.0)

    window._tick()

    assert "추론 미확인: selected.hef" == window.model_status_label.text()
    assert "AI 추론 실패" in window.warning_label.text()
    window.close()


def test_live_detection_worker_keeps_ui_rotation_map_for_pipeline_start():
    settings = _settings()

    worker = LiveDetectionWorker(settings, ("ceiling", "front"), camera_rotations={"ceiling": 90, "front": 0}, hef_path=Path("/tmp/selected.hef"))

    assert worker.camera_rotations == {"ceiling": 90, "front": 0}
    assert worker.hef_path == Path("/tmp/selected.hef")


def test_live_detection_worker_legacy_mode_keeps_previous_pipeline_inputs():
    settings = _settings()

    worker = LiveDetectionWorker(
        settings,
        ("ceiling", "front"),
        camera_rotations={"ceiling": 90, "front": 0},
        hef_path=Path("/tmp/selected.hef"),
        legacy_mode=True,
    )

    assert worker.legacy_mode is True
    assert worker.camera_rotations == {"ceiling": 90, "front": 0}


def test_live_detection_worker_normal_mode_reuses_previous_event_path_with_selected_hef(monkeypatch):
    settings = _settings()
    calls = []

    def fake_live_process(settings_arg, cameras_arg, **kwargs):
        calls.append((settings_arg, cameras_arg, kwargs))
        return SimpleNamespace(command=("gst-launch-1.0", f"hailonet hef-path={kwargs.get('hef_path') or settings_arg.hailo_hef_path}"), hef_path=kwargs.get("hef_path") or settings_arg.hailo_hef_path)

    monkeypatch.setattr("towersightai.ui.pyqt_app.live_multistream_detection_process", fake_live_process)
    worker = LiveDetectionWorker(
        settings,
        ("ceiling", "front"),
        camera_rotations={"ceiling": 90, "front": 0},
        hef_path=Path("/tmp/selected.hef"),
    )

    process = worker._build_process()

    assert process.hef_path == Path("/tmp/selected.hef")
    assert calls[0][2] == {"camera_rotations": {"ceiling": 90, "front": 0}, "hef_path": Path("/tmp/selected.hef")}
    assert "event_dir" not in calls[0][2]


def test_live_detection_worker_legacy_mode_uses_previous_process_without_hef_override(monkeypatch):
    settings = _settings()
    calls = []

    def fake_live_process(settings_arg, cameras_arg, **kwargs):
        calls.append((settings_arg, cameras_arg, kwargs))
        return SimpleNamespace(command=("gst-launch-1.0", f"hailonet hef-path={settings_arg.hailo_hef_path}"), hef_path=settings_arg.hailo_hef_path)

    monkeypatch.setattr("towersightai.ui.pyqt_app.live_multistream_detection_process", fake_live_process)
    worker = LiveDetectionWorker(
        settings,
        ("ceiling", "front"),
        camera_rotations={"ceiling": 90, "front": 0},
        hef_path=Path("/tmp/selected.hef"),
        legacy_mode=True,
    )

    process = worker._build_process()

    assert process.hef_path == settings.hailo_hef_path
    assert calls[0][2] == {"camera_rotations": {"ceiling": 90, "front": 0}}
    assert "hef_path" not in calls[0][2]
    assert "event_dir" not in calls[0][2]


def test_legacy_ai_detection_button_starts_previous_detection_path(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._runtime_camera_status = {"ceiling": "정상 수신", "front": "정상 수신"}

    window.sidebar_buttons["이전 AI Detection"].click()

    assert window._detection_legacy_mode is True
    assert window.legacy_ai_detection_button.isChecked() is True
    assert window.legacy_ai_detection_button.text() == "이전 AI Detection ON"
    assert not hasattr(window, "ai_detection_button")
    assert len(window._detection_workers) == 1
    assert window._detection_workers[0].legacy_mode is True
    assert window._detection_workers[0].hef_path is None
    assert window.ai_detection_label.text() == "이전 AI Detection ON: ceiling(0), front(0)"
    window.close()


def test_vehicle_purpose_button_starts_front_only_task(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._runtime_camera_status = {"front": "정상 수신", "ceiling": "정상 수신"}

    window.sidebar_buttons["차량 전용 검출"].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "vehicle_detection"
    assert window.purpose_task_buttons["vehicle_detection"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert isinstance(window._purpose_workers[0], PurposeInferenceWorker)
    assert window._purpose_workers[0].camera_ids == ("front",)
    assert window.ai_detection_label.text() == "차량 전용 검출 ON: front(0) / loading 0.0s"
    window.close()


def test_lpr_purpose_button_starts_image_task_without_camera(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)

    window.sidebar_buttons["번호판 이미지 LPR"].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "lpr_image"
    assert window.purpose_task_buttons["lpr_image"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert window._purpose_workers[0].camera_ids == ()
    assert window.ai_detection_label.text() == "번호판 이미지 LPR ON / loading 0.0s"
    window.close()


def test_lpr_result_updates_top_instruction_label_and_keeps_ok_blocked(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._purpose_task_id = PURPOSE_LPR_IMAGE

    window._set_lpr_results(
        (
            PlateOcrEvent(
                plate_number="12가3456",
                confidence=0.94,
                timestamp=datetime.now(timezone.utc),
            ),
        )
    )

    assert window.instruction_label.text() == "번호판 인식: 12가3456"
    assert "최종 OK는 차단" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_lpr_no_result_updates_top_instruction_label(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._purpose_task_id = PURPOSE_LPR_IMAGE

    window._set_lpr_results(())

    assert window.instruction_label.text() == "번호판 인식 실패: 결과 없음"
    assert "최종 OK는 차단" in window.warning_label.text()
    window.close()


def test_front_camera_lpr_runs_without_stopping_ai_detection(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    stopped = False

    def fake_stop_ai_detection(_self):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(OperatorWindow, "_stop_ai_detection", fake_stop_ai_detection)
    window = OperatorWindow(display, settings=settings)
    frame = QImage(64, 32, QImage.Format.Format_RGB32)
    frame.fill(QColor("#ffffff"))
    window.camera_widgets[CameraRole.front].set_frame(frame)
    window._detection_enabled = True
    window._detection_workers.append(object())

    window.sidebar_buttons["정면카메라LPR"].click()

    assert stopped is False
    assert window._detection_enabled is True
    assert len(window._front_lpr_workers) == 1
    assert window.front_lpr_button.isChecked() is True
    assert window.front_lpr_button.text() == "정면카메라LPR ON"
    window.close()


def test_front_camera_lpr_runs_while_purpose_worker_exists(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    frame = QImage(64, 32, QImage.Format.Format_RGB32)
    frame.fill(QColor("#ffffff"))
    window.camera_widgets[CameraRole.front].set_frame(frame)

    class ExistingWorker:
        def stop(self):
            pass

    existing_worker = ExistingWorker()
    window._purpose_workers.append(existing_worker)
    window._purpose_task_enabled = True
    window._purpose_task_id = "person_presence"

    window.sidebar_buttons["정면카메라LPR"].click()

    assert existing_worker in window._purpose_workers
    assert len(window._front_lpr_workers) == 1
    assert window._purpose_task_id == "person_presence"
    window.close()


def test_front_camera_lpr_requires_front_frame(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)

    window.sidebar_buttons["정면카메라LPR"].click()

    assert len(window._front_lpr_workers) == 0
    assert window.instruction_label.text() == "정면카메라LPR 실패: 정면 프레임 없음"
    assert window.front_lpr_button.isChecked() is False
    window.close()


def test_front_camera_lpr_result_displays_last_four_digits(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(display, settings=settings)

    window._set_front_lpr_result({"ok": True, "plate_number": "47L1972", "last4": "1972", "log_path": "log"})

    assert window.instruction_label.text() == "정면카메라LPR: 1972"
    assert "47L1972" not in window.instruction_label.text()
    assert "최종 OK는 차단" in window.warning_label.text()
    window.close()


def test_front_camera_lpr_duplicate_click_does_not_start_second_worker(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    frame = QImage(64, 32, QImage.Format.Format_RGB32)
    frame.fill(QColor("#ffffff"))
    window.camera_widgets[CameraRole.front].set_frame(frame)

    window.sidebar_buttons["정면카메라LPR"].click()
    window.sidebar_buttons["정면카메라LPR"].click()

    assert len(window._front_lpr_workers) == 1
    assert "실행 중" in window.warning_label.text()
    window.close()


def test_person_presence_purpose_button_uses_streaming_cameras(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)
    window._runtime_camera_status = {"ceiling": "정상 수신", "front": "정상 수신", "rear_side": "NG: 프레임 지연"}

    window.sidebar_buttons["사람 존재 감지"].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "person_presence"
    assert window.purpose_task_buttons["person_presence"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert window._purpose_workers[0].camera_ids == ("ceiling", "front")
    assert window.ai_detection_label.text() == "사람 존재 감지 ON: ceiling(0), front(0) / loading 0.0s"
    window.close()


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
