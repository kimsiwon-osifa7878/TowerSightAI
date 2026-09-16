from datetime import datetime, timedelta, timezone

from towersightai.config.settings import CameraRole
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.process.engine import (
    COPY_VEHICLE_EXITING,
    AUDIO_EXIT_WARNING,
    COPY_EXIT_PERSON,
    COPY_IDLE_PERSON,
    ParkingProcessEngine,
    UNRECOGNIZED_PLATE,
)
from towersightai.process.settings_store import OperatorRuntimeSettings
from towersightai.state_machine.core import ParkingState

T0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

CAMERAS = {
    "cam-ceiling": CameraRole.ceiling,
    "cam-front": CameraRole.front,
    "cam-left": CameraRole.rear_side,
    "cam-right": CameraRole.opposite_side,
}


def _event(label: str, confidence: float, *, x: float = 0.4, y: float = 0.4, at: datetime = T0,
           w: float = 0.2, h: float = 0.2):
    return DetectionEvent(
        camera_id="unused",
        label=label,
        confidence=confidence,
        bbox=BoundingBox(x=x, y=y, w=w, h=h),
        timestamp=at,
    )


# Front-camera box shapes measured at 구로 신안타워 2026-09-16.
def _front_entering(at: datetime, x: float = 0.23):
    """Car driving in, facing the camera: width ~0.54, w/h ~1.6."""
    return _event("car", 0.9, at=at, x=x, y=0.30, w=0.54, h=0.34)


def _front_exiting(at: datetime):
    """Car being retrieved, side-on: fills the frame, w/h ~3.9."""
    return _event("car", 0.9, at=at, x=0.0, y=0.22, w=0.98, h=0.25)


def _feed_front(engine, at: datetime, factory, count: int = 5, step_ms: int = 100):
    for index in range(count):
        stamp = at + timedelta(milliseconds=step_ms * index)
        engine.observe_detections("cam-front", CameraRole.front, (factory(stamp),), stamp)
    return at + timedelta(milliseconds=step_ms * count)


def _engine(**settings_kwargs) -> ParkingProcessEngine:
    engine = ParkingProcessEngine(OperatorRuntimeSettings(**settings_kwargs))
    engine.observe_monitoring_health(running=True)
    for camera_id, role in CAMERAS.items():
        engine.observe_camera_health(camera_id, role, True)
    return engine


def _send_trigger(engine: ParkingProcessEngine, at: datetime, *, confidence: float = 0.8, count: int = 5):
    for index in range(count):
        stamp = at + timedelta(milliseconds=100 * index)
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("car", confidence, at=stamp),), stamp
        )


def _lpr_attempt(plate: str, center_y: float, height: int = 1000, confidence: float = 0.9):
    return {
        "status": "recognized",
        "best_plate": {
            "plate_number": plate,
            "confidence": confidence,
            "bbox": {"x1": 100, "y1": center_y - 20, "x2": 300, "y2": center_y + 20},
        },
    }, height


def _drive_to_plate_reading(engine: ParkingProcessEngine, now: datetime) -> datetime:
    """Trigger → direction classification (front-facing car = entry) → plate reading."""
    _send_trigger(engine, now)
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE  # classifying: nothing announced yet
    assert out.phase == "entry_classify"
    assert out.lpr_control == "start"
    _feed_front(engine, now + timedelta(seconds=1), _front_entering)
    out = engine.tick(now + timedelta(seconds=2))
    assert out.public_state is ParkingState.VEHICLE_DETECTED
    assert any(r.kind == "vehicle_entry" and r.camera_id == "cam-right" for r in out.raw_events)
    now = now + timedelta(seconds=3)
    out = engine.tick(now)
    assert out.public_state is ParkingState.PLATE_RECOGNITION
    return now


