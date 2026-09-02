import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, QSize, QThread, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from towersightai.config.settings import CameraRole, LD2410Config, RawStorageConfig, Settings
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.inference.purpose_tasks import (
    PlateOcrEvent,
    PURPOSE_LPR_IMAGE,
    PURPOSE_PERSON_PRESENCE,
    PURPOSE_VEHICLE_DETECTION,
)
from towersightai.sensors.ld2410 import LD2410Frame
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import AlignmentResult, DriverTone, build_operator_display
from towersightai.ui.driver_view import (
    DRIVER_STYLESHEET,
    OPERATOR_BUTTON_LABEL,
    OPERATOR_HOLD_MS,
    OperatorEntryHotspot,
)
from towersightai.storage.connection_test import NasConnectionTestResult
from towersightai.ui.pyqt_app import (
    NAS_TEST_CLIP_FPS,
    NAS_TEST_CLIP_SECONDS,
    OPERATOR_PANEL_WIDTH,
    OPERATOR_SIDEBAR_WIDTH,
    LD2410_CONSOLE_MAX_LINES,
    SIDEBAR_ACTION_LABELS,
    PERSON_ALERT_STREAK_THRESHOLD,
    WINDOWED_DEFAULT_HEIGHT,
    WINDOWED_DEFAULT_WIDTH,
    WINDOWED_MAX_HEIGHT,
    WINDOWED_MAX_WIDTH,
    LiveDetectionWorker,
    OperatorWindow,
    PurposeInferenceWorker,
    _cover_source_rect,
    _bbox_to_rect,
    _ai_detection_label,
    _detection_label,
    _fresh_detections,
    _front_lpr_payload,
    _legacy_ai_detection_label,
    _network_bbox_to_source_bbox,
    _prepare_operator_window,
    _purpose_detection_label,
    _rotate_cv_frame,
    _rotation_label,
    _streaming_camera_ids,
)

_APP: QApplication | None = None


def _settings(*, birdview_mode: str = "ceiling") -> Settings:
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
        birdview_mode=birdview_mode,
    )


def _qt_app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_front_lpr_payload_returns_confidence_and_crop_bbox(tmp_path: Path):
    event_path = tmp_path / "lpr.jsonl"
    event_path.write_text(
        '\n'.join(
            (
                '{"type":"plate_ocr","plate_number":"12가3456"}',
                '{"status":"recognized","best_plate":{"plate_number":"12가3456","confidence":0.93,"bbox":{"x1":10,"y1":20,"x2":110,"y2":55}}}',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    payload = _front_lpr_payload(event_path)
    assert payload["plate_number"] == "12가3456"
    assert payload["confidence"] == 0.93
    assert payload["plate_bbox"] == {"x1": 10, "y1": 20, "x2": 110, "y2": 55}


def test_windowed_operator_window_is_capped_to_reference_and_screen():
    _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)

    _prepare_operator_window(window, fullscreen=False)

    available = window.screen().availableGeometry()
    assert window.maximumWidth() == min(WINDOWED_MAX_WIDTH, available.width())
    assert window.maximumHeight() == min(WINDOWED_MAX_HEIGHT, available.height())
    assert window.width() == min(WINDOWED_DEFAULT_WIDTH, window.maximumWidth())
    assert window.height() == min(WINDOWED_DEFAULT_HEIGHT, window.maximumHeight())
    window.close()


def test_fullscreen_window_is_real_fullscreen_with_bounded_content_canvas():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)

    _prepare_operator_window(window, fullscreen=True)
    window.showFullScreen()
    app.processEvents()

    assert window.isFullScreen() is True
    assert window.maximumWidth() > WINDOWED_MAX_WIDTH
    assert window.maximumHeight() > WINDOWED_MAX_HEIGHT
    assert window.stack.width() <= WINDOWED_MAX_WIDTH
    assert window.stack.height() <= WINDOWED_MAX_HEIGHT
    assert window.stack.geometry().center() == window.content_viewport.rect().center()
    window.close()


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


def test_cover_source_rect_crops_wide_source_from_center():
    assert _cover_source_rect(QSize(1920, 1080), QSize(400, 800)) == QRect(690, 0, 540, 1080)


def test_cover_source_rect_crops_tall_source_from_center():
    assert _cover_source_rect(QSize(720, 1280), QSize(1200, 600)) == QRect(0, 460, 720, 360)


def test_cover_bbox_maps_visible_center_crop_to_target_rect():
    event = DetectionEvent(
        camera_id="front",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.45, 0.4, 0.1, 0.2),
        timestamp=datetime.now(timezone.utc),
    )

    rect = _bbox_to_rect(
        event,
        QRect(10, 20, 400, 800),
        source_size=QSize(1920, 1080),
        source_crop_rect=QRect(690, 0, 540, 1080),
    )

    assert rect == QRect(139, 278, 142, 284)


def test_cover_bbox_returns_none_when_box_is_outside_crop():
    event = DetectionEvent(
        camera_id="front",
        label="car",
        confidence=0.84,
        bbox=BoundingBox(0.05, 0.4, 0.1, 0.2),
        timestamp=datetime.now(timezone.utc),
    )

    rect = _bbox_to_rect(
        event,
        QRect(10, 20, 400, 800),
        source_size=QSize(1920, 1080),
        source_crop_rect=QRect(690, 0, 540, 1080),
    )

    assert rect is None


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
    assert _purpose_detection_label("차량 감지", ("front",), {"front": 2}) == "차량 감지 ON: front(2)"
    assert "loading 1.5s" in _purpose_detection_label("번호판 이미지 인식", (), loading_seconds=1.5)
    assert "first inference 2.4s" in _purpose_detection_label("사람 감지", ("front",), first_inference_seconds=2.4)


def test_operator_side_panel_width_is_fixed_for_long_detection_status():
    assert OPERATOR_PANEL_WIDTH == 400


