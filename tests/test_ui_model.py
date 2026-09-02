from pathlib import Path

from towersightai.config.settings import CameraRole, Settings
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import (
    AlignmentResult,
    DriverLayout,
    DriverTone,
    GlobalSafetyStatus,
    PlcConnectionState,
    build_driver_display,
    build_operator_display,
    camera_layout_for_state,
    guidance_message_for_alignment,
)


def _settings() -> Settings:
    return Settings(
        tappas_workspace=Path("/opt/hailo/tappas"),
        hailo_hef_path=Path("/opt/hailo/tappas/model.hef"),
        hailo_postprocess_so=Path("/opt/hailo/tappas/post.so"),
        camera_1={
            "id": "ceiling",
            "role": "ceiling",
            "rtsp_url": "rtsp://operator:secret@192.0.2.10:554/stream1",
        },
        camera_2={
            "id": "front",
            "role": "front",
            "rtsp_url": "rtsp://operator:secret@192.0.2.11:554/stream1",
        },
        camera_3={
            "id": "rear_side",
            "role": "rear_side",
            "rtsp_url": "rtsp://operator:secret@192.0.2.12:554/stream1",
        },
        camera_4={
            "id": "opposite_side",
            "role": "opposite_side",
            "rtsp_url": "rtsp://operator:secret@192.0.2.13:554/stream1",
        },
        calibration_path=Path("data/calibration/site.json"),
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_alignment_guidance_messages_are_single_operator_instructions():
    assert guidance_message_for_alignment(AlignmentResult.MOVE_RIGHT) == "차량을 오른쪽으로 조금 이동해 주세요."
    assert guidance_message_for_alignment(AlignmentResult.MOVE_LEFT) == "차량을 왼쪽으로 조금 이동해 주세요."
    assert guidance_message_for_alignment(AlignmentResult.MOVE_FORWARD) == "조금 더 앞으로 진입해 주세요."
    assert guidance_message_for_alignment(AlignmentResult.MOVE_BACKWARD) == "차량을 조금 후진해 주세요."


def test_camera_layout_prioritizes_front_then_alignment_then_all_safety_views():
    idle_primary, idle_secondary = camera_layout_for_state(ParkingState.IDLE)
    assert [role.value for role in idle_primary] == ["front"]
    assert {role.value for role in idle_secondary} == {"ceiling", "rear_side", "opposite_side"}

    alignment_primary, alignment_secondary = camera_layout_for_state(ParkingState.ALIGNMENT_GUIDE)
    assert [role.value for role in alignment_primary] == ["ceiling", "front"]
    assert {role.value for role in alignment_secondary} == {"rear_side", "opposite_side"}

    safety_primary, safety_secondary = camera_layout_for_state(ParkingState.SAFETY_CHECK)
    assert {role.value for role in safety_primary} == {"ceiling", "front", "rear_side", "opposite_side"}
    assert safety_secondary == ()


def test_display_redacts_camera_credentials():
    settings = _settings()
    model = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    assert all("secret" not in tile.redacted_source for tile in model.camera_tiles)
    assert all("***:***" in tile.redacted_source for tile in model.camera_tiles)


def test_unknown_camera_or_plc_blocks_final_ok_styling():
    settings = _settings()
    model = build_operator_display(
        state=ParkingState.READY_FOR_OPERATION,
        cameras=settings.cameras,
        plc_state=PlcConnectionState.UNKNOWN,
        healthy_camera_ids=["ceiling", "front", "rear_side", "opposite_side"],
        stale_camera_ids=[],
        hailo_healthy=True,
        calibration_valid=True,
        human_possible=False,
        occupant_possible=False,
        obstacle_possible=False,
    )

    assert model.safety_status is GlobalSafetyStatus.NG
    assert model.can_show_final_ok is False
    assert "PLC" in model.warning


def test_ready_for_operation_requires_all_prerequisites():
    settings = _settings()
    model = build_operator_display(
        state=ParkingState.READY_FOR_OPERATION,
        cameras=settings.cameras,
        plc_state=PlcConnectionState.CONNECTED,
        healthy_camera_ids=["ceiling", "front", "rear_side", "opposite_side"],
        stale_camera_ids=[],
        hailo_healthy=True,
        calibration_valid=True,
        human_possible=False,
        occupant_possible=False,
        obstacle_possible=False,
    )

    assert model.safety_status is GlobalSafetyStatus.READY
    assert model.can_show_final_ok is True
    assert model.warning == "모든 안전 조건 통과"


def test_disabled_birdview_hides_ceiling_and_blocks_ready_state():
    settings = _settings()
    active_cameras = [camera for camera in settings.cameras if camera.role is not CameraRole.ceiling]
    model = build_operator_display(
        state=ParkingState.READY_FOR_OPERATION,
        cameras=active_cameras,
        plc_state=PlcConnectionState.CONNECTED,
        healthy_camera_ids=["front", "rear_side", "opposite_side"],
        stale_camera_ids=[],
        hailo_healthy=True,
        calibration_valid=True,
        human_possible=False,
        occupant_possible=False,
        obstacle_possible=False,
        birdview_available=False,
    )

    assert {tile.role for tile in model.camera_tiles} == {
        CameraRole.front,
        CameraRole.rear_side,
        CameraRole.opposite_side,
    }
    assert model.safety_status is GlobalSafetyStatus.NG
    assert model.can_show_final_ok is False
    assert model.camera_health_summary == "카메라 3/3 정상 · 버드뷰 OFF"
    assert model.warning == "버드뷰 OFF: 정렬 판단 불가 · 최종 OK 차단"

    driver = build_driver_display(
        model,
        state_override=ParkingState.ALIGNMENT_GUIDE,
        blocked_roles=(),
    )
    assert driver.layout is DriverLayout.FRONT
    assert driver.visible_roles == (CameraRole.front,)
    assert driver.headline == "정지"
    assert driver.tone is DriverTone.DANGER
    assert "버드뷰 비활성화" in driver.detail


def test_disabled_birdview_remains_visible_when_an_active_camera_is_blocked():
    settings = _settings()
    active_cameras = [camera for camera in settings.cameras if camera.role is not CameraRole.ceiling]

    model = build_operator_display(
        state=ParkingState.IDLE,
        cameras=active_cameras,
        birdview_available=False,
    )

    assert "카메라 입력 차단" in model.warning
    assert "버드뷰 OFF" in model.warning
    assert "최종 OK 차단" in model.warning


def test_driver_display_uses_state_specific_camera_priority_and_short_actions():
    settings = _settings()
    source = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    idle = build_driver_display(source, blocked_roles=())
    entry = build_driver_display(
        source,
        state_override=ParkingState.VEHICLE_ENTERING,
        blocked_roles=(),
        simulated=True,
    )
    alignment = build_driver_display(
        source,
        state_override=ParkingState.ALIGNMENT_GUIDE,
        alignment_override=AlignmentResult.MOVE_RIGHT,
        blocked_roles=(),
        simulated=True,
    )
    safety = build_driver_display(
        source,
        state_override=ParkingState.SAFETY_CHECK,
        blocked_roles=(),
        simulated=True,
    )
    detected = build_driver_display(
        source,
        state_override=ParkingState.VEHICLE_DETECTED,
        blocked_roles=(),
        simulated=True,
    )

    assert idle.layout is DriverLayout.FRONT
    assert [role.value for role in idle.visible_roles] == ["front"]
    assert idle.headline == "진입 준비"
    assert detected.layout is DriverLayout.ENTRY
    assert [role.value for role in detected.visible_roles] == ["front", "ceiling"]
    assert entry.layout is DriverLayout.ENTRY
    assert [role.value for role in entry.visible_roles] == ["front", "ceiling"]
    assert entry.headline == "천천히 진입"
    assert alignment.layout is DriverLayout.ALIGNMENT
    assert alignment.headline == "오른쪽 이동"
    assert safety.layout is DriverLayout.ENTRY
    assert [role.value for role in safety.visible_roles] == ["front", "ceiling"]


def test_driver_display_promotes_human_camera_and_blocks_simulated_ok():
    settings = _settings()
    ready = build_operator_display(
        state=ParkingState.READY_FOR_OPERATION,
        cameras=settings.cameras,
        plc_state=PlcConnectionState.CONNECTED,
        healthy_camera_ids=["ceiling", "front", "rear_side", "opposite_side"],
        stale_camera_ids=[],
        hailo_healthy=True,
        calibration_valid=True,
        human_possible=False,
        occupant_possible=False,
        obstacle_possible=False,
    )

    simulation = build_driver_display(
        ready,
        state_override=ParkingState.HUMAN_DETECTED,
        blocked_roles=(),
        primary_alert_role=ready.camera_tiles[3].role,
        simulated=True,
    )

    assert simulation.layout is DriverLayout.ENTRY
    assert [role.value for role in simulation.visible_roles] == ["front", "ceiling"]
    assert simulation.primary_role.value == "front"
    assert simulation.headline == "즉시 밖으로 이동"
    assert simulation.tone is DriverTone.TEST
    assert simulation.can_show_final_ok is False
    assert "PLC OK 차단" in simulation.blocking_reason


def test_required_driver_camera_loss_forces_stop_and_danger():
    settings = _settings()
    source = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    display = build_driver_display(source, blocked_roles=[source.camera_tiles[1].role])

    assert display.tone is DriverTone.DANGER
    assert display.headline == "정지"
    assert "카메라" in display.blocking_reason
    assert display.can_show_final_ok is False


def test_runtime_camera_recovery_replaces_stale_startup_warning():
    settings = _settings()
    source = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    display = build_driver_display(source, blocked_roles=())

    assert display.blocking_reason == "PLC 상태 미확인: 최종 OK 차단"
    assert "카메라 입력 차단" not in display.blocking_reason


def test_runtime_side_camera_failure_restores_camera_warning():
    settings = _settings()
    source = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    display = build_driver_display(source, blocked_roles=[CameraRole.rear_side])

    assert display.blocking_reason == "카메라 입력 차단: 좌측면"
    assert display.can_show_final_ok is False


def test_primary_side_camera_loss_blocks_safety_display():
    # The left (rear_side) camera is a primary person-watch camera: losing it
    # during a safety state is a hard block.
    settings = _settings()
    source = build_operator_display(state=ParkingState.SAFETY_CHECK, cameras=settings.cameras)

    display = build_driver_display(
        source,
        blocked_roles=[CameraRole.rear_side],
    )

    assert display.tone is DriverTone.DANGER
    assert display.headline == "정지"
    assert display.can_show_final_ok is False


def test_opposite_side_camera_loss_is_supplementary_not_a_hard_block():
    # The right (opposite_side) camera is supplementary (OR): its loss during a
    # safety state must NOT read as "필수 카메라 입력 없음". Person detection is OR,
    # so a person on any other watched camera still counts as a person inside.
    settings = _settings()
    source = build_operator_display(state=ParkingState.SAFETY_CHECK, cameras=settings.cameras)

    display = build_driver_display(
        source,
        blocked_roles=[CameraRole.opposite_side],
    )

    assert display.blocking_reason != "필수 카메라 입력 없음 · 주차기 동작 금지"
    assert display.headline != "정지"
    assert display.can_show_final_ok is False


def test_idle_person_alert_keeps_front_only_layout():
    settings = _settings()
    source = build_operator_display(state=ParkingState.IDLE, cameras=settings.cameras)

    display = build_driver_display(
        source,
        state_override=ParkingState.HUMAN_DETECTED,
        layout_state_override=ParkingState.IDLE,
        blocked_roles=(),
    )

    assert display.state is ParkingState.HUMAN_DETECTED
    assert display.layout is DriverLayout.FRONT
    assert display.visible_roles == (CameraRole.front,)
    assert display.headline == "즉시 밖으로 이동"
    assert display.tone is DriverTone.DANGER


def test_copy_override_replaces_driver_copy():
    source = build_operator_display(
        state=ParkingState.PARKED,
        cameras=_settings().cameras,
        healthy_camera_ids=("ceiling", "front", "rear_side", "opposite_side"),
        stale_camera_ids=(),
    )
    display = build_driver_display(source, copy_key="parked_instruct", blocked_roles=())
    assert display.detail == "기어를 P에 놓고 시동을 끈 후 밖으로 나가 주십시오."
    assert display.can_show_final_ok is False


def test_exit_person_copy_override_is_danger():
    source = build_operator_display(
        state=ParkingState.HUMAN_DETECTED,
        cameras=_settings().cameras,
        healthy_camera_ids=("ceiling", "front", "rear_side", "opposite_side"),
        stale_camera_ids=(),
    )
    display = build_driver_display(source, copy_key="exit_person_warning", blocked_roles=())
    assert display.detail == "주차가 시작될 예정이므로 바깥으로 나가 주십시오."
    assert display.tone is DriverTone.DANGER
    assert display.can_show_final_ok is False


def test_front_guide_alignment_copy_suppresses_birdview_danger():
    source = build_operator_display(
        state=ParkingState.ALIGNMENT_GUIDE,
        cameras=_settings().cameras,
        healthy_camera_ids=("front", "rear_side", "opposite_side"),
        stale_camera_ids=(),
        birdview_available=False,
    )
    default = build_driver_display(source, blocked_roles=())
    assert default.tone is DriverTone.DANGER  # birdview-off alignment stays blocked by default
    guided = build_driver_display(source, copy_key="alignment_front_guide", blocked_roles=())
    assert guided.tone is not DriverTone.DANGER
    assert guided.detail == "천천히 진입해 주십시오. 바퀴를 유도선에 맞추세요."
    assert guided.can_show_final_ok is False  # gate still blocks final OK


def test_person_warning_survives_missing_right_camera_dev_scenario():
    # Dev/site reality: only front + left are up; the right camera is down. During
    # the person watch (HUMAN_DETECTED) that must NOT read as "필수 카메라 입력 없음";
    # the person warning copy shows instead, and final OK stays blocked.
    source = build_operator_display(
        state=ParkingState.HUMAN_DETECTED,
        cameras=_settings().cameras,
        healthy_camera_ids=("front", "rear_side"),
        stale_camera_ids=("ceiling", "opposite_side"),
        birdview_available=False,
    )
    display = build_driver_display(
        source,
        copy_key="exit_person_warning",
        blocked_roles=[CameraRole.ceiling, CameraRole.opposite_side],
    )
    assert display.blocking_reason != "필수 카메라 입력 없음 · 주차기 동작 금지"
    assert display.detail == "주차가 시작될 예정이므로 바깥으로 나가 주십시오."
    assert display.can_show_final_ok is False  # gate still blocks (dead tiles + PLC unknown)
