"""Continuous parking-process engine.

Pure Python (no Qt), deterministic under an injected clock, unit-testable from
synthetic events. The engine consumes normalized detection batches, LPR
attempts, and health signals; on each 1 Hz ``tick`` it advances an
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
- The LD2410 radar is **not an input**. It is a verification-only sensor: its
  readings are recorded to raw data for the offline camera-vs-radar study and
  never reach the engine, the driver display, or any operating decision. Person
  presence comes from the cameras alone.
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
COPY_VEHICLE_EXITING = "vehicle_exiting"

AUDIO_EXIT_WARNING = "exit_warning"


@dataclass(frozen=True)
class PlcRequest:
    name: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class _PlateRead:
    """One accepted 1 Hz read, kept with its frame so the winner can be cropped for evidence."""

    plate: str
    confidence: float
    source_image_path: str = ""
    bbox: Mapping[str, float] | None = None


@dataclass(frozen=True)
class RawEventRequest:
    kind: str  # "vehicle_entry" | "vehicle_session_end" | "plate" | "plate_attempt"
    camera_id: str = ""
    reason: str = ""
    plate_number: str = ""
    confidence: float | None = None
    # plate_attempt: one 1 Hz front-camera read. ``accepted`` is False for a plate seen above the
    # 차량진입선 (outside the machine) or for a frame with no plate at all — the analysis dashboard
    # needs the rejected reads to measure how often LPR actually sees a plate during an entry.
    accepted: bool = True
    bbox: Mapping[str, float] | None = None
    # plate: the majority-vote outcome. ``recognized`` is False for 미인식 so every entry leaves
    # an explicit result instead of silence.
    recognized: bool = True
    reads: int = 0
    # Winning read's frame + plate box, so the evidence layer can store the plate crop.
    source_image_path: str = ""


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

        self._trigger_streak = 0
        self._trigger_last_at: datetime | None = None
        self._trigger_camera_id = ""
        self._entry_last_evidence_at: datetime | None = None

        self._front_vehicle_center: tuple[float, float] | None = None
        self._front_vehicle_at: datetime | None = None
        self._front_stable_since: datetime | None = None

        self._plate_reads: list[_PlateRead] = []
        # When the front camera first reported a vehicle during plate reading. The read timeout
        # runs from here, not from the opposite_side trigger.
        self._plate_front_seen_at: datetime | None = None
        # 입고/출고 판별 (front camera box shape + plate presence)
        self._classify_started_at: datetime | None = None
        self._entry_shape_streak = 0
        self._exit_shape_streak = 0
        self._exit_started_at: datetime | None = None
        self._exit_last_evidence_at: datetime | None = None
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
                if self._phase == "entry_plate_reading" and self._plate_front_seen_at is None:
                    self._plate_front_seen_at = received_at
                if self._phase in ("entry_classify", "vehicle_exiting"):
                    self._exit_last_evidence_at = received_at
                self._observe_vehicle_shape(best)
            elif self._phase == "entry_classify":
                # No vehicle in the front frame: neither shape is being confirmed.
                self._entry_shape_streak = 0
                self._exit_shape_streak = 0

    def observe_lpr_attempt(self, attempt: Mapping[str, object], frame_height: int) -> None:
        """Collect a plate read when its bbox center sits below the configured line."""
        if self._phase not in ("entry_classify", "entry_plate_reading") or frame_height <= 0:
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
            self._queue_plate_attempt("", None, accepted=False, reason="no_plate_detected")
            return
        plate = str(best.get("plate_number") or "").strip()
        bbox = best.get("bbox")
        confidence = float(best.get("confidence") or 0.0)
        if not plate or not isinstance(bbox, Mapping):
            self._queue_plate_attempt(plate, confidence, accepted=False, reason="no_plate_bbox")
            return
        try:
            center_y = (float(bbox["y1"]) + float(bbox["y2"])) / 2.0
        except (KeyError, TypeError, ValueError):
            self._queue_plate_attempt(plate, confidence, accepted=False, reason="invalid_bbox")
            return
        line_y = self._settings.plate_zone.line_y_norm * frame_height
        normalized = self._normalized_bbox(bbox, frame_height)
        if center_y <= line_y:
            # Above the 차량진입선: the plate is outside the machine, so it never feeds the vote.
            self._queue_plate_attempt(plate, confidence, accepted=False, reason="above_entry_line", bbox=normalized)
            return
        source = str(attempt.get("source_image") or "")
        self._plate_reads.append(_PlateRead(plate, confidence, source, normalized))
        self._queue_plate_attempt(plate, confidence, accepted=True, reason="", bbox=normalized)
        self._entry_last_evidence_at = self._now or self._entry_last_evidence_at

    def _observe_vehicle_shape(self, event: DetectionEvent) -> None:
        """Front-camera box shape → entering (narrow, facing the camera) or exiting (fills the
        frame sideways). Width is the primary signal; a sliver of a car driving in can show a
        large aspect ratio but never a near-full-frame width."""
        direction = self._settings.vehicle_direction
        width, height = event.bbox.w, event.bbox.h
        if height <= 0:
            return
        aspect = width / height
        if width >= direction.exit_min_width_norm and aspect >= direction.exit_min_aspect:
            self._exit_shape_streak += 1
            self._entry_shape_streak = 0
        elif width >= direction.entry_min_width_norm and aspect <= direction.entry_max_aspect:
            self._entry_shape_streak += 1
            self._exit_shape_streak = 0
        else:  # ambiguous (partly visible car, odd angle): neither streak advances
            self._entry_shape_streak = 0
            self._exit_shape_streak = 0

    def _queue_plate_attempt(
        self,
        plate: str,
        confidence: float | None,
        *,
        accepted: bool,
        reason: str,
        bbox: Mapping[str, float] | None = None,
    ) -> None:
        self._queue_raw(
            RawEventRequest(
                kind="plate_attempt",
                camera_id="front",
                plate_number=plate,
                confidence=confidence,
                accepted=accepted,
                reason=reason,
                bbox=bbox,
            )
        )

    @staticmethod
    def _normalized_bbox(bbox: Mapping[str, object], frame_height: int) -> dict[str, float] | None:
        try:
            return {key: round(float(bbox[key]), 2) for key in ("x1", "y1", "x2", "y2")}
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ tick

    def tick(self, now: datetime) -> EngineOutput:
        self._now = now
        self._expire_signals(now)
        uncertain = self._uncertain_reason()

        if self._phase in ("idle_monitoring", "idle_person_warning"):
            self._tick_idle(now, uncertain)
        elif self._phase == "entry_classify":
            self._tick_classify(now, uncertain)
        elif self._phase == "vehicle_exiting":
            self._tick_vehicle_exiting(now)
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
        """A vehicle is at the trigger camera. Which direction it is going is still unknown, so
        stay on the IDLE surface and classify first — an exiting car must never be announced as
        an entry (field observation 2026-09-16)."""
        self._transition("entry_classify", ParkingState.IDLE, now)
        self._entry_last_evidence_at = now
        self._exit_last_evidence_at = now
        self._classify_started_at = now
        self._entry_shape_streak = 0
        self._exit_shape_streak = 0
        self._plate_reads = []
        self._plate_number = ""
        self._plate_confidence = None
        self._plate_front_seen_at = None
        self._pending_lpr = "start"  # the plate is the primary direction signal
        _LOGGER.info(
            "process-engine vehicle at trigger camera=%s streak=%d; classifying direction",
            self._trigger_camera_id,
            self._trigger_streak,
        )

    def _tick_classify(self, now: datetime, uncertain: str) -> None:
        if uncertain:
            self._abort_to_idle(now, uncertain)
            return
        release = self._settings.vehicle_trigger.release_seconds
        last = self._exit_last_evidence_at or self._entry_last_evidence_at
        if last is not None and (now - last).total_seconds() > release:
            # Nothing reached the front camera: the trigger saw traffic outside the open door.
            self._pending_lpr = "stop"
            self._reset_cycle()
            self._transition("idle_monitoring", ParkingState.IDLE, now)
            return
        direction = self._settings.vehicle_direction
        if self._plate_reads:
            self._start_entry(now, "plate")
            return
        if self._exit_shape_streak >= direction.consecutive_frames:
            self._start_exit(now)
            return
        if self._entry_shape_streak >= direction.consecutive_frames:
            self._start_entry(now, "front_shape")
            return
        if (
            self._classify_started_at is not None
            and (now - self._classify_started_at).total_seconds() > direction.classify_timeout_seconds
        ):
            # Owner rule: no plate and no confident shape → never assume an entry, report an error.
            self._pending_lpr = "stop"
            self._abort_to_idle(now, "차량 방향을 판별하지 못했습니다 (번호판 미인식)", reason="direction_unknown")

    def _start_entry(self, now: datetime, evidence: str) -> None:
        self._transition("entry_trigger_confirmed", ParkingState.VEHICLE_DETECTED, now)
        self._entry_last_evidence_at = now
        self._queue_raw(
            RawEventRequest(kind="vehicle_entry", camera_id=self._trigger_camera_id, reason=evidence)
        )
        _LOGGER.info("process-engine entry confirmed by %s", evidence)

    def _start_exit(self, now: datetime) -> None:
        """Retrieval: the driver display says 출고중 and nothing else. A person around an exiting
        car is normal, so the person watch does not warn here (owner decision 2026-09-16)."""
        self._transition("vehicle_exiting", ParkingState.IDLE, now)
        self._exit_started_at = now
        self._exit_last_evidence_at = now
        self._pending_lpr = "stop"
        self._queue_raw(
            RawEventRequest(kind="vehicle_exit_start", camera_id=self._trigger_camera_id)
        )
        _LOGGER.info("process-engine vehicle exiting (front box is side-on, no plate)")

    def _tick_vehicle_exiting(self, now: datetime) -> None:
        release = self._settings.vehicle_trigger.release_seconds
        last = self._exit_last_evidence_at
        gone = last is not None and (now - last).total_seconds() > release
        started = self._exit_started_at or now
        timed_out = (now - started).total_seconds() > self._settings.timers.machine_operation_seconds
        if gone or timed_out:
            self._queue_raw(
                RawEventRequest(
                    kind="vehicle_exit_end",
                    reason="vehicle_gone" if gone else "timeout",
                )
            )
            _LOGGER.info("process-engine vehicle exit finished reason=%s", "vehicle_gone" if gone else "timeout")
            self._reset_cycle()
            self._transition("idle_monitoring", ParkingState.IDLE, now)

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
        elif self._plate_read_deadline_passed(now, zone):
            decided = True
        elif self._front_vehicle_stable(now) and self._plate_read_minimum_met(now, zone):
            decided = True  # vehicle arrived and stopped, and the reader had its chance
        if decided:
            self._decide_plate()
            self._pending_lpr = "stop"
            self._transition("entering", ParkingState.VEHICLE_ENTERING, now)

    def _plate_read_deadline_passed(self, now: datetime, zone) -> bool:  # noqa: ANN001 - settings dataclass
        """Timeout from the front camera's first sight of the car, with a hard cap from the
        trigger for a car that never arrives (false trigger on traffic outside the open door)."""
        if self._plate_front_seen_at is not None:
            return (now - self._plate_front_seen_at).total_seconds() > zone.read_timeout_seconds
        if self._plate_started_at is None:
            return False
        return (now - self._plate_started_at).total_seconds() > zone.arrival_timeout_seconds

    def _plate_read_minimum_met(self, now: datetime, zone) -> bool:  # noqa: ANN001 - settings dataclass
        """A stationary car may only end the vote once a read landed or the reader has had
        ``min_read_seconds`` in front of the car."""
        if self._plate_reads:
            return True
        reference = self._plate_front_seen_at or self._plate_started_at
        if reference is None:
            return False
        return (now - reference).total_seconds() >= zone.min_read_seconds

    def _has_majority(self) -> bool:
        counts: dict[str, int] = {}
        for read in self._plate_reads:
            counts[read.plate] = counts.get(read.plate, 0) + 1
        if not counts:
            return False
        top = max(counts.values())
        return top * 2 > len(self._plate_reads)

    def _decide_plate(self, *, reason: str = "vote") -> None:
        """Close the plate vote. Both outcomes are recorded: a recognized plate and 미인식 alike,
        so every entry leaves an explicit raw result instead of silence."""
        counts: dict[str, list[_PlateRead]] = {}
        for read in self._plate_reads:
            counts.setdefault(read.plate, []).append(read)
        reads = len(self._plate_reads)
        if not counts:
            self._plate_number = UNRECOGNIZED_PLATE
            self._plate_confidence = None
            self._queue_raw(
                RawEventRequest(
                    kind="plate",
                    plate_number=UNRECOGNIZED_PLATE,
                    confidence=None,
                    recognized=False,
                    reads=reads,
                    reason=reason,
                )
            )
        else:
            def rank(item: tuple[str, list[_PlateRead]]) -> tuple[int, float]:
                _plate, group = item
                return (len(group), sum(r.confidence for r in group) / len(group))

            plate, group = max(counts.items(), key=rank)
            best = max(group, key=lambda r: r.confidence)
            self._plate_number = plate
            self._plate_confidence = sum(r.confidence for r in group) / len(group)
            self._queue_raw(
                RawEventRequest(
                    kind="plate",
                    plate_number=plate,
                    confidence=self._plate_confidence,
                    recognized=True,
                    reads=reads,
                    reason=reason,
                    # Frame + box of the clearest winning read → plate image and crop evidence.
                    source_image_path=best.source_image_path,
                    bbox=best.bbox,
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
        if self._phase in ("entry_classify", "entry_plate_reading") and self._plate_reads and not self._plate_number:
            # The entry is being abandoned mid-vote; record what the reads amounted to so the
            # session is not silent about the plate.
            self._decide_plate(reason=f"aborted:{reason}")
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
        self._plate_front_seen_at = None
        self._classify_started_at = None
        self._entry_shape_streak = 0
        self._exit_shape_streak = 0
        self._exit_started_at = None
        self._exit_last_evidence_at = None
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
        # Cameras only. The radar is verification-only data and must never influence a
        # person decision, a driver warning, or the parking machine's operation.
        if self._phase == "vehicle_exiting":
            # A person next to a car being retrieved is normal; the machine is not about to move
            # for us and final OK is blocked in IDLE anyway (owner decision 2026-09-16).
            return False
        return self._person.active(self._person_threshold())

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
        elif self._phase == "vehicle_exiting":
            copy_key = COPY_VEHICLE_EXITING
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
    "COPY_VEHICLE_EXITING",
    "RawEventRequest",
    "UNRECOGNIZED_PLATE",
]