def test_operator_ui_starts_on_user_mode_with_sidebar_closed():
    app = _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    assert app is not None
    assert window.stack.currentWidget() is window.user_view
    assert window.stack.count() == 3
    assert window._camera_layout_mode == "all"
    assert not hasattr(window, "user_mode_buttons")
    assert set(window.driver_test_buttons) == {"실제 상태", "IDLE", "진입", "진입완료", "번호판인식", "주차시작"}
    assert window.driver_test_panel.isHidden() is True
    assert window.user_grid.count() == 1
    assert window.driver_view.headline_label.text() == "정지"
    assert window.driver_view.operator_hotspot.objectName() == "operatorEntryHotspot"
    assert window.camera_widgets[CameraRole.ceiling].display_mode == "cover"
    assert window.camera_widgets[CameraRole.front].display_mode == "cover"
    assert window._purpose_task_enabled is False
    assert window._purpose_workers == []

    window.driver_view.operator_hotspot.begin_hold()
    window.driver_view.operator_hotspot._complete_hold()
    assert window.stack.currentWidget() is window.operator_view
    assert window.camera_widgets[CameraRole.ceiling].display_mode == "contain"
    assert window.camera_widgets[CameraRole.front].display_mode == "contain"
    assert window.operator_sidebar.isHidden() is True
    assert tuple(button.text() for button in window.sidebar_buttons.values()) == SIDEBAR_ACTION_LABELS
    labels = {button.text() for button in window.sidebar_buttons.values()}
    assert {"사용자 화면", "주차 프로세스 테스트", "전체 카메라", "차량 감지", "사람 감지", "번호판 인식"} <= labels
    assert {"레이더 (LD2410)", "NAS 연결 확인", "시스템 점검", "실행 로그", "카메라 설정", "프로그램 종료"} <= labels
    # Task run/stop controls moved into their pages; the sidebar is navigation only.
    assert "차량 감지 시작" not in labels
    assert "사람 감지 시작" not in labels
    assert "번호판 이미지 인식 시작" not in labels
    assert "정면 카메라 인식" not in labels
    assert "이전 AI Detection" not in labels
    assert "차량 진입 시뮬레이션" not in labels
    assert "EMPTY" not in labels
    assert window.legacy_ai_detection_button.text() == "이전 AI Detection"
    assert window.front_lpr_button.text() == "정면 카메라 인식"
    assert window.vehicle_sim_button.text() == "차량 진입 시뮬레이션"

    window.sidebar_toggle_button.click()
    assert window.operator_sidebar.isHidden() is False
    window.sidebar_buttons["사용자 화면"].click()
    assert window.stack.currentWidget() is window.user_view
    assert window.camera_widgets[CameraRole.ceiling].display_mode == "cover"
    assert window.camera_widgets[CameraRole.front].display_mode == "cover"
    window.close()


def test_disabled_birdview_is_hidden_and_does_not_start_ceiling_capture(monkeypatch):
    _qt_app()
    settings = _settings(birdview_mode="disabled")
    model = build_operator_display(
        state=ParkingState.IDLE,
        cameras=settings.active_cameras,
        birdview_available=settings.birdview_enabled,
    )
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)

    assert CameraRole.ceiling not in window.camera_widgets
    assert "ceiling" not in window.camera_rotation_buttons
    assert [worker.camera.id for worker in window._workers] == ["front", "rear_side", "opposite_side"]
    assert window.camera_summary_label.text().endswith("버드뷰 OFF")
    assert "버드뷰 OFF" in window.warning_label.text()
    assert "최종 OK 차단" in window.warning_label.text()
    assert window.driver_view.display.visible_roles == (CameraRole.front,)

    window._show_operator_dashboard()
    assert window.grid.count() == 3
    assert window.grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]

    window._show_all_cameras()
    assert window.grid.count() == 3
    assert window.grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert window.grid.itemAtPosition(0, 1).widget() is window.camera_widgets[CameraRole.rear_side]
    assert window.grid.itemAtPosition(1, 1).widget() is window.camera_widgets[CameraRole.opposite_side]

    window._simulate_vehicle_entry()
    assert window.driver_view.display.visible_roles == (CameraRole.front,)
    assert "버드뷰" not in window.instruction_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_mode_restores_camera_and_overlays_after_all_camera_inspection():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    window._show_all_cameras()
    app.processEvents()
    assert window.grid.count() == 4

    revision_before_return = window.driver_view.layout_revision
    window._show_user_mode()
    app.processEvents()

    front = window.camera_widgets[CameraRole.front]
    assert window.stack.currentWidget() is window.user_view
    assert window.grid.count() == 0
    assert window.user_grid.count() == 1
    assert window.user_grid.itemAtPosition(0, 0).widget() is front
    assert front.parentWidget() is window.driver_view.camera_area
    assert front.isVisible() is True
    assert front.geometry().width() > 0
    assert front.geometry().height() > 0
    assert window.driver_view.instruction_panel.isVisible() is True
    assert window.driver_view.bottom_strip.isVisible() is True
    assert window.driver_view.layout_revision == revision_before_return + 1
    window.close()


def test_all_cameras_closes_driver_preview_and_shows_four_operator_cameras():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    window._unlock_operator()
    window.driver_test_toggle.click()
    assert window.operator_workspace_stack.currentWidget() is window.operator_pages["주차 프로세스 테스트"]
    assert window.driver_preview_host.isVisible() is True

    window.sidebar_buttons["전체 카메라"].click()
    app.processEvents()

    assert window.driver_test_toggle.isChecked() is False
    assert window.driver_test_panel.isHidden() is True
    assert window.operator_workspace_stack.currentWidget() is window.operator_pages["전체 카메라"]
    assert window.operator_camera_area.parentWidget() is window.operator_pages["전체 카메라"]
    assert window.grid.count() == 4
    assert {
        window.grid.itemAtPosition(row, column).widget()
        for row in range(2)
        for column in range(2)
    } == set(window.camera_widgets.values())
    window.close()


def test_user_mode_restores_after_person_inference_operator_flow(monkeypatch):
    app = _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window.resize(1440, 900)
    window.show()
    app.processEvents()
    window._runtime_camera_status = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "정상 수신",
        "opposite_side": "정상 수신",
    }

    window._show_operator_dashboard()
    window.purpose_task_buttons[PURPOSE_PERSON_PRESENCE].click()
    app.processEvents()
    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "person_presence"

    window._show_user_mode()
    app.processEvents()

    front = window.camera_widgets[CameraRole.front]
    assert window.stack.currentWidget() is window.user_view
    assert window.user_grid.count() == 1
    assert front.parentWidget() is window.driver_view.camera_area
    assert front.isVisible() is True
    assert window.driver_view.instruction_panel.isVisible() is True
    assert window.driver_view.display.can_show_final_ok is False
    window.close()


def test_long_inference_status_does_not_expand_user_mode_after_return():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    window._show_operator_dashboard()
    window.ai_detection_label.setText(
        "사람 감지 ON: ceiling(146), front(266), rear_side(123), "
        "opposite_side(1) / first inference 13.9s"
    )
    app.processEvents()
    window._show_user_mode()
    app.processEvents()

    assert window.width() == 1440
    assert window.minimumSizeHint().width() <= 1440
    assert window.driver_view.blocking_label.isVisible() is True
    assert window.driver_view.status_label.isVisible() is True
    window.close()


def test_operator_hotspot_requires_completed_hold_and_accepts_touch():
    _qt_app()
    hotspot = OperatorEntryHotspot()
    hotspot.show()
    hotspot._timer.setInterval(20)
    activated = QSignalSpy(hotspot.activated)

    QTest.mousePress(hotspot, Qt.MouseButton.LeftButton, pos=hotspot.rect().center())
    QTest.mouseRelease(hotspot, Qt.MouseButton.LeftButton, pos=hotspot.rect().center())
    QTest.qWait(30)
    assert len(activated) == 0

    QTest.mousePress(hotspot, Qt.MouseButton.LeftButton, pos=hotspot.rect().center())
    QTest.qWait(30)
    assert len(activated) == 1
    QTest.mouseRelease(hotspot, Qt.MouseButton.LeftButton, pos=hotspot.rect().center())

    assert OPERATOR_HOLD_MS == 2000
    assert hotspot.testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents) is True
    hotspot.close()