def _drive_to_safety_check(engine: ParkingProcessEngine, now: datetime) -> datetime:
    now = _drive_to_plate_reading(engine, now)
    for _ in range(3):
        engine.observe_lpr_attempt(*_lpr_attempt("12가3456", center_y=800))
    now += timedelta(seconds=1)
    out = engine.tick(now)
    assert out.public_state is ParkingState.VEHICLE_ENTERING
    assert out.lpr_control == "stop"
    assert out.plate_number == "12가3456"
    now += timedelta(seconds=1)
    out = engine.tick(now)
    assert out.public_state is ParkingState.ALIGNMENT_GUIDE
    assert out.show_wheel_guides
    # front vehicle stationary for stop_stable_seconds (5 s), refreshed every second
    for step in range(7):
        stamp = now + timedelta(seconds=step)
        engine.observe_detections(
            "cam-front", CameraRole.front, (_event("car", 0.9, x=0.4, y=0.5, at=stamp),), stamp
        )
        out = engine.tick(stamp)
    assert out.public_state is ParkingState.PARKED
    now = now + timedelta(seconds=6)
    # parked instruct window (5 s)
    now += timedelta(seconds=6)
    out = engine.tick(now)
    assert out.public_state is ParkingState.SAFETY_CHECK
    return now


def test_full_cycle_happy_path():
    engine = _engine()
    now = _drive_to_safety_check(engine, T0)
    # 10 s clear countdown
    out = engine.tick(now + timedelta(seconds=10))
    assert out.public_state is ParkingState.READY_FOR_OPERATION
    names = [r.name for r in out.plc_requests]
    assert names == ["safety_check_complete", "vehicle_parked"]
    parked = out.plc_requests[1]
    assert parked.payload["plate_number"] == "12가3456"
    assert parked.payload["simulated"] is True
    now = now + timedelta(seconds=11)
    out = engine.tick(now)
    assert out.public_state is ParkingState.AI_STOP
    assert any(r.name == "ai_stopped" for r in out.plc_requests)
    assert any(
        r.kind == "vehicle_session_end" and r.reason == "parking_started" for r in out.raw_events
    )
    # 60 s machine operation → IDLE, cycle restarts
    out = engine.tick(now + timedelta(seconds=60))
    assert out.public_state is ParkingState.IDLE
    assert engine.phase == "idle_monitoring"


def test_all_plc_requests_are_simulated():
    engine = _engine()
    now = _drive_to_safety_check(engine, T0)
    collected = []
    for delta in (10, 11, 71):
        collected.extend(engine.tick(now + timedelta(seconds=delta)).plc_requests)
    assert collected
    assert all(request.payload["simulated"] is True for request in collected)


def test_trigger_needs_confidence_and_streak():
    engine = _engine()
    _send_trigger(engine, T0, confidence=0.5)  # below 0.6 threshold
    assert engine.tick(T0 + timedelta(seconds=1)).public_state is ParkingState.IDLE
    _send_trigger(engine, T0 + timedelta(seconds=2), confidence=0.8, count=4)  # below 5 frames
    assert engine.tick(T0 + timedelta(seconds=3)).public_state is ParkingState.IDLE


def test_entry_releases_when_evidence_disappears():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    # no further vehicle/plate evidence for > release_seconds (5 s)
    out = engine.tick(now + timedelta(seconds=6))
    assert out.public_state is ParkingState.IDLE
    assert any(
        r.kind == "vehicle_session_end" and "entry_released" in r.reason for r in out.raw_events
    )
    assert out.lpr_control == "stop"


def test_idle_person_debounce_and_clear():
    engine = _engine()
    stamp = T0
    engine.observe_detections("cam-left", CameraRole.rear_side, (_event("person", 0.9),), stamp)
    out = engine.tick(stamp + timedelta(milliseconds=500))
    assert out.public_state is ParkingState.IDLE  # single frame: not yet
    engine.observe_detections("cam-left", CameraRole.rear_side, (_event("person", 0.9),), stamp)
    out = engine.tick(stamp + timedelta(seconds=1))
    assert out.public_state is ParkingState.HUMAN_DETECTED
    assert out.copy_key == COPY_IDLE_PERSON
    assert "cam-left" in out.warning_text
    assert [r.name for r in out.plc_requests] == ["human_detected"]
    # stale (3 s without person) → clear
    out = engine.tick(stamp + timedelta(seconds=5))
    assert out.public_state is ParkingState.IDLE
    assert [r.name for r in out.plc_requests] == ["human_clear"]


