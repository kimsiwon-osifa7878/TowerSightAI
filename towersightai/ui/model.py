from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from towersightai.camera.pipeline import redact_rtsp
from towersightai.config.settings import CameraConfig, CameraRole
from towersightai.state_machine.core import ParkingState


class AlignmentResult(str, Enum):
    UNKNOWN = "unknown"
    MOVE_RIGHT = "move_right"
    MOVE_LEFT = "move_left"
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    PARKED_OK = "parked_ok"
    ALIGNMENT_NG = "alignment_ng"


class GlobalSafetyStatus(str, Enum):
    NG = "NG"
    WAIT = "WAIT"
    READY = "READY"
    STOPPED = "STOPPED"


class PlcConnectionState(str, Enum):
    UNKNOWN = "UNKNOWN"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CameraTileState:
    camera_id: str
    role: CameraRole
    title: str
    redacted_source: str
    healthy: bool = False
    stale: bool = True
    fps: float | None = None
    latency_ms: int | None = None
    message: str = "프레임 대기"

    @property
    def blocked(self) -> bool:
        return not self.healthy or self.stale

    @property
    def status_text(self) -> str:
        if not self.healthy:
            return "NG: 카메라 연결 이상"
        if self.stale:
            return "NG: 프레임 지연"
        return "정상 수신"


@dataclass(frozen=True)
class OperatorDisplayModel:
    state: ParkingState
    safety_status: GlobalSafetyStatus
    primary_roles: tuple[CameraRole, ...]
    secondary_roles: tuple[CameraRole, ...]
    instruction: str
    warning: str
    plc_state: PlcConnectionState
    camera_tiles: tuple[CameraTileState, ...]
    fullscreen: bool = True
    hailo_healthy: bool = False
    calibration_valid: bool = False
    human_possible: bool = True
    occupant_possible: bool = True
    obstacle_possible: bool = True

    @property
    def can_show_final_ok(self) -> bool:
        return (
            self.state is ParkingState.READY_FOR_OPERATION
            and self.safety_status is GlobalSafetyStatus.READY
            and self.plc_state is PlcConnectionState.CONNECTED
            and self.hailo_healthy
            and self.calibration_valid
            and not self.human_possible
            and not self.occupant_possible
            and not self.obstacle_possible
            and all(not tile.blocked for tile in self.camera_tiles)
        )

    @property
    def camera_health_summary(self) -> str:
        blocked = sum(1 for tile in self.camera_tiles if tile.blocked)
        if blocked:
            return f"카메라 {blocked}/4 차단"
        return "카메라 4/4 정상"


def build_operator_display(
    *,
    state: ParkingState,
    cameras: Iterable[CameraConfig],
    alignment: AlignmentResult = AlignmentResult.UNKNOWN,
    plc_state: PlcConnectionState = PlcConnectionState.UNKNOWN,
    fullscreen: bool = True,
    healthy_camera_ids: Iterable[str] = (),
    stale_camera_ids: Iterable[str] | None = None,
    hailo_healthy: bool = False,
    calibration_valid: bool = False,
    human_possible: bool = True,
    occupant_possible: bool = True,
    obstacle_possible: bool = True,
) -> OperatorDisplayModel:
    healthy = set(healthy_camera_ids)
    stale = set(stale_camera_ids or ())
    tiles = tuple(
        CameraTileState(
            camera_id=camera.id,
            role=camera.role,
            title=_camera_title(camera.role),
            redacted_source=redact_rtsp(camera.rtsp_url),
            healthy=camera.id in healthy,
            stale=camera.id in stale or camera.id not in healthy,
        )
        for camera in cameras
    )
    safety_status = _safety_status_for_state(
        state=state,
        plc_state=plc_state,
        camera_tiles=tiles,
        hailo_healthy=hailo_healthy,
        calibration_valid=calibration_valid,
        human_possible=human_possible,
        occupant_possible=occupant_possible,
        obstacle_possible=obstacle_possible,
    )
    primary, secondary = camera_layout_for_state(state)
    instruction = _instruction_for_state(state, alignment, safety_status)
    warning = _warning_for_state(
        state=state,
        safety_status=safety_status,
        plc_state=plc_state,
        camera_tiles=tiles,
        hailo_healthy=hailo_healthy,
        calibration_valid=calibration_valid,
        human_possible=human_possible,
        occupant_possible=occupant_possible,
        obstacle_possible=obstacle_possible,
    )
    return OperatorDisplayModel(
        state=state,
        safety_status=safety_status,
        primary_roles=primary,
        secondary_roles=secondary,
        instruction=instruction,
        warning=warning,
        plc_state=plc_state,
        camera_tiles=tiles,
        fullscreen=fullscreen,
        hailo_healthy=hailo_healthy,
        calibration_valid=calibration_valid,
        human_possible=human_possible,
        occupant_possible=occupant_possible,
        obstacle_possible=obstacle_possible,
    )