def test_driver_view_uses_full_camera_canvas_translucent_overlay_and_scaled_copy():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1920, 1080)
    window.show()
    app.processEvents()

    panel = window.driver_view.instruction_panel.geometry()
    assert (panel.x(), panel.y(), panel.width()) == (24, 22, 1872)
    assert window.driver_view.headline_label.font().pixelSize() == 86
    assert "rgba(2, 8, 12, 128)" in DRIVER_STYLESHEET
    assert "#driverHeadlineLabel {\n        background: transparent;" in DRIVER_STYLESHEET
    assert "#driverDetailLabel {\n        background: transparent;" in DRIVER_STYLESHEET
    assert window.driver_view.bottom_strip.height() == 42

    window.resize(1440, 900)
    app.processEvents()
    assert window.driver_view.headline_label.font().pixelSize() == 68
    window.close()


def test_large_driver_text_uses_font_metrics_without_clipping_detail():
    app = _qt_app()
    model = build_operator_display(state=ParkingState.VEHICLE_ENTERING, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1920, 1024)
    window.show()
    app.processEvents()

    view = window.driver_view
    panel = view.instruction_panel
    required_copy_height = (
        view.headline_label.fontMetrics().height()
        + view.detail_label.fontMetrics().height()
        + 4
        + 28
    )

    assert panel.height() >= required_copy_height
    assert view.headline_label.geometry().bottom() < view.detail_label.geometry().top()
    assert view.detail_label.geometry().bottom() <= panel.contentsRect().bottom()
    window.close()


def test_operator_driver_test_buttons_show_same_ratio_driver_preview(monkeypatch):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    window._unlock_operator()
    window.driver_test_toggle.click()

    preview = window.driver_preview
    assert window.operator_workspace_stack.currentWidget() is window.operator_pages["주차 프로세스 테스트"]
    assert window.driver_preview_host.isVisible() is True
    assert preview.operator_hotspot.isHidden() is True
    assert abs(preview.width() / preview.height() - 1920 / 1024) < 0.01

    frame = QImage(96, 54, QImage.Format.Format_RGB32)
    frame.fill(QColor("#123456"))
    window._set_camera_frame("front", frame)
    assert preview.camera_widgets[CameraRole.front].current_frame().size() == frame.size()

    for label in ("IDLE", "진입", "진입완료", "번호판인식", "주차시작", "실제 상태"):
        window.driver_test_buttons[label].click()
        app.processEvents()
        assert preview.display == window.driver_view.display
        assert preview.headline_label.text() == window.driver_view.headline_label.text()
        assert preview.display.can_show_final_ok is False
    window.close()


def test_repeated_camera_health_does_not_rebuild_driver_camera_layout(monkeypatch):
    _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    initial_revision = window.driver_view.layout_revision
    refresh_calls = 0
    original_refresh = window._refresh_driver_display

    def counted_refresh(*args, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(window, "_refresh_driver_display", counted_refresh)

    window._set_camera_status("front", "정상 수신")
    first_revision = window.driver_view.layout_revision
    for _ in range(20):
        window._set_camera_status("front", "정상 수신")

    assert refresh_calls == 1
    assert first_revision == initial_revision
    assert window.driver_view.layout_revision == initial_revision
    window.close()


def test_driver_warning_follows_runtime_camera_recovery_and_failure():
    _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)

    for camera_id in ("ceiling", "front", "rear_side", "opposite_side"):
        window._set_camera_status(camera_id, "정상 수신")

    assert window.camera_summary_label.text() == "카메라 4/4 정상"
    assert window.driver_view.blocking_label.text() == "PLC 상태 미확인: 최종 OK 차단"

    window._set_camera_status("rear_side", "NG: 프레임 지연")
    assert window.driver_view.blocking_label.text() == "카메라 입력 차단: 좌측면"
    assert window.driver_view.display.can_show_final_ok is False

    window._set_camera_status("rear_side", "정상 수신")
    window._set_camera_status("front", "NG: 카메라 연결 이상")
    assert window.driver_view.blocking_label.text() == "필수 카메라 입력 없음 · 주차기 동작 금지"
    assert window.driver_view.display.tone is DriverTone.DANGER
    window.close()


def test_driver_view_reflows_cameras_for_entry_alignment_and_safety_states():
    _qt_app()
    model = build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras)
    window = OperatorWindow(model)
    window._runtime_camera_status = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "정상 수신",
        "opposite_side": "정상 수신",
    }

    window._driver_state_override = ParkingState.VEHICLE_ENTERING
    window._driver_simulated = True
    window._refresh_driver_display(apply_layout=True)
    assert window.user_grid.count() == 2
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert window.user_grid.itemAtPosition(0, 1).widget() is window.camera_widgets[CameraRole.ceiling]

    window._driver_state_override = ParkingState.ALIGNMENT_GUIDE
    window._driver_alignment_override = AlignmentResult.MOVE_RIGHT
    window._refresh_driver_display(apply_layout=True)
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.ceiling]
    assert window.driver_view.headline_label.text() == "오른쪽 이동"

    window._driver_state_override = ParkingState.SAFETY_CHECK
    window._refresh_driver_display(apply_layout=True)
    assert window.user_grid.count() == 2
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert window.user_grid.itemAtPosition(0, 1).widget() is window.camera_widgets[CameraRole.ceiling]
    assert window.driver_view.display.can_show_final_ok is False
    window.close()