def test_engine_has_no_radar_input_at_all():
    """The LD2410 is verification-only data (owner decision 2026-09-16): it is recorded to raw
    data for the camera-vs-radar study and must never reach the engine, the driver display, or the
    parking machine's operation. Person presence comes from the cameras alone."""
    engine = _engine()
    assert not hasattr(engine, "observe_radar")
    assert not any("radar" in name.lower() for name in vars(engine))
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE
    assert not out.person_possible


def test_opposite_side_person_ignored_in_idle():
    engine = _engine()
    for _ in range(5):
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("person", 0.95),), T0
        )
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE
    assert not out.person_possible


def test_person_blocks_entry_trigger_in_idle():
    engine = _engine()
    engine.observe_detections("cam-front", CameraRole.front, (_event("person", 0.9),), T0)
    engine.observe_detections("cam-front", CameraRole.front, (_event("person", 0.9),), T0)
    _send_trigger(engine, T0)
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.public_state is ParkingState.HUMAN_DETECTED  # person wins over trigger


def test_exit_warning_repeats_audio_and_resets_countdown():
    engine = _engine()
    now = _drive_to_safety_check(engine, T0)
    # 6 s into countdown a person appears on the left camera
    for _ in range(2):
        engine.observe_detections(
            "cam-left", CameraRole.rear_side, (_event("person", 0.9),), now + timedelta(seconds=6)
        )
    out = engine.tick(now + timedelta(seconds=6))
    assert out.public_state is ParkingState.HUMAN_DETECTED
    assert out.copy_key == COPY_EXIT_PERSON
    assert out.audio_cue == AUDIO_EXIT_WARNING
    assert "바깥으로 나가" in out.warning_text
    # audio repeats while the person stays
    for _ in range(2):
        engine.observe_detections(
            "cam-left", CameraRole.rear_side, (_event("person", 0.9),), now + timedelta(seconds=7)
        )
    assert engine.tick(now + timedelta(seconds=7)).audio_cue == AUDIO_EXIT_WARNING
    # person leaves → back to SAFETY_CHECK, countdown restarts from zero
    out = engine.tick(now + timedelta(seconds=11))
    assert out.public_state is ParkingState.SAFETY_CHECK
    out = engine.tick(now + timedelta(seconds=15))  # only 4 s after restart
    assert out.public_state is ParkingState.SAFETY_CHECK
    out = engine.tick(now + timedelta(seconds=21))  # 10 s after restart
    assert out.public_state is ParkingState.READY_FOR_OPERATION


def test_uncertainty_aborts_entry_to_idle():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    engine.observe_monitoring_health(running=False)
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE
    assert out.uncertain_reason
    assert any(r.kind == "vehicle_session_end" and "uncertainty" in r.reason for r in out.raw_events)


def test_required_camera_failure_aborts_entry():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    engine.observe_camera_health("cam-front", CameraRole.front, False)
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE
    assert "cam-front" in out.uncertain_reason


def test_machine_operating_never_aborts_but_warns():
    engine = _engine()
    now = _drive_to_safety_check(engine, T0)
    engine.tick(now + timedelta(seconds=10))
    now = now + timedelta(seconds=11)
    assert engine.tick(now).public_state is ParkingState.AI_STOP
    # uncertainty + person during operation: stays AI_STOP, warns, completes on time
    engine.observe_monitoring_health(running=False)
    stamp = now + timedelta(seconds=30)
    for _ in range(2):
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("person", 0.9),), stamp
        )
    out = engine.tick(stamp)
    assert out.public_state is ParkingState.AI_STOP
    assert "사람 감지" in out.warning_text
    engine.observe_monitoring_health(running=True)
    out = engine.tick(now + timedelta(seconds=60))
    assert out.public_state is ParkingState.IDLE


