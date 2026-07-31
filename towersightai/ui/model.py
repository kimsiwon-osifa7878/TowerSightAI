from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from towersightai.camera.pipeline import redact_rtsp
from towersightai.config.settings import CameraConfig, CameraRole
from towersightai.state_machine.core import ParkingState


class UiMode(str, Enum):
    DRIVER = "DRIVER"
    OPERATOR = "OPERATOR"
    TESTS = "TESTS"


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


class OperatorAccessState(str, Enum):
    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"


class DriverTone(str, Enum):
    WAIT = "WAIT"
    DANGER = "DANGER"
    READY = "READY"
    STOPPED = "STOPPED"
    TEST = "TEST"


class DriverLayout(str, Enum):
    FRONT = "front"
    ENTRY = "entry"
    ALIGNMENT = "alignment"
    SAFETY = "safety"
    HUMAN = "human"


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
    alignment: AlignmentResult = AlignmentResult.UNKNOWN
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


@dataclass(frozen=True)
class DriverDisplayModel:
    state: ParkingState
    stage_label: str
    headline: str
    detail: str
    blocking_reason: str
    tone: DriverTone
    symbol: str
    layout: DriverLayout
    visible_roles: tuple[CameraRole, ...]
    primary_role: CameraRole | None
    masked_plate_text: str = ""
    simulated: bool = False
    can_show_final_ok: bool = False


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
        alignment=alignment,
        fullscreen=fullscreen,
        hailo_healthy=hailo_healthy,
        calibration_valid=calibration_valid,
        human_possible=human_possible,
        occupant_possible=occupant_possible,
        obstacle_possible=obstacle_possible,
    )