def test_user_idle_button_starts_person_presence_for_streaming_cameras(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "정상 수신",
        "opposite_side": "정상 수신",
    }

    window.driver_test_buttons["IDLE"].click()

    assert window._user_mode_state == "idle"
    assert window._purpose_task_id == "person_presence"
    assert window._purpose_workers[0].camera_ids == ("ceiling", "front", "rear_side", "opposite_side")
    assert window.driver_view.headline_label.text() == "진입 준비"
    assert window.user_grid.count() == 1
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert window.driver_view.display.simulated is True
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_idle_button_blocks_when_front_is_missing_even_if_another_camera_streams(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {"ceiling": "정상 수신", "front": "NG: 카메라 연결 이상"}

    window.driver_test_buttons["IDLE"].click()

    assert window._user_mode_state == "idle"
    assert window._purpose_task_id == ""
    assert window._purpose_workers == []
    assert "프론트 카메라가 정상 수신" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_idle_button_allows_front_only_person_presence(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {"front": "정상 수신"}

    window.driver_test_buttons["IDLE"].click()

    assert window._purpose_task_id == "person_presence"
    assert window._purpose_workers[0].camera_ids == ("front",)
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_entry_complete_allows_front_only_person_presence_after_vehicle_worker_stops(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {"front": "정상 수신"}
    window.driver_test_buttons["진입"].click()

    window.driver_test_buttons["진입완료"].click()
    old_thread = window._purpose_threads[0]
    old_worker = window._purpose_workers[0]
    window._cleanup_purpose_worker(old_thread, old_worker)

    assert window._purpose_task_id == "person_presence"
    assert window._purpose_workers[-1].camera_ids == ("front",)
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_entry_button_starts_front_vehicle_detection(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {"front": "정상 수신"}

    window.driver_test_buttons["진입"].click()

    assert window._user_mode_state == "entry"
    assert window._purpose_task_id == "vehicle_detection"
    assert window._purpose_workers[0].camera_ids == ("front",)
    assert window.driver_view.headline_label.text() == "천천히 진입"
    assert window.user_grid.count() == 2
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert window.user_grid.itemAtPosition(0, 1).widget() is window.camera_widgets[CameraRole.ceiling]
    window.close()


def test_user_entry_complete_switches_from_vehicle_to_pending_person_presence(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "정상 수신",
        "opposite_side": "정상 수신",
    }
    window.driver_test_buttons["진입"].click()

    window.driver_test_buttons["진입완료"].click()

    assert window._user_mode_state == "entry_complete"
    assert window._pending_user_purpose_task_id == "person_presence"
    assert window._purpose_workers[0]._stop_requested is True
    assert "기존 AI 추론 종료" in window.warning_label.text()
    window.close()


def test_user_plate_recognition_uses_front_lpr_without_stopping_purpose_worker(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    frame = QImage(64, 32, QImage.Format.Format_RGB32)
    frame.fill(QColor("#ffffff"))
    window.camera_widgets[CameraRole.front].set_frame(frame)
    window._runtime_camera_status = {"front": "정상 수신"}
    window.driver_test_buttons["진입"].click()

    window.driver_test_buttons["번호판인식"].click()

    assert window._user_mode_state == "plate_recognition"
    assert window._purpose_task_id == "vehicle_detection"
    assert len(window._front_lpr_workers) == 1
    assert window.front_lpr_button.isChecked() is True
    window._set_front_lpr_result({"ok": True, "plate_number": "47L1972", "last4": "1972", "log_path": "log"})
    assert window.user_plate_label.text() == "번호판: 1972"
    assert "47L1972" not in window.user_instruction_label.text()
    window.close()


def test_user_parking_started_stops_all_ai_and_keeps_ok_blocked(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status = {"front": "정상 수신"}
    window.driver_test_buttons["진입"].click()
    frame = QImage(64, 32, QImage.Format.Format_RGB32)
    frame.fill(QColor("#ffffff"))
    window.camera_widgets[CameraRole.front].set_frame(frame)
    window.driver_test_buttons["번호판인식"].click()

    window.driver_test_buttons["주차시작"].click()

    assert window._user_mode_state == "parking_started"
    assert window._purpose_task_enabled is False
    assert window._front_lpr_enabled is False
    assert window._purpose_workers[0]._stop_requested is True
    assert window._front_lpr_workers[0]._stop_requested is True
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_person_alert_requires_consecutive_person_detections(monkeypatch):
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    window = OperatorWindow(model, settings=settings)
    window._purpose_task_id = "person_presence"
    window._runtime_camera_status = {
        "ceiling": "정상 수신",
        "front": "정상 수신",
        "rear_side": "정상 수신",
        "opposite_side": "정상 수신",
    }
    event = DetectionEvent(
        camera_id="front",
        label="person",
        confidence=0.91,
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        timestamp=datetime.now(timezone.utc),
    )

    for _ in range(PERSON_ALERT_STREAK_THRESHOLD):
        window._set_camera_detections("front", (event,))

    assert window.driver_view.headline_label.text() == "즉시 밖으로 이동"
    assert window.user_grid.count() == 1
    assert window.user_grid.itemAtPosition(0, 0).widget() is window.camera_widgets[CameraRole.front]
    assert "최종 OK" in window.driver_view.status_label.text()
    window.close()


def test_operator_window_routes_runtime_events_to_raw_data_manager(monkeypatch, tmp_path: Path):
    _qt_app()
    calls: list[tuple[str, object]] = []

    class FakeRawDataManager:
        def __init__(self, config, camera_ids, **kwargs):
            calls.append(("init", tuple(camera_ids)))

        def record_application_started(self, *, metadata):
            calls.append(("application_started", metadata))

        def start_background_sync(self):
            calls.append(("sync", None))
            return True

        def record_detection_batch(self, camera_id, detections, *, task_id):
            calls.append(("detections", (camera_id, task_id, tuple(detections))))

        def record_vehicle_entry(self, *, camera_id, confidence=None, simulated=False, at=None):
            calls.append(("vehicle", (camera_id, simulated)))

        def record_plate(self, plate_number, **kwargs):
            calls.append(("plate", plate_number))

        def record_ai_started(self, *args, **kwargs):
            calls.append(("ai_started", args[0]))

        def record_ai_stopped(self, *args, **kwargs):
            calls.append(("ai_stopped", args[0]))

        def end_vehicle_session(self, *, reason):
            calls.append(("vehicle_ended", reason))

        def tick(self):
            return 0

        def record_application_stopped(self):
            calls.append(("application_stopped", None))

    settings = _settings()
    settings.raw_storage = RawStorageConfig(
        enabled=True,
        local_dir=tmp_path,
        nas_host="nas.example.com",
        nas_port=45222,
        nas_username="uploader",
        nas_password="test-password",
        nas_folder="/home/site",
    )
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr("towersightai.ui.pyqt_app.RawDataManager", FakeRawDataManager)
    window = OperatorWindow(model, settings=settings)
    window._runtime_camera_status["front"] = "정상 수신"
    window._purpose_task_id = "person_presence"
    event = DetectionEvent(
        camera_id="front",
        label="person",
        confidence=0.91,
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        timestamp=datetime.now(timezone.utc),
    )

    window._set_camera_detections("front", (event,))
    window._set_front_lpr_result({"ok": True, "plate_number": "12가3456", "last4": "3456"})
    window._simulate_vehicle_entry()
    window.close()

    assert ("init", ("ceiling", "front", "rear_side", "opposite_side")) in calls
    assert any(name == "detections" for name, _payload in calls)
    assert ("plate", "12가3456") in calls
    assert ("vehicle", ("front", True)) in calls
    assert ("application_stopped", None) in calls


def test_operator_window_starts_and_stops_ld2410_raw_service(monkeypatch, tmp_path: Path):
    _qt_app()
    calls: list[tuple[str, object]] = []

    class FakeLD2410Service:
        def __init__(self, config):
            calls.append(("sensor_init", (config.bind_host, config.port)))
            self.callback = None

        def set_status_callback(self, callback):
            self.callback = callback

        def set_frame_callback(self, callback):
            calls.append(("frame_callback", callback))

        def snapshot_at(self, sampled_at):
            return {"status": "unavailable", "sampled_at": sampled_at.isoformat()}

        def start(self):
            calls.append(("sensor_start", None))
            self.callback("listening", {"port": 9000})

        def stop(self):
            calls.append(("sensor_stop", None))

    class FakeRawDataManager:
        def __init__(self, config, camera_ids, *, ld2410_snapshot_provider=None):
            calls.append(("raw_provider", ld2410_snapshot_provider))

        def record_application_started(self, *, metadata):
            calls.append(("application_started", metadata))

        def record_ld2410_status(self, state, details):
            calls.append(("sensor_status", (state, dict(details))))

        def record_ai_stopped(self, task_id, **kwargs):
            calls.append(("ai_stopped", task_id))

        def start_background_sync(self):
            return True

        def record_application_stopped(self):
            calls.append(("application_stopped", None))

        def close(self):
            calls.append(("raw_close", None))

    settings = _settings()
    settings.raw_storage = RawStorageConfig(
        enabled=True,
        local_dir=tmp_path,
        nas_host="nas.example.com",
        nas_username="uploader",
        nas_password="test-password",
        nas_folder="/home/site",
    )
    settings.ld2410 = LD2410Config(enabled=True)
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr("towersightai.ui.pyqt_app.RawDataManager", FakeRawDataManager)
    monkeypatch.setattr("towersightai.ui.pyqt_app.LD2410TCPService", FakeLD2410Service)

    window = OperatorWindow(model, settings=settings)
    window.close()

    provider = next(payload for name, payload in calls if name == "raw_provider")
    assert callable(provider)
    assert ("sensor_init", ("0.0.0.0", 9000)) in calls
    assert ("sensor_start", None) in calls
    assert ("sensor_status", ("listening", {"port": 9000})) in calls
    assert calls.index(("sensor_stop", None)) < calls.index(("application_stopped", None))


def test_camera_settings_rotation_button_updates_runtime_rotation_state():
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    window._unlock_operator()
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

    window.legacy_ai_detection_button.click()

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

    window.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "vehicle_detection"
    assert window.purpose_task_buttons["vehicle_detection"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert isinstance(window._purpose_workers[0], PurposeInferenceWorker)
    assert window._purpose_workers[0].camera_ids == ("front",)
    assert window.ai_detection_label.text() == "차량 감지 ON: front(0) / loading 0.0s"
    window.close()


def test_lpr_purpose_button_starts_image_task_without_camera(monkeypatch):
    _qt_app()
    settings = _settings()
    display = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    monkeypatch.setattr(OperatorWindow, "_start_camera_capture", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self: None)
    window = OperatorWindow(display, settings=settings)

    window.purpose_task_buttons[PURPOSE_LPR_IMAGE].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "lpr_image"
    assert window.purpose_task_buttons["lpr_image"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert window._purpose_workers[0].camera_ids == ()
    assert window.ai_detection_label.text() == "번호판 이미지 인식 ON / loading 0.0s"
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

    window.front_lpr_button.click()

    assert stopped is False
    assert window._detection_enabled is True
    assert len(window._front_lpr_workers) == 1
    assert window.front_lpr_button.isChecked() is True
    assert window.front_lpr_button.text() == "정면 카메라 인식 중…"
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

    window.front_lpr_button.click()

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

    window.front_lpr_button.click()

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

    window.front_lpr_button.click()
    window.front_lpr_button.click()

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

    window.purpose_task_buttons[PURPOSE_PERSON_PRESENCE].click()

    assert window._purpose_task_enabled is True
    assert window._purpose_task_id == "person_presence"
    assert window.purpose_task_buttons["person_presence"].isChecked() is True
    assert len(window._purpose_workers) == 1
    assert window._purpose_workers[0].camera_ids == ("ceiling", "front")
    assert window.ai_detection_label.text() == "사람 감지 ON: ceiling(0), front(0) / loading 0.0s"
    window.close()


def test_vehicle_entry_simulation_is_ui_only_and_keeps_final_ok_blocked():
    _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)

    window.vehicle_sim_button.click()

    assert window._vehicle_entry_simulation is True
    assert "진입 차량 감지" in window.instruction_label.text()
    assert "PLC OK는 차단" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_ld2410_console_shows_frames_and_controls_are_display_only():
    app = _qt_app()
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    window = OperatorWindow(model)
    window.resize(1440, 900)
    window.show()
    app.processEvents()
    window._unlock_operator()
    before_ok = window.model.can_show_final_ok

    window.sidebar_buttons["레이더 (LD2410)"].click()
    frame = LD2410Frame(
        received_at=datetime.now(timezone.utc),
        data_type=2,
        target_status=2,
        moving_distance_cm=0,
        moving_energy=0,
        motionless_distance_cm=82,
        motionless_energy=31,
        detection_distance_cm=463,
        max_moving_gate=0,
        max_motionless_gate=0,
        moving_gate_energy=(0,) * 9,
        motionless_gate_energy=(31,) + (0,) * 8,
        raw_hex="F4 F3 F2 F1 0D 00 02 AA",
    )
    window._append_ld2410_frame(frame, "192.0.2.30")
    app.processEvents()

    assert window.operator_workspace_stack.currentWidget() is window.ld2410_console_view
    assert "BASIC" in window.ld2410_console.toPlainText()
    assert "대상=정지" in window.ld2410_console.toPlainText()
    assert "정지=82cm/E31" in window.ld2410_console.toPlainText()
    assert "HEX=F4 F3 F2 F1" in window.ld2410_console.toPlainText()
    assert "192.0.2.30" in window.ld2410_connection_label.text()
    assert window.model.can_show_final_ok is before_ok is False

    visible_before_pause = window.ld2410_console.toPlainText()
    window.ld2410_pause_button.click()
    window._append_ld2410_frame(frame, "192.0.2.30")
    assert window.ld2410_console.toPlainText() == visible_before_pause
    assert len(window._ld2410_console_lines) == 2
    window.ld2410_pause_button.click()
    assert window.ld2410_console.toPlainText().count("HEX=F4 F3 F2 F1") == 2

    window.ld2410_clear_button.click()
    assert window.ld2410_console.toPlainText() == ""
    assert len(window._ld2410_console_lines) == 0
    window.close()


def test_ld2410_console_keeps_only_latest_five_hundred_lines():
    _qt_app()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=_settings().cameras))

    for index in range(LD2410_CONSOLE_MAX_LINES + 7):
        window._append_ld2410_console_line(f"line-{index}")

    assert len(window._ld2410_console_lines) == LD2410_CONSOLE_MAX_LINES
    assert "line-0\n" not in window.ld2410_console.toPlainText()
    assert f"line-{LD2410_CONSOLE_MAX_LINES + 6}" in window.ld2410_console.toPlainText()
    assert window.ld2410_console.document().blockCount() == LD2410_CONSOLE_MAX_LINES
    window.close()


def test_sidebar_user_screen_test_panel_fits_window_after_empty_removal():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1440, 900)
    window.show()
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    window.driver_test_toggle.click()
    app.processEvents()

    last_button = window.driver_test_buttons["주차시작"]
    sidebar_bottom = window.operator_sidebar.rect().bottom()
    assert window.driver_test_toggle.isVisible() is True
    assert last_button.isVisible() is True
    assert last_button.mapTo(window.operator_sidebar, last_button.rect().bottomLeft()).y() <= sidebar_bottom

    window.sidebar_buttons["레이더 (LD2410)"].click()
    assert window.model.can_show_final_ok is False
    window.close()


def test_user_mode_operator_button_sits_bottom_right_and_opens_operator_mode():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1920, 1024)
    window.show()
    app.processEvents()

    view = window.driver_view
    button = view.operator_button
    assert button.text() == OPERATOR_BUTTON_LABEL
    assert button.objectName() == "driverOperatorButton"
    assert button.isVisible() is True

    geometry = button.geometry()
    assert geometry.right() <= view.width()
    assert geometry.left() > view.width() // 2
    assert geometry.bottom() <= view.height() - view.bottom_strip.height()
    assert geometry.top() > view.instruction_panel.geometry().bottom()

    button.click()
    assert window._operator_unlocked is True
    assert window.stack.currentWidget() is window.operator_view

    window.sidebar_toggle_button.click()
    window.sidebar_buttons["사용자 화면"].click()
    assert window.stack.currentWidget() is window.user_view
    assert view.operator_button.isVisible() is True
    assert view.operator_button.geometry().right() <= view.width()
    window.close()


def test_user_mode_operator_button_stays_inside_view_after_resize():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.show()
    for width, height in ((1920, 1024), (1440, 900), (1280, 720)):
        window.resize(width, height)
        app.processEvents()
        view = window.driver_view
        geometry = view.operator_button.geometry()
        assert geometry.left() >= 0
        assert geometry.top() >= 0
        assert geometry.right() <= view.width()
        assert geometry.bottom() <= view.height() - view.bottom_strip.height()
        assert geometry.width() >= 104
        assert geometry.height() >= 32
    window.close()


def test_driver_preview_in_operator_mode_has_no_operator_entry_button(monkeypatch):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1440, 900)
    window.show()
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    window.driver_test_toggle.click()
    app.processEvents()

    assert window.driver_preview.operator_button.isVisible() is False
    assert window.driver_preview.operator_button.isEnabled() is False
    assert window.driver_preview.operator_hotspot.isVisible() is False
    window.close()


def test_operator_menu_exit_button_requires_confirmation_and_keeps_ok_blocked(monkeypatch):
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1440, 900)
    window.show()
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    app.processEvents()

    exit_button = window.sidebar_buttons["프로그램 종료"]
    assert exit_button.text() == "프로그램 종료"
    assert exit_button.objectName() == "sidebarButton"
    assert exit_button.property("danger") == "true"

    monkeypatch.setattr(window, "_confirm_shutdown", lambda: False)
    exit_button.click()
    app.processEvents()
    assert window.isVisible() is True
    assert window.model.can_show_final_ok is False

    monkeypatch.setattr(window, "_confirm_shutdown", lambda: True)
    exit_button.click()
    app.processEvents()
    assert window.isVisible() is False


def test_exit_is_ignored_while_operator_mode_is_locked(monkeypatch):
    _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.show()
    confirmed: list[bool] = []
    monkeypatch.setattr(window, "_confirm_shutdown", lambda: confirmed.append(True) or True)

    window._request_shutdown()
    assert confirmed == []
    assert window.isVisible() is True
    window.close()


def test_sidebar_menu_is_scrollable_so_every_action_stays_reachable():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1280, 720)
    window.show()
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    window.driver_test_toggle.click()
    app.processEvents()

    scroll = window.sidebar_scroll
    assert scroll.widgetResizable() is True
    assert scroll.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.viewport().width() <= OPERATOR_SIDEBAR_WIDTH

    exit_button = window.sidebar_buttons["프로그램 종료"]
    scroll.ensureWidgetVisible(exit_button)
    app.processEvents()
    viewport = scroll.viewport()
    top_left = exit_button.mapTo(viewport, exit_button.rect().topLeft())
    bottom_left = exit_button.mapTo(viewport, exit_button.rect().bottomLeft())
    assert top_left.y() >= 0
    assert bottom_left.y() <= viewport.height()
    window.close()