def _drive_plate_phase(engine, now, seconds, *, front_car=True, moving=True):
    """Hold the entry alive for ``seconds``; optionally park a car in the front camera."""
    current = now
    out = None
    step = 0
    while (current - now).total_seconds() <= seconds:
        current += timedelta(seconds=1)
        step += 1
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("car", 0.8, at=current),), current
        )
        if front_car:
            offset = 0.2 if moving and step % 2 else 0.0
            engine.observe_detections(
                "cam-front",
                CameraRole.front,
                (_event("car", 0.9, at=current, x=0.3 + offset),),
                current,
            )
        out = engine.tick(current)
        if out.public_state is not ParkingState.PLATE_RECOGNITION:
            break
    return current, out


def test_plate_timeout_runs_from_the_front_cameras_first_sight():
    """The read window must start when the car reaches the front camera, not at the
    opposite_side trigger: field data 2026-09-16 had entries whose car arrived 20-80 s late."""
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    # 25 s with no front vehicle at all: the read timeout has not even started.
    current, out = _drive_plate_phase(engine, now, 25, front_car=False)
    assert out.public_state is ParkingState.PLATE_RECOGNITION
    # The car finally arrives; the 30 s read window starts here.
    current, out = _drive_plate_phase(engine, current, 25)
    assert out.public_state is ParkingState.PLATE_RECOGNITION
    current, out = _drive_plate_phase(engine, current, 10)
    assert out.public_state is ParkingState.VEHICLE_ENTERING
    assert out.plate_number == UNRECOGNIZED_PLATE


def test_plate_phase_gives_up_when_the_car_never_reaches_the_front_camera():
    """A false trigger on traffic outside the open door must still resolve, via the arrival cap."""
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    _current, out = _drive_plate_phase(engine, now, 62, front_car=False)
    assert out.public_state is ParkingState.VEHICLE_ENTERING
    assert out.plate_number == UNRECOGNIZED_PLATE


def test_stationary_front_car_does_not_end_the_vote_before_the_reader_had_a_chance():
    """Field data 2026-09-16: 5 of 9 entries ended after 1-2 reads because the car was already
    stationary in front. The reader now gets min_read_seconds before that short-circuit."""
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    current, out = _drive_plate_phase(engine, now, 6, moving=False)
    assert out.public_state is ParkingState.PLATE_RECOGNITION  # would have decided immediately before
    # A read landing lets the stationary car end the vote right away.
    engine.observe_lpr_attempt(*_lpr_attempt("12가3456", center_y=800))
    _current, out = _drive_plate_phase(engine, current, 2, moving=False)
    assert out.public_state is ParkingState.VEHICLE_ENTERING
    assert out.plate_number == "12가3456"


def test_decided_plate_carries_the_winning_frame_and_box_for_the_crop():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    attempt, height = _lpr_attempt("12가3456", center_y=800, confidence=0.7)
    attempt["source_image"] = "/tmp/frames/frame-a.png"
    engine.observe_lpr_attempt(attempt, height)
    best, height = _lpr_attempt("12가3456", center_y=810, confidence=0.95)
    best["source_image"] = "/tmp/frames/frame-b.png"
    engine.observe_lpr_attempt(best, height)
    third, height = _lpr_attempt("12가3456", center_y=805, confidence=0.6)
    third["source_image"] = "/tmp/frames/frame-c.png"
    engine.observe_lpr_attempt(third, height)  # min_reads_for_vote = 3
    out = engine.tick(now + timedelta(seconds=1))
    plate_event = next(event for event in out.raw_events if event.kind == "plate")
    assert plate_event.plate_number == "12가3456"
    assert plate_event.source_image_path == "/tmp/frames/frame-b.png"  # highest-confidence read
    assert plate_event.bbox == {"x1": 100.0, "y1": 790.0, "x2": 300.0, "y2": 830.0}


def test_plate_majority_vote():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    for plate in ("12가3456", "12가3456", "12기3456", "12가3456"):
        engine.observe_lpr_attempt(*_lpr_attempt(plate, center_y=800))
    out = engine.tick(now + timedelta(seconds=1))
    assert out.plate_number == "12가3456"


def test_plate_above_line_ignored():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    for _ in range(5):
        engine.observe_lpr_attempt(*_lpr_attempt("99라9999", center_y=100))  # above 0.55*1000
    # keep evidence alive so entry does not release
    engine.observe_detections(
        "cam-right",
        CameraRole.opposite_side,
        (_event("car", 0.8, at=now + timedelta(seconds=1)),),
        now + timedelta(seconds=1),
    )
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.PLATE_RECOGNITION  # nothing collected