def build_driver_display(
    source: OperatorDisplayModel,
    *,
    state_override: ParkingState | None = None,
    layout_state_override: ParkingState | None = None,
    alignment_override: AlignmentResult | None = None,
    blocked_roles: Iterable[CameraRole] | None = None,
    primary_alert_role: CameraRole | None = None,
    masked_plate_text: str = "",
    simulated: bool = False,
) -> DriverDisplayModel:
    state = state_override or source.state
    alignment = alignment_override or source.alignment
    layout, visible_roles, primary_role = _driver_layout_for_state(
        layout_state_override or state,
        primary_alert_role=primary_alert_role,
    )
    runtime_health_supplied = blocked_roles is not None
    if blocked_roles is None:
        blocked = {tile.role for tile in source.camera_tiles if tile.blocked}
    else:
        blocked = set(blocked_roles)
    required_roles = _driver_required_roles_for_state(state, visible_roles)
    required_blocked = any(role in blocked for role in required_roles)

    stage_label, headline, detail, symbol = _driver_copy_for_state(state, alignment)
    tone = DriverTone.WAIT
    blocking_reason = (
        _warning_for_runtime_camera_health(source, state=state, blocked_roles=blocked)
        if runtime_health_supplied
        else source.warning
    )
    can_show_final_ok = source.can_show_final_ok and not simulated

    if simulated:
        tone = DriverTone.TEST
        blocking_reason = "TEST · 시뮬레이션 입력 · PLC OK 차단"
        can_show_final_ok = False
    elif required_blocked:
        tone = DriverTone.DANGER
        headline = "정지"
        detail = "필수 카메라 영상을 확인할 수 없습니다."
        symbol = "!"
        blocking_reason = "필수 카메라 입력 없음 · 주차기 동작 금지"
        can_show_final_ok = False
    elif state is ParkingState.HUMAN_DETECTED:
        tone = DriverTone.DANGER
    elif state is ParkingState.AI_STOP:
        tone = DriverTone.STOPPED
    elif can_show_final_ok:
        tone = DriverTone.READY

    return DriverDisplayModel(
        state=state,
        stage_label=stage_label,
        headline=headline,
        detail=detail,
        blocking_reason=blocking_reason,
        tone=tone,
        symbol=symbol,
        layout=layout,
        visible_roles=visible_roles,
        primary_role=primary_role,
        masked_plate_text=masked_plate_text,
        simulated=simulated,
        can_show_final_ok=can_show_final_ok,
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


def _driver_layout_for_state(
    state: ParkingState,
    *,
    primary_alert_role: CameraRole | None,
) -> tuple[DriverLayout, tuple[CameraRole, ...], CameraRole | None]:
    del primary_alert_role
    if state is ParkingState.IDLE:
        return DriverLayout.FRONT, (CameraRole.front,), CameraRole.front
    if state in {
        ParkingState.VEHICLE_DETECTED,
        ParkingState.PLATE_RECOGNITION,
        ParkingState.VEHICLE_ENTERING,
    }:
        return DriverLayout.ENTRY, (CameraRole.front, CameraRole.ceiling), CameraRole.front
    if state in {ParkingState.ALIGNMENT_GUIDE, ParkingState.PARKED}:
        return DriverLayout.ALIGNMENT, (CameraRole.ceiling, CameraRole.front), CameraRole.ceiling
    return DriverLayout.ENTRY, (CameraRole.front, CameraRole.ceiling), CameraRole.front


def _driver_required_roles_for_state(
    state: ParkingState,
    visible_roles: tuple[CameraRole, ...],
) -> tuple[CameraRole, ...]:
    if state in {
        ParkingState.SAFETY_CHECK,
        ParkingState.HUMAN_DETECTED,
        ParkingState.READY_FOR_OPERATION,
    }:
        return (
            CameraRole.ceiling,
            CameraRole.front,
            CameraRole.rear_side,
            CameraRole.opposite_side,
        )
    return visible_roles


def _driver_copy_for_state(
    state: ParkingState,
    alignment: AlignmentResult,
) -> tuple[str, str, str, str]:
    if state is ParkingState.IDLE:
        return "입차 대기", "진입 준비", "안내가 표시되면 천천히 진입하세요.", "P"
    if state is ParkingState.VEHICLE_DETECTED:
        return "차량 감지", "천천히 진입", "정면 가이드 안쪽으로 직진하세요.", "↑"
    if state is ParkingState.PLATE_RECOGNITION:
        return "번호판 인식", "천천히 진입", "차량번호 확인 중", "▣"
    if state is ParkingState.VEHICLE_ENTERING:
        return "차량 진입", "천천히 진입", "정면 가이드 안쪽으로 직진하세요.", "↑"
    if state is ParkingState.ALIGNMENT_GUIDE:
        headline, symbol = {
            AlignmentResult.MOVE_RIGHT: ("오른쪽 이동", "→"),
            AlignmentResult.MOVE_LEFT: ("왼쪽 이동", "←"),
            AlignmentResult.MOVE_FORWARD: ("전진", "↑"),
            AlignmentResult.MOVE_BACKWARD: ("후진", "↓"),
            AlignmentResult.PARKED_OK: ("정지", "■"),
            AlignmentResult.ALIGNMENT_NG: ("정지", "!"),
            AlignmentResult.UNKNOWN: ("천천히 진입", "…"),
        }[alignment]
        return "버드뷰 정렬", headline, "차량 중심을 중앙선에 맞추세요.", symbol
    if state is ParkingState.PARKED:
        return "주차 위치 도달", "정지", "시동을 끄고 사이드미러를 접으세요.", "■"
    if state is ParkingState.SAFETY_CHECK:
        return "내부 안전 확인", "주차기 밖으로 이동", "안전 확인이 끝날 때까지 밖에서 기다리세요.", "…"
    if state is ParkingState.HUMAN_DETECTED:
        return "사람 감지", "즉시 밖으로 이동", "사람 감지 · 주차기에서 멀리 떨어지세요.", "!"
    if state is ParkingState.READY_FOR_OPERATION:
        return "안전 확인 완료", "밖에서 기다리세요", "주차기 동작 준비 중입니다.", "✓"
    return "AI 감시 중지", "밖에서 기다리세요", "주차기가 동작 중입니다.", "■"


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


def _warning_for_runtime_camera_health(
    source: OperatorDisplayModel,
    *,
    state: ParkingState,
    blocked_roles: set[CameraRole],
) -> str:
    runtime_tiles = tuple(
        replace(
            tile,
            healthy=tile.role not in blocked_roles,
            stale=tile.role in blocked_roles,
        )
        for tile in source.camera_tiles
    )
    return _warning_for_state(
        state=state,
        safety_status=source.safety_status,
        plc_state=source.plc_state,
        camera_tiles=runtime_tiles,
        hailo_healthy=source.hailo_healthy,
        calibration_valid=source.calibration_valid,
        human_possible=source.human_possible,
        occupant_possible=source.occupant_possible,
        obstacle_possible=source.obstacle_possible,
    )


def _camera_title(role: CameraRole) -> str:
    return {
        CameraRole.ceiling: "천장 버드뷰",
        CameraRole.front: "정면",
        CameraRole.rear_side: "좌측면",
        CameraRole.opposite_side: "우측면",
    }[role]