def test_driver_camera_status_text_is_lifted_above_the_bottom_strip():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1920, 1024)
    window.show()
    app.processEvents()

    front = window.camera_widgets[CameraRole.front]
    strip_height = window.driver_view.bottom_strip.height()
    assert front.bottom_inset >= strip_height

    window.driver_view.operator_button.click()
    app.processEvents()
    assert front.bottom_inset == 0

    window.sidebar_toggle_button.click()
    window.sidebar_buttons["사용자 화면"].click()
    app.processEvents()
    assert front.bottom_inset >= strip_height
    window.close()


def _nas_settings(tmp_path, **overrides):
    raw_storage = {
        "enabled": False,
        "local_dir": tmp_path / "raw",
        "nas_host": "nas.example.test",
        "nas_username": "uploader",
        "nas_password": "secret",
        "nas_folder": "/home/site",
        "known_hosts_path": tmp_path / "known_hosts",
    }
    raw_storage.update(overrides)
    return Settings(
        tappas_workspace="/tmp/tappas",
        hailo_hef_path="/tmp/model.hef",
        hailo_postprocess_so="/tmp/post.so",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
        raw_storage=raw_storage,
    )


def _open_operator_menu(window):
    window._unlock_operator()
    window.sidebar_toggle_button.click()


def test_nas_connection_test_button_is_in_the_operator_menu():
    _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    assert "NAS 연결 확인" in window.sidebar_buttons
    assert tuple(button.text() for button in window.sidebar_buttons.values()) == SIDEBAR_ACTION_LABELS
    assert window.sidebar_buttons["NAS 연결 확인"].isEnabled() is True
    window.close()