def test_every_plate_read_is_recorded_with_its_accept_reason():
    """Analysis needs the rejected reads too: they are the denominator for LPR hit rate."""
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    engine.observe_lpr_attempt(*_lpr_attempt("12가3456", center_y=800))  # below the line → counted
    engine.observe_lpr_attempt(*_lpr_attempt("99라9999", center_y=100))  # above the line → rejected
    engine.observe_lpr_attempt({"status": "no_plate", "detections": []}, 1000)  # nothing seen
    out = engine.tick(now + timedelta(seconds=1))
    attempts = [event for event in out.raw_events if event.kind == "plate_attempt"]
    assert [(a.plate_number, a.accepted, a.reason) for a in attempts] == [
        ("12가3456", True, ""),
        ("99라9999", False, "above_entry_line"),
        ("", False, "no_plate_detected"),
    ]
    assert attempts[0].camera_id == "front"
    assert attempts[0].bbox == {"x1": 100.0, "y1": 780.0, "x2": 300.0, "y2": 820.0}
    assert attempts[2].bbox is None


def test_recognized_plate_records_the_vote_size():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    for plate in ("12가3456", "12가3456", "12가3456"):
        engine.observe_lpr_attempt(*_lpr_attempt(plate, center_y=800))
    out = engine.tick(now + timedelta(seconds=1))
    plate_events = [event for event in out.raw_events if event.kind == "plate"]
    assert len(plate_events) == 1
    assert plate_events[0].recognized is True
    assert plate_events[0].plate_number == "12가3456"
    assert plate_events[0].reads == 3
    assert plate_events[0].reason == "vote"


def test_unrecognized_plate_is_recorded_instead_of_silence():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    current = now
    plate_events = []
    step = 0
    while (current - now).total_seconds() <= 40:
        current += timedelta(seconds=1)
        step += 1
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("car", 0.8, at=current),), current
        )
        engine.observe_detections(
            "cam-front",
            CameraRole.front,
            (_event("car", 0.9, at=current, x=0.3 + (0.2 if step % 2 else 0.0)),),
            current,
        )
        out = engine.tick(current)
        plate_events.extend(event for event in out.raw_events if event.kind == "plate")
        if out.public_state is not ParkingState.PLATE_RECOGNITION:
            break
    assert out.plate_number == UNRECOGNIZED_PLATE
    assert len(plate_events) == 1
    assert plate_events[0].recognized is False
    assert plate_events[0].plate_number == UNRECOGNIZED_PLATE
    assert plate_events[0].reads == 0


def test_entry_aborted_mid_vote_still_records_the_plate_outcome():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    engine.observe_lpr_attempt(*_lpr_attempt("12가3456", center_y=800))
    engine.observe_monitoring_health(running=False)
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.IDLE
    plate_events = [event for event in out.raw_events if event.kind == "plate"]
    assert len(plate_events) == 1
    assert plate_events[0].plate_number == "12가3456" and plate_events[0].reads == 1
    assert plate_events[0].reason.startswith("aborted:")
    assert any(event.kind == "vehicle_session_end" for event in out.raw_events)


# ---- 입고 / 출고 판별 ---------------------------------------------------------------------

def test_side_on_car_is_a_retrieval_and_never_announced_as_an_entry():
    """Field observation 2026-09-16: a retrieval sits side-on to the front camera and shows no
    plate, yet the engine announced 진입 준비 and gave parking guidance."""
    engine = _engine()
    _send_trigger(engine, T0)
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.phase == "entry_classify" and out.public_state is ParkingState.IDLE
    _feed_front(engine, T0 + timedelta(seconds=1), _front_exiting)
    out = engine.tick(T0 + timedelta(seconds=2))
    assert out.phase == "vehicle_exiting"
    assert out.public_state is ParkingState.IDLE
    assert out.copy_key == COPY_VEHICLE_EXITING
    assert out.lpr_control == "stop"
    assert [r.kind for r in out.raw_events] == ["vehicle_exit_start"]
    assert not any(r.kind == "vehicle_entry" for r in out.raw_events)
    assert not out.show_wheel_guides


