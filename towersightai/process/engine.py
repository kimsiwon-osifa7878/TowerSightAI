"""Continuous parking-process engine.

Pure Python (no Qt), deterministic under an injected clock, unit-testable from
synthetic events. The engine consumes normalized detection batches, radar
presence, LPR attempts, and health signals; on each 1 Hz ``tick`` it advances an
internal :class:`SafetyStateMachine` and returns an :class:`EngineOutput` that
the UI host applies (display override, audio cue, simulated PLC requests, raw
event requests, LPR loop control).

Safety posture:

- The engine produces states and *add-only* danger flags. It never computes an
  OK of its own — ``OperatorDisplayModel.can_show_final_ok`` remains the only
  gate and stays false while the PLC is unknown/simulated.
- Every PLC request payload carries ``"simulated": True``. Timer-based
  progressions (10 s clear, 60 s operation) are stand-ins for missing PLC
  signals, never authorization.
- Uncertainty (monitoring task not running, required camera unhealthy) aborts
  any in-progress entry back to IDLE. The machine-operating phase is the one
  exception: the machine is already moving, so the engine keeps warning instead
  of pretending to stop it.
- Radar (LD2410) can only *add* person-possible; it can never clear a person.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from towersightai.config.settings import CameraRole
from towersightai.inference.events import DetectionEvent
from towersightai.process.settings_store import OperatorRuntimeSettings
from towersightai.state_machine.core import ParkingState, SafetyStateMachine

_LOGGER = logging.getLogger("towersightai.process.engine")

PERSON_LABELS = frozenset({"person", "human"})
VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle", "vehicle"})

# Person-watch camera roles per public state. The opposite_side camera sees
# outside the door, so it joins only while the machine is operating.
IDLE_PERSON_ROLES = frozenset({CameraRole.ceiling, CameraRole.front, CameraRole.rear_side})
EXIT_PERSON_ROLES = frozenset({CameraRole.front, CameraRole.rear_side})
OPERATING_PERSON_ROLES = frozenset(
    {CameraRole.front, CameraRole.rear_side, CameraRole.opposite_side}
)

TRIGGER_ROLE = CameraRole.opposite_side

UNRECOGNIZED_PLATE = "미인식"

# Copy keys resolved to Korean driver copy by towersightai.ui.model.
COPY_IDLE_PERSON = "idle_person_warning"
COPY_EXIT_PERSON = "exit_person_warning"
COPY_ALIGNMENT_FRONT = "alignment_front_guide"
COPY_PARKED = "parked_instruct"

AUDIO_EXIT_WARNING = "exit_warning"


@dataclass(frozen=True)
class PlcRequest:
    name: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class RawEventRequest:
    kind: str  # "vehicle_entry" | "vehicle_session_end" | "plate"
    camera_id: str = ""
    reason: str = ""
    plate_number: str = ""
    confidence: float | None = None


@dataclass(frozen=True)
class EngineOutput:
    public_state: ParkingState
    phase: str
    copy_key: str | None = None
    plate_number: str = ""
    warning_text: str = ""
    uncertain_reason: str = ""
    person_possible: bool = False
    audio_cue: str | None = None
    show_wheel_guides: bool = False
    plc_requests: tuple[PlcRequest, ...] = ()
    raw_events: tuple[RawEventRequest, ...] = ()
    lpr_control: str | None = None  # "start" | "stop" | None


@dataclass
class _PersonWatch:
    streak: int = 0
    last_qualifying_at: datetime | None = None

    def observe(self, qualifying: bool, at: datetime) -> None:
        if qualifying:
            self.streak += 1
            self.last_qualifying_at = at
        else:
            self.streak = 0

    def expire(self, now: datetime, stale_seconds: float) -> None:
        if (
            self.last_qualifying_at is not None
            and (now - self.last_qualifying_at).total_seconds() > stale_seconds
        ):
            self.streak = 0
            self.last_qualifying_at = None

    def active(self, threshold: int) -> bool:
        return self.streak >= threshold and self.last_qualifying_at is not None


class ParkingProcessEngine:
    """State progression for the continuous inbound parking cycle (no outbound)."""

    def __init__(self, settings: OperatorRuntimeSettings) -> None:
        self._settings = settings
        self._machine = SafetyStateMachine()
        self._phase = "idle_monitoring"
        self._now: datetime | None = None

        self._monitoring_running = False
        self._monitoring_recovering = False
        self._camera_health: dict[str, bool] = {}
        self._camera_roles: dict[str, CameraRole] = {}

        self._person = _PersonWatch()
        self._person_cameras: set[str] = set()
        self._radar_present = False
        self._radar_at: datetime | None = None

        self._trigger_streak = 0
        self._trigger_last_at: datetime | None = None
        self._trigger_camera_id = ""
        self._entry_last_evidence_at: datetime | None = None

        self._front_vehicle_center: tuple[float, float] | None = None
        self._front_vehicle_at: datetime | None = None
        self._front_stable_since: datetime | None = None

        self._plate_reads: list[tuple[str, float]] = []
        self._plate_started_at: datetime | None = None
        self._plate_number = ""
        self._plate_confidence: float | None = None
        self._lpr_active = False

        self._phase_entered_at: datetime | None = None
        self._clear_since: datetime | None = None
        self._machine_started_at: datetime | None = None
        self._plc_human_reported = False

        self._pending_plc: list[PlcRequest] = []
        self._pending_raw: list[RawEventRequest] = []
        self._pending_lpr: str | None = None

    # ------------------------------------------------------------------ inputs

    def apply_settings(self, settings: OperatorRuntimeSettings) -> None:
        self._settings = settings

    def observe_monitoring_health(self, *, running: bool, recovering: bool = False) -> None:
        self._monitoring_running = running
        self._monitoring_recovering = recovering

    def observe_camera_health(self, camera_id: str, role: CameraRole, healthy: bool) -> None:
        self._camera_health[camera_id] = healthy
        self._camera_roles[camera_id] = role

    def observe_radar(self, *, person_present: bool, received_at: datetime) -> None:
        """Radar is add-only: it can raise person-possible, never clear it early."""
        if person_present:
            self._radar_present = True
            self._radar_at = received_at

    def observe_detections(
        self,
        camera_id: str,
        role: CameraRole,
        events: tuple[DetectionEvent, ...],
        received_at: datetime,
    ) -> None:
        self._camera_roles[camera_id] = role
        person_roles = self._person_watch_roles()
        if role in person_roles:
            qualifying = any(event.label.lower() in PERSON_LABELS for event in events)
            self._person.observe(qualifying, received_at)
            if qualifying:
                self._person_cameras.add(camera_id)

        if role is TRIGGER_ROLE:
            threshold = self._settings.vehicle_trigger.min_confidence
            qualifying = any(
                event.label.lower() in VEHICLE_LABELS and event.confidence >= threshold
                for event in events
            )
            if qualifying:
                self._trigger_streak += 1
                self._trigger_last_at = received_at
                self._trigger_camera_id = camera_id
                if self._phase in ("entry_trigger_confirmed", "entry_plate_reading"):
                    self._entry_last_evidence_at = received_at
            else:
                self._trigger_streak = 0

        if role is CameraRole.front:
            best = None
            for event in events:
                if event.label.lower() in VEHICLE_LABELS:
                    if best is None or event.confidence > best.confidence:
                        best = event
            if best is not None:
                center = (best.bbox.x + best.bbox.w / 2.0, best.bbox.y + best.bbox.h / 2.0)
                epsilon = self._settings.alignment.motion_epsilon_norm
                previous = self._front_vehicle_center
                if (
                    previous is not None
                    and abs(center[0] - previous[0]) <= epsilon
                    and abs(center[1] - previous[1]) <= epsilon
                ):
                    if self._front_stable_since is None:
                        self._front_stable_since = received_at
                else:
                    self._front_stable_since = None
                self._front_vehicle_center = center
                self._front_vehicle_at = received_at
                if self._phase in ("entry_trigger_confirmed", "entry_plate_reading"):
                    self._entry_last_evidence_at = received_at

    def observe_lpr_attempt(self, attempt: Mapping[str, object], frame_height: int) -> None:
        """Collect a plate read when its bbox center sits below the configured line."""
        if self._phase != "entry_plate_reading" or frame_height <= 0:
            return
        best = attempt.get("best_plate")
        if not isinstance(best, Mapping):
            detections = attempt.get("detections")
            if isinstance(detections, (list, tuple)) and detections:
                candidates = [d for d in detections if isinstance(d, Mapping) and d.get("plate_number")]
                best = max(candidates, key=lambda d: float(d.get("confidence") or 0.0), default=None)
            else:
                best = None
        if not isinstance(best, Mapping):
            return
        plate = str(best.get("plate_number") or "").strip()
        bbox = best.get("bbox")
        if not plate or not isinstance(bbox, Mapping):
            return
        try:
            center_y = (float(bbox["y1"]) + float(bbox["y2"])) / 2.0
        except (KeyError, TypeError, ValueError):
            return
        line_y = self._settings.plate_zone.line_y_norm * frame_height
        if center_y <= line_y:
            return  # above the line: outside the machine, ignore
        confidence = float(best.get("confidence") or 0.0)
        self._plate_reads.append((plate, confidence))
        self._entry_last_evidence_at = self._now or self._entry_last_evidence_at

    # ------------------------------------------------------------------ tick

    def tick(self, now: datetime) -> EngineOutput:
        self._now = now
        self._expire_signals(now)
        uncertain = self._uncertain_reason()

        if self._phase in ("idle_monitoring", "idle_person_warning"):
            self._tick_idle(now, uncertain)
        elif self._phase in ("entry_trigger_confirmed", "entry_plate_reading"):
            self._tick_entry(now, uncertain)
        elif self._phase == "entering":
            self._tick_simple_advance(now, uncertain, "alignment", ParkingState.ALIGNMENT_GUIDE)
        elif self._phase == "alignment":
            self._tick_alignment(now, uncertain)
        elif self._phase == "parked_instruct":
            self._tick_parked(now, uncertain)
        elif self._phase in ("exit_clear_countdown", "exit_person_warning"):
            self._tick_exit_clear(now, uncertain)
        elif self._phase == "ok_sent":
            if self._person_possible():
                # Person reappeared between OK-send and machine start: regress.
                self._transition("exit_clear_countdown", ParkingState.SAFETY_CHECK, now)
                self._queue_plc("safety_status_ng", {"context": "person_after_ok"})
                self._clear_since = None
            else:
                self._enter_machine_operating(now)
        elif self._phase == "machine_operating":
            self._tick_operating(now)

        return self._build_output(uncertain)

    # ------------------------------------------------------------------ phases

    def _tick_idle(self, now: datetime, uncertain: str) -> None:
        person = self._person_possible()
        if self._phase == "idle_monitoring":
            if person:
                self._transition("idle_person_warning", ParkingState.HUMAN_DETECTED, now)
                self._queue_plc("human_detected", {"context": "idle"})
                self._plc_human_reported = True
            elif not uncertain and self._trigger_confirmed():
                self._confirm_entry(now)
        else:  # idle_person_warning
            if not person:
                self._transition("idle_monitoring", ParkingState.IDLE, now)
                if self._plc_human_reported:
                    self._queue_plc("human_clear", {"context": "idle"})
                    self._plc_human_reported = False

    def _confirm_entry(self, now: datetime) -> None:
        self._transition("entry_trigger_confirmed", ParkingState.VEHICLE_DETECTED, now)
        self._entry_last_evidence_at = now
        self._plate_reads = []
        self._plate_number = ""
        self._plate_confidence = None
        self._queue_raw(RawEventRequest(kind="vehicle_entry", camera_id=self._trigger_camera_id))
        _LOGGER.info(
            "process-engine entry confirmed camera=%s streak=%d threshold=%.2f",
            self._trigger_camera_id,
            self._trigger_streak,
            self._settings.vehicle_trigger.min_confidence,
        )

    def _tick_entry(self, now: datetime, uncertain: str) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        release = self._settings.vehicle_trigger.release_seconds
        last_evidence = self._entry_last_evidence_at
        if last_evidence is not None and (now - last_evidence).total_seconds() > release:
            self._abort_to_idle(now, "진입 증거 소실 (차량 미확인)", reason="entry_released")
            return

        if self._phase == "entry_trigger_confirmed":
            self._transition("entry_plate_reading", ParkingState.PLATE_RECOGNITION, now)
            self._plate_started_at = now
            self._pending_lpr = "start"
            return

        # entry_plate_reading
        zone = self._settings.plate_zone
        decided = False
        if len(self._plate_reads) >= zone.max_reads:
            decided = True
        elif len(self._plate_reads) >= zone.min_reads_for_vote and self._has_majority():
            decided = True
        elif (
            self._plate_started_at is not None
            and (now - self._plate_started_at).total_seconds() > zone.read_timeout_seconds
        ):
            decided = True
        elif self._front_vehicle_stable(now):
            decided = True  # vehicle already arrived and stopped; stop waiting for reads
        if decided:
            self._decide_plate()
            self._pending_lpr = "stop"
            self._transition("entering", ParkingState.VEHICLE_ENTERING, now)

    def _has_majority(self) -> bool:
        counts: dict[str, int] = {}
        for plate, _confidence in self._plate_reads:
            counts[plate] = counts.get(plate, 0) + 1
        if not counts:
            return False
        top = max(counts.values())
        return top * 2 > len(self._plate_reads)

    def _decide_plate(self) -> None:
        counts: dict[str, list[float]] = {}
        for plate, confidence in self._plate_reads:
            counts.setdefault(plate, []).append(confidence)
        if not counts:
            self._plate_number = UNRECOGNIZED_PLATE
            self._plate_confidence = None
        else:
            def rank(item: tuple[str, list[float]]) -> tuple[int, float]:
                _plate, confidences = item
                return (len(confidences), sum(confidences) / len(confidences))

            plate, confidences = max(counts.items(), key=rank)
            self._plate_number = plate
            self._plate_confidence = sum(confidences) / len(confidences)
            self._queue_raw(
                RawEventRequest(
                    kind="plate",
                    plate_number=plate,
                    confidence=self._plate_confidence,
                )
            )
        _LOGGER.info(
            "process-engine plate decided plate=%s reads=%d",
            self._plate_number,
            len(self._plate_reads),
        )

    def _tick_simple_advance(
        self, now: datetime, uncertain: str, next_phase: str, next_state: ParkingState
    ) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        self._transition(next_phase, next_state, now)

    def _tick_alignment(self, now: datetime, uncertain: str) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        if self._front_vehicle_stable(now):
            self._transition("parked_instruct", ParkingState.PARKED, now)

    def _tick_parked(self, now: datetime, uncertain: str) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        entered = self._phase_entered_at or now
        if (now - entered).total_seconds() >= self._settings.alignment.parked_instruct_seconds:
            self._transition("exit_clear_countdown", ParkingState.SAFETY_CHECK, now)
            self._clear_since = now

    def _tick_exit_clear(self, now: datetime, uncertain: str) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        person = self._person_possible()
        if self._phase == "exit_clear_countdown":
            if person:
                self._transition("exit_person_warning", ParkingState.HUMAN_DETECTED, now)
                self._queue_plc("human_detected", {"context": "pre_operation"})
                self._plc_human_reported = True
                self._clear_since = None
                return
            if self._clear_since is None:
                self._clear_since = now
            if (now - self._clear_since).total_seconds() >= self._settings.timers.exit_clear_seconds:
                self._transition("ok_sent", ParkingState.READY_FOR_OPERATION, now)
                self._queue_plc("safety_check_complete", {"clear_seconds": self._settings.timers.exit_clear_seconds})
                self._queue_plc(
                    "vehicle_parked",
                    {"plate_number": self._plate_number or UNRECOGNIZED_PLATE},
                )
        else:  # exit_person_warning
            if not person:
                self._transition("exit_clear_countdown", ParkingState.SAFETY_CHECK, now)
                if self._plc_human_reported:
                    self._queue_plc("human_clear", {"context": "pre_operation"})
                    self._plc_human_reported = False
                self._clear_since = now

    def _enter_machine_operating(self, now: datetime) -> None:
        self._transition("machine_operating", ParkingState.AI_STOP, now)
        self._machine_started_at = now
        self._queue_plc("ai_stopped", {"reason": "machine_operating"})
        self._queue_raw(RawEventRequest(kind="vehicle_session_end", reason="parking_started"))

    def _tick_operating(self, now: datetime) -> None:
        # Machine is out of our control: never abort early; person watch warns only.
        started = self._machine_started_at or now
        if (now - started).total_seconds() >= self._settings.timers.machine_operation_seconds:
            self._reset_cycle()
            self._transition("idle_monitoring", ParkingState.IDLE, now)
            _LOGGER.info("process-engine machine operation window elapsed; cycle complete")

    # ------------------------------------------------------------------ helpers

    def _abort_to_idle(self, now: datetime, detail: str, *, reason: str = "uncertainty") -> None:
        _LOGGER.warning("process-engine abort to IDLE phase=%s reason=%s (%s)", self._phase, reason, detail)
        if self._lpr_active or self._pending_lpr == "start":
            self._pending_lpr = "stop"
        self._queue_raw(RawEventRequest(kind="vehicle_session_end", reason=f"{reason}:{detail}"))
        self._reset_cycle()
        self._transition("idle_monitoring", ParkingState.IDLE, now)

    def _reset_cycle(self) -> None:
        self._trigger_streak = 0
        self._trigger_last_at = None
        self._entry_last_evidence_at = None
        self._plate_reads = []
        self._plate_started_at = None
        self._front_stable_since = None
        self._front_vehicle_center = None
        self._front_vehicle_at = None
        self._clear_since = None
        self._machine_started_at = None
        self._plate_number = ""
        self._plate_confidence = None
        if self._plc_human_reported:
            self._plc_human_reported = False

    def _transition(self, phase: str, state: ParkingState, now: datetime) -> None:
        if state is not self._machine.current_state:
            self._machine.transition(state)
        self._phase = phase
        self._phase_entered_at = now

    def _expire_signals(self, now: datetime) -> None:
        debounce = self._settings.person_debounce
        self._person.expire(now, debounce.stale_seconds)
        if self._person.streak == 0:
            self._person_cameras.clear()
        if self._radar_at is not None and (
            (now - self._radar_at).total_seconds() > debounce.stale_seconds
        ):
            self._radar_present = False
            self._radar_at = None
        trigger = self._settings.vehicle_trigger
        if self._trigger_last_at is not None and (
            (now - self._trigger_last_at).total_seconds() > trigger.stale_seconds
        ):
            self._trigger_streak = 0
            self._trigger_last_at = None
        # Front stability requires fresh observations.
        if self._front_vehicle_at is not None and (
            (now - self._front_vehicle_at).total_seconds() > 2.0
        ):
            self._front_stable_since = None
            self._front_vehicle_center = None

    def _person_watch_roles(self) -> frozenset[CameraRole]:
        if self._phase == "machine_operating":
            return OPERATING_PERSON_ROLES
        if self._phase in ("parked_instruct", "exit_clear_countdown", "exit_person_warning"):
            return EXIT_PERSON_ROLES
        return IDLE_PERSON_ROLES

    def _person_threshold(self) -> int:
        debounce = self._settings.person_debounce
        if self._phase in ("parked_instruct", "exit_clear_countdown", "exit_person_warning"):
            return debounce.parked_frames
        return debounce.idle_frames

    def _person_possible(self) -> bool:
        return self._person.active(self._person_threshold()) or self._radar_present

    def _trigger_confirmed(self) -> bool:
        return (
            self._trigger_streak >= self._settings.vehicle_trigger.consecutive_frames
            and self._trigger_last_at is not None
        )

    def _front_vehicle_stable(self, now: datetime) -> bool:
        return (
            self._front_stable_since is not None
            and (now - self._front_stable_since).total_seconds()
            >= self._settings.alignment.stop_stable_seconds
        )

    def _uncertain_reason(self) -> str:
        if not self._monitoring_running:
            return "AI 감시 추론이 실행 중이 아닙니다"
        if self._monitoring_recovering:
            return "AI 감시 추론 복구 중"
        unhealthy = sorted(
            camera_id
            for camera_id, healthy in self._camera_health.items()
            if not healthy
            and self._camera_roles.get(camera_id) in (CameraRole.front, CameraRole.rear_side)
        )
        if unhealthy:
            return f"필수 카메라 이상: {', '.join(unhealthy)}"
        return ""

    def _queue_plc(self, name: str, payload: Mapping[str, object]) -> None:
        merged = dict(payload)
        merged["simulated"] = True
        self._pending_plc.append(PlcRequest(name=name, payload=merged))

    def _queue_raw(self, request: RawEventRequest) -> None:
        self._pending_raw.append(request)

    def _build_output(self, uncertain: str) -> EngineOutput:
        state = self._machine.current_state
        copy_key: str | None = None
        warning = ""
        audio: str | None = None
        if self._phase == "idle_person_warning":
            copy_key = COPY_IDLE_PERSON
            cameras = ", ".join(sorted(self._person_cameras)) or "레이더"
            warning = f"사람이 감지되었습니다. ({cameras})"
        elif self._phase == "exit_person_warning":
            copy_key = COPY_EXIT_PERSON
            warning = "주차가 시작될 예정이므로 바깥으로 나가 주십시오."
            audio = AUDIO_EXIT_WARNING
        elif self._phase == "alignment":
            copy_key = COPY_ALIGNMENT_FRONT
        elif self._phase == "parked_instruct":
            copy_key = COPY_PARKED
        elif self._phase == "machine_operating" and self._person_possible():
            warning = "주차기 동작 중 사람 감지! 즉시 확인이 필요합니다."

        plc = tuple(self._pending_plc)
        raw = tuple(self._pending_raw)
        lpr = self._pending_lpr
        self._pending_plc = []
        self._pending_raw = []
        self._pending_lpr = None
        if lpr == "start":
            self._lpr_active = True
        elif lpr == "stop":
            self._lpr_active = False

        return EngineOutput(
            public_state=state,
            phase=self._phase,
            copy_key=copy_key,
            plate_number=self._plate_number,
            warning_text=warning,
            uncertain_reason=uncertain,
            person_possible=self._person_possible(),
            audio_cue=audio,
            show_wheel_guides=self._phase in ("entering", "alignment"),
            plc_requests=plc,
            raw_events=raw,
            lpr_control=lpr,
        )

    # ------------------------------------------------------------------ introspection

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def public_state(self) -> ParkingState:
        return self._machine.current_state


__all__ = [
    "AUDIO_EXIT_WARNING",
    "COPY_ALIGNMENT_FRONT",
    "COPY_EXIT_PERSON",
    "COPY_IDLE_PERSON",
    "COPY_PARKED",
    "EngineOutput",
    "ParkingProcessEngine",
    "PlcRequest",
    "RawEventRequest",
    "UNRECOGNIZED_PLATE",
]