def test_nas_connection_test_without_settings_reports_and_keeps_ok_blocked(tmp_path: Path):
    app = _qt_app()
    settings = _nas_settings(tmp_path, nas_host="", nas_username="", nas_password="", nas_folder="")
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    _open_operator_menu(window)

    window.nas_test_button.click()
    app.processEvents()

    assert window._nas_test_running is False
    assert "SYNOLOGY_NAS" in window.warning_label.text()
    assert "최종 OK는 차단" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_nas_connection_test_runs_without_camera_and_keeps_ok_blocked(monkeypatch, tmp_path: Path):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _nas_settings(tmp_path)
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    _open_operator_menu(window)

    window.nas_test_button.click()
    app.processEvents()

    assert window._nas_test_running is True
    assert window.nas_test_button.isEnabled() is False
    assert window._nas_test_camera_id == ""
    assert window._nas_test_timer is None  # no camera, so no frame collection phase
    assert len(window._nas_test_workers) == 1
    worker = window._nas_test_workers[0]
    assert worker.frames == ()
    assert worker.config.nas_folder == "/home/site"
    assert window.model.can_show_final_ok is False
    window.close()


def test_nas_connection_test_collects_frames_from_a_streaming_camera(monkeypatch, tmp_path: Path):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _nas_settings(tmp_path)
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    _open_operator_menu(window)

    frame = QImage(64, 48, QImage.Format.Format_RGB888)
    frame.fill(QColor("#204060"))
    window.camera_widgets[CameraRole.front].set_frame(frame)
    window._runtime_camera_status["front"] = "정상 수신"

    window.nas_test_button.click()
    app.processEvents()

    assert window._nas_test_camera_id == "front"
    assert window._nas_test_timer is not None
    assert window._nas_test_workers == []

    for _ in range(round(NAS_TEST_CLIP_SECONDS * NAS_TEST_CLIP_FPS)):
        window._collect_nas_test_frame()
    app.processEvents()

    assert window._nas_test_timer is None
    assert len(window._nas_test_workers) == 1
    worker = window._nas_test_workers[0]
    assert len(worker.frames) == round(NAS_TEST_CLIP_SECONDS * NAS_TEST_CLIP_FPS)
    assert worker.camera_id == "front"
    assert window.model.can_show_final_ok is False
    window.close()