def test_person_during_a_retrieval_is_not_reported_as_a_problem():
    engine = _engine()
    _send_trigger(engine, T0)
    engine.tick(T0 + timedelta(seconds=1))
    _feed_front(engine, T0 + timedelta(seconds=1), _front_exiting)
    out = engine.tick(T0 + timedelta(seconds=2))
    assert out.phase == "vehicle_exiting"
    for index in range(5):
        stamp = T0 + timedelta(seconds=2, milliseconds=100 * index)
        engine.observe_detections("cam-front", CameraRole.front, (_event("person", 0.95, at=stamp),), stamp)
        engine.observe_detections("cam-front", CameraRole.front, (_front_exiting(stamp),), stamp)
    out = engine.tick(T0 + timedelta(seconds=3))
    assert out.phase == "vehicle_exiting"
    assert out.person_possible is False
    assert out.warning_text == ""
    assert out.audio_cue is None


def test_retrieval_ends_when_the_car_is_gone_and_returns_to_idle():
    engine = _engine()
    _send_trigger(engine, T0)
    engine.tick(T0 + timedelta(seconds=1))
    _feed_front(engine, T0 + timedelta(seconds=1), _front_exiting)
    engine.tick(T0 + timedelta(seconds=2))
    current = T0 + timedelta(seconds=2)
    out = None
    for _ in range(10):
        current += timedelta(seconds=1)
        out = engine.tick(current)
        if out.phase != "vehicle_exiting":
            break
    assert out.phase == "idle_monitoring"
    assert out.public_state is ParkingState.IDLE
    end = [r for r in out.raw_events if r.kind == "vehicle_exit_end"]
    assert end and end[0].reason == "vehicle_gone"


def test_unclear_direction_without_a_plate_is_an_error_not_an_entry():
    """Owner rule: a car whose plate cannot be read is never confirmed as an entry."""
    engine = _engine()
    _send_trigger(engine, T0)
    engine.tick(T0 + timedelta(seconds=1))
    current = T0 + timedelta(seconds=1)
    out = None
    for _ in range(25):
        current += timedelta(seconds=1)
        # A box that matches neither shape: too narrow for a retrieval, too flat for an entry.
        engine.observe_detections(
            "cam-front", CameraRole.front, (_event("car", 0.9, at=current, w=0.60, h=0.12),), current
        )
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("car", 0.8, at=current),), current
        )
        out = engine.tick(current)
        if out.phase != "entry_classify":
            break
    assert out.phase == "idle_monitoring"
    assert out.public_state is ParkingState.IDLE
    assert "판별" in out.uncertain_reason or any(
        r.kind == "vehicle_session_end" and "direction_unknown" in r.reason for r in out.raw_events
    )
    assert not any(r.kind == "vehicle_entry" for r in out.raw_events)


def test_a_plate_read_confirms_an_entry_even_when_the_box_shape_is_unclear():
    engine = _engine()
    _send_trigger(engine, T0)
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.phase == "entry_classify"
    engine.observe_lpr_attempt(*_lpr_attempt("12가3456", center_y=800))
    out = engine.tick(T0 + timedelta(seconds=2))
    assert out.public_state is ParkingState.VEHICLE_DETECTED
    entry = [r for r in out.raw_events if r.kind == "vehicle_entry"]
    assert entry and entry[0].reason == "plate"


def test_trigger_without_any_front_vehicle_returns_to_idle_quietly():
    """Traffic outside the open door: nothing reaches the front camera, so nothing is announced."""
    engine = _engine()
    _send_trigger(engine, T0)
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.phase == "entry_classify"
    current = T0 + timedelta(seconds=1)
    for _ in range(10):
        current += timedelta(seconds=1)
        out = engine.tick(current)
        if out.phase != "entry_classify":
            break
    assert out.phase == "idle_monitoring"
    assert not any(r.kind in ("vehicle_entry", "vehicle_exit_start") for r in out.raw_events)
