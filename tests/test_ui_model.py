from pathlib import Path

from towersightai.config.settings import Settings
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import (
    AlignmentResult,
    GlobalSafetyStatus,
    PlcConnectionState,
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