def test_nas_connection_test_result_is_reported_and_never_authorizes_ok(monkeypatch, tmp_path: Path):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _nas_settings(tmp_path)
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    _open_operator_menu(window)

    window._set_nas_test_result(
        NasConnectionTestResult(
            ok=True,
            summary="NAS 저장 확인: 2개 파일 1,024B, 0.4s",
            remote_dir="/home/site/connectiontest/check-20260901-040506Z",
        )
    )
    assert "connectiontest" in window.instruction_label.text()
    assert "최종 OK는 차단" in window.warning_label.text()
    assert window.model.can_show_final_ok is False

    window._set_nas_test_result(
        NasConnectionTestResult(ok=False, summary="NAS 저장 실패", error="OSError: timed out")
    )
    assert window.instruction_label.text() == "NAS 연결 확인 실패"
    assert "OSError" in window.warning_label.text()
    assert window.model.can_show_final_ok is False
    window.close()


def test_nas_connection_test_ignores_a_second_click_while_running(monkeypatch, tmp_path: Path):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _nas_settings(tmp_path)
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    _open_operator_menu(window)

    window.nas_test_button.click()
    app.processEvents()
    window._start_nas_connection_test()
    app.processEvents()

    assert len(window._nas_test_workers) == 1
    assert "이미 실행 중" in window.warning_label.text()
    window.close()


class _FakeCapture:
    def __init__(self, opened: bool) -> None:
        self._opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def release(self) -> None:
        self.released = True


class _FakeCv2NoGstreamer:
    """cv2 stand-in whose GStreamer backend never opens (pip opencv-python wheel)."""

    CAP_GSTREAMER = 1800

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def VideoCapture(self, *args):  # noqa: N802 - mimics the cv2 API.
        self.calls.append(args)
        if len(args) == 2 and args[1] == self.CAP_GSTREAMER:
            return _FakeCapture(opened=False)
        return _FakeCapture(opened=True)


def test_capture_falls_back_to_ffmpeg_rtsp_for_every_camera_without_gstreamer():
    _qt_app()
    settings = _settings()
    fake_cv2 = _FakeCv2NoGstreamer()
    from towersightai.ui.pyqt_app import CameraCaptureWorker

    for camera_id in ("front", "rear_side", "opposite_side", "ceiling"):
        worker = CameraCaptureWorker(settings, camera_id)
        capture, using_gstreamer = worker._open_capture(fake_cv2)
        assert using_gstreamer is False
        assert capture.isOpened() is True
        # The fallback must target the camera's own RTSP URL, not an empty device.
        assert fake_cv2.calls[-1] == (worker.camera.rtsp_url,)


def test_capture_fallback_logs_once_per_worker(caplog):
    _qt_app()
    settings = _settings()
    fake_cv2 = _FakeCv2NoGstreamer()
    from towersightai.ui.pyqt_app import CameraCaptureWorker

    worker = CameraCaptureWorker(settings, "rear_side")
    with caplog.at_level("WARNING", logger="towersightai.camera.capture"):
        worker._open_capture(fake_cv2)
        worker._open_capture(fake_cv2)

    fallback_lines = [r for r in caplog.records if "gstreamer-unavailable" in r.message]
    assert len(fallback_lines) == 1
    assert "rear_side" in fallback_lines[0].getMessage()


def test_operator_console_pages_navigate_and_share_the_camera_grid():
    app = _qt_app()
    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window.resize(1440, 900)
    window.show()
    window._unlock_operator()
    app.processEvents()

    assert window.operator_workspace_stack.currentWidget() is window.operator_pages["전체 카메라"]
    assert window._camera_layout_mode == "all"

    window.sidebar_toggle_button.click()
    for label, expected_mode in (("차량 감지", "front"), ("사람 감지", "all"), ("번호판 인식", "front")):
        window.sidebar_buttons[label].click()
        app.processEvents()
        assert window.operator_workspace_stack.currentWidget() is window.operator_pages[label]
        assert window.operator_camera_area.parentWidget() is window.operator_pages[label]
        assert window._camera_layout_mode == expected_mode
        assert window.sidebar_buttons[label].isChecked() is True

    # Pages without cameras never steal the shared grid.
    window.sidebar_buttons["NAS 연결 확인"].click()
    app.processEvents()
    assert window.operator_camera_area.parentWidget() is window.operator_pages["번호판 인식"]
    assert window.model.can_show_final_ok is False
    window.close()


def test_log_page_shows_tail_and_applies_filter(monkeypatch, tmp_path: Path):
    app = _qt_app()
    import towersightai.ui.pyqt_app as pyqt_app_module

    log_path = tmp_path / "towersightai.log"
    log_path.write_text(
        "2026-09-02 INFO app start\n"
        "2026-09-02 INFO camera-capture-status camera=front status=streaming\n"
        "2026-09-02 ERROR nas upload failed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pyqt_app_module, "DEFAULT_RUNTIME_LOG", log_path)

    settings = _settings()
    window = OperatorWindow(build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras))
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    window.sidebar_buttons["실행 로그"].click()
    app.processEvents()

    assert "camera-capture-status" in window.log_view.toPlainText()
    assert "nas upload failed" in window.log_view.toPlainText()

    window.log_filter_input.setText("ERROR")
    app.processEvents()
    filtered = window.log_view.toPlainText()
    assert "nas upload failed" in filtered
    assert "camera-capture-status" not in filtered
    window.close()