def camera_layout_for_state(state: ParkingState) -> tuple[tuple[CameraRole, ...], tuple[CameraRole, ...]]:
    if state in {ParkingState.IDLE, ParkingState.VEHICLE_DETECTED, ParkingState.PLATE_RECOGNITION}:
        return (CameraRole.front,), (CameraRole.ceiling, CameraRole.rear_side, CameraRole.opposite_side)
    if state in {ParkingState.VEHICLE_ENTERING, ParkingState.ALIGNMENT_GUIDE}:
        return (CameraRole.ceiling, CameraRole.front), (CameraRole.rear_side, CameraRole.opposite_side)
    return (
        CameraRole.ceiling,
        CameraRole.front,
        CameraRole.rear_side,
        CameraRole.opposite_side,
    ), ()


def guidance_message_for_alignment(alignment: AlignmentResult) -> str:
    return {
        AlignmentResult.MOVE_RIGHT: "차량을 오른쪽으로 조금 이동해 주세요.",
        AlignmentResult.MOVE_LEFT: "차량을 왼쪽으로 조금 이동해 주세요.",
        AlignmentResult.MOVE_FORWARD: "조금 더 앞으로 진입해 주세요.",
        AlignmentResult.MOVE_BACKWARD: "차량을 조금 후진해 주세요.",
        AlignmentResult.PARKED_OK: "정상 위치에 주차되었습니다.",
        AlignmentResult.ALIGNMENT_NG: "정렬 상태를 확인할 수 없습니다. 잠시 정지해 주세요.",
        AlignmentResult.UNKNOWN: "차량 위치를 확인 중입니다. 천천히 진입해 주세요.",
    }[alignment]


def _safety_status_for_state(
    *,
    state: ParkingState,
    plc_state: PlcConnectionState,
    camera_tiles: tuple[CameraTileState, ...],
    hailo_healthy: bool,
    calibration_valid: bool,
    human_possible: bool,
    occupant_possible: bool,
    obstacle_possible: bool,
) -> GlobalSafetyStatus:
    if state is ParkingState.AI_STOP:
        return GlobalSafetyStatus.STOPPED
    if any(tile.blocked for tile in camera_tiles):
        return GlobalSafetyStatus.NG
    if plc_state is not PlcConnectionState.CONNECTED:
        return GlobalSafetyStatus.NG
    if not hailo_healthy or not calibration_valid:
        return GlobalSafetyStatus.NG
    if state is ParkingState.HUMAN_DETECTED or human_possible or occupant_possible or obstacle_possible:
        return GlobalSafetyStatus.NG
    if state is ParkingState.READY_FOR_OPERATION:
        return GlobalSafetyStatus.READY
    return GlobalSafetyStatus.WAIT


def _instruction_for_state(
    state: ParkingState,
    alignment: AlignmentResult,
    safety_status: GlobalSafetyStatus,
) -> str:
    if state in {ParkingState.VEHICLE_ENTERING, ParkingState.ALIGNMENT_GUIDE}:
        return guidance_message_for_alignment(alignment)
    if state is ParkingState.PARKED:
        return "시동을 끄고 사이드미러를 접어 주세요."
    if state is ParkingState.SAFETY_CHECK:
        return "주차기 내부와 차량 내부의 안전 상태를 확인 중입니다."
    if state is ParkingState.HUMAN_DETECTED:
        return "사람 또는 위험 요소가 감지되었습니다. 주차기 내부를 비워 주세요."
    if state is ParkingState.READY_FOR_OPERATION and safety_status is GlobalSafetyStatus.READY:
        return "안전 확인 완료. PLC 운전 준비 상태입니다."
    if state is ParkingState.AI_STOP:
        return "AI 감시가 중지되었습니다."
    if state is ParkingState.PLATE_RECOGNITION:
        return "차량번호를 인식 중입니다."
    if state is ParkingState.VEHICLE_DETECTED:
        return "진입 차량을 확인했습니다. 천천히 진입해 주세요."
    return "진입 차량을 대기 중입니다."


def _warning_for_state(
    *,
    state: ParkingState,
    safety_status: GlobalSafetyStatus,
    plc_state: PlcConnectionState,
    camera_tiles: tuple[CameraTileState, ...],
    hailo_healthy: bool,
    calibration_valid: bool,
    human_possible: bool,
    occupant_possible: bool,
    obstacle_possible: bool,
) -> str:
    if state is ParkingState.AI_STOP:
        return "AI 감시 중지: PLC OK 신호를 표시하지 않습니다."
    blocked_cameras = [tile.title for tile in camera_tiles if tile.blocked]
    if blocked_cameras:
        return "카메라 입력 차단: " + ", ".join(blocked_cameras)
    if plc_state is not PlcConnectionState.CONNECTED:
        return "PLC 상태 미확인: 최종 OK 차단"
    if not hailo_healthy:
        return "Hailo 추론 상태 미확인: 최종 OK 차단"
    if not calibration_valid:
        return "캘리브레이션 미확인: 최종 OK 차단"
    if human_possible:
        return "사람 존재 가능성: 최종 OK 차단"
    if occupant_possible:
        return "차량 내부 탑승자 가능성: 최종 OK 차단"
    if obstacle_possible:
        return "장애물 가능성: 최종 OK 차단"
    if safety_status is GlobalSafetyStatus.READY:
        return "모든 안전 조건 통과"
    return "안전 조건 확인 중"


def _camera_title(role: CameraRole) -> str:
    return {
        CameraRole.ceiling: "천장 버드뷰",
        CameraRole.front: "정면",
        CameraRole.rear_side: "좌측면",
        CameraRole.opposite_side: "우측면",
    }[role]
