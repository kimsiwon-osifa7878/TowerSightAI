from datetime import datetime, timedelta, timezone

from towersightai.config.settings import CameraRole
from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.process.engine import (
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


def _event(label: str, confidence: float, *, x: float = 0.4, y: float = 0.4, at: datetime = T0):
    return DetectionEvent(
        camera_id="unused",
        label=label,
        confidence=confidence,
        bbox=BoundingBox(x=x, y=y, w=0.2, h=0.2),
        timestamp=at,
    )


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
    _send_trigger(engine, now)
    out = engine.tick(now + timedelta(seconds=1))
    assert out.public_state is ParkingState.VEHICLE_DETECTED
    assert any(r.kind == "vehicle_entry" and r.camera_id == "cam-right" for r in out.raw_events)
    now = now + timedelta(seconds=2)
    out = engine.tick(now)
    assert out.public_state is ParkingState.PLATE_RECOGNITION
    assert out.lpr_control == "start"
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


def test_radar_is_add_only_person_source():
    engine = _engine()
    engine.observe_radar(person_present=True, received_at=T0)
    out = engine.tick(T0 + timedelta(seconds=1))
    assert out.public_state is ParkingState.HUMAN_DETECTED
    assert out.person_possible
    # radar "no person" never arrives as a clearing signal; only staleness clears
    out = engine.tick(T0 + timedelta(seconds=2))
    assert out.public_state is ParkingState.HUMAN_DETECTED
    out = engine.tick(T0 + timedelta(seconds=6))
    assert out.public_state is ParkingState.IDLE


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


def test_plate_timeout_yields_unrecognized():
    engine = _engine()
    now = _drive_to_plate_reading(engine, T0)
    # keep entry evidence alive with right-side vehicle, but no plate reads
    current = now
    while (current - now).total_seconds() <= 31:
        current += timedelta(seconds=1)
        engine.observe_detections(
            "cam-right", CameraRole.opposite_side, (_event("car", 0.8, at=current),), current
        )
        out = engine.tick(current)
        if out.public_state is not ParkingState.PLATE_RECOGNITION:
            break
    assert out.public_state is ParkingState.VEHICLE_ENTERING
    assert out.plate_number == UNRECOGNIZED_PLATE


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