def test_system_test_button_reports_diagnostic_result_without_authorizing_ok(monkeypatch):
    app = _qt_app()
    import towersightai.ui.pyqt_app as pyqt_app_module
    from towersightai.diagnostics import DiagnosticResult, DiagnosticStatus

    class _FakeService:
        def __init__(self, settings):
            self.settings = settings

        def run(self, test_id, *, timeout_seconds=10):
            return DiagnosticResult(
                test_id=test_id,
                label="설정 검증",
                status=DiagnosticStatus.PASS,
                summary="네 개 카메라 역할 확인",
                duration_ms=7,
            )

    monkeypatch.setattr(pyqt_app_module, "DiagnosticsService", _FakeService)

    settings = _settings()
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    window._unlock_operator()
    window.sidebar_toggle_button.click()
    window.sidebar_buttons["시스템 점검"].click()
    app.processEvents()

    window.system_test_buttons["settings"].click()
    for _ in range(40):
        app.processEvents()
        if not window._system_test_running:
            break
        QTest.qWait(25)

    text = window.system_test_log.toPlainText()
    assert "설정 검증" in text
    assert "safe_to_operate=False" in text
    assert window.model.can_show_final_ok is False
    assert window.system_test_buttons["settings"].isEnabled() is True
    window.close()


def test_camera_surface_contain_mode_centers_the_letterboxed_frame():
    _qt_app()
    from towersightai.ui.pyqt_app import CameraSurface

    surface = CameraSurface("정면")
    surface.resize(400, 300)
    wide = QImage(640, 120, QImage.Format.Format_RGB888)
    wide.fill(QColor("#ffffff"))
    surface.set_frame(wide)
    surface.set_status("정상 수신")
    rendered = QImage(surface.size(), QImage.Format.Format_RGB32)
    rendered.fill(QColor("#000000"))
    from PyQt6.QtGui import QPainter as _QPainter

    painter = _QPainter(rendered)
    surface.render(painter)
    painter.end()

    center_y = surface.height() // 2
    # A 640x120 frame letterboxed into the tile must appear at the vertical center,
    # and the band just under the title row must stay background, not image.
    assert rendered.pixelColor(surface.width() // 2, center_y).red() > 200
    assert rendered.pixelColor(surface.width() // 2, 60).red() < 100


def test_starting_another_purpose_task_auto_switches_instead_of_refusing(monkeypatch, tmp_path: Path):
    """Clicking a different AI task while one is running stops the old one and
    starts the new one as soon as the old worker has cleaned up."""
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _settings()
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    window._unlock_operator()
    window._runtime_camera_status["front"] = "정상 수신"
    window._runtime_camera_status["rear_side"] = "정상 수신"

    window.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION].click()
    app.processEvents()
    assert window._purpose_task_id == PURPOSE_VEHICLE_DETECTION
    vehicle_thread = window._purpose_threads[0]
    vehicle_worker = window._purpose_workers[0]

    # Switch request: the old task is asked to stop and the new one is queued, not refused.
    window.purpose_task_buttons[PURPOSE_PERSON_PRESENCE].click()
    app.processEvents()
    assert vehicle_worker._stop_requested is True
    assert window._pending_user_purpose_task_id == PURPOSE_PERSON_PRESENCE
    assert "자동 시작" in window.warning_label.text()
    assert window.purpose_task_buttons[PURPOSE_PERSON_PRESENCE].isChecked() is True
    assert window.model.can_show_final_ok is False

    # Old worker finishes cleaning up -> the queued task starts automatically.
    window._cleanup_purpose_worker(vehicle_thread, vehicle_worker)
    app.processEvents()
    assert window._pending_user_purpose_task_id == ""
    assert window._purpose_task_id == PURPOSE_PERSON_PRESENCE
    assert window._purpose_task_enabled is True
    assert len(window._purpose_workers) == 1
    assert window._purpose_workers[0].task_id == PURPOSE_PERSON_PRESENCE
    window.close()


def test_clicking_the_running_purpose_task_still_stops_it(monkeypatch):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _settings()
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    window._unlock_operator()
    window._runtime_camera_status["front"] = "정상 수신"

    window.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION].click()
    app.processEvents()
    worker = window._purpose_workers[0]

    window.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION].click()
    app.processEvents()
    assert worker._stop_requested is True
    assert window._purpose_task_enabled is False
    assert window._pending_user_purpose_task_id == ""
    window.close()


def test_purpose_run_buttons_use_start_stop_wording(monkeypatch):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _settings()
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    window._unlock_operator()
    window._runtime_camera_status["front"] = "정상 수신"

    vehicle = window.purpose_task_buttons[PURPOSE_VEHICLE_DETECTION]
    assert vehicle.text() == "차량 감지 시작"
    vehicle.click()
    app.processEvents()
    assert vehicle.text() == "차량 감지 중지"
    assert window.purpose_task_buttons[PURPOSE_PERSON_PRESENCE].text() == "사람 감지 시작"
    window.close()


def test_hailo_health_snapshot_updates_pill_and_system_panel(monkeypatch):
    app = _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    from towersightai.inference.hailo_health import HailoHealthSnapshot

    settings = _settings()
    window = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    assert window.hailo_status_label.text() == "HAILO 확인 중"

    window._set_hailo_health(
        HailoHealthSnapshot(
            status="ok",
            summary="정상",
            pcie_address="0000:02:00.0",
            driver_loaded=True,
            driver_version="4.23.0",
            device_node_exists=True,
            rxerr_count=0,
            chip_temperature_c=47.2,
        )
    )
    assert window.hailo_status_label.text() == "HAILO 정상 47°C"
    assert window.hailo_status_label.property("hailo") == "ok"
    assert "4.23.0" in window.hailo_health_label.text()

    window._set_hailo_health(
        HailoHealthSnapshot(
            status="error",
            summary="장치가 제어 요청에 응답하지 않습니다",
            pcie_address="0000:02:00.0",
            driver_loaded=True,
            device_node_exists=True,
            detail="HAILO_DRIVER_OPERATION_FAILED(36) · 콜드 부팅(전원 완전 차단)이 필요할 수 있습니다.",
        )
    )
    assert window.hailo_status_label.text() == "HAILO 오류"
    assert window.hailo_status_label.property("hailo") == "error"
    assert "콜드 부팅" in window.hailo_health_label.text()
    # 건강 스냅샷은 진단 정보일 뿐 안전 게이트를 열지 못한다.
    assert window.model.can_show_final_ok is False
    window.close()


def test_hailo_health_worker_starts_only_with_settings(monkeypatch):
    _qt_app()
    monkeypatch.setattr(QThread, "start", lambda self: None)
    settings = _settings()
    with_settings = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras), settings
    )
    assert len(with_settings._hailo_health_workers) == 1
    with_settings.close()

    without_settings = OperatorWindow(
        build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)
    )
    assert without_settings._hailo_health_workers == []
    without_settings.close()
