import pytest

from towersightai.state_machine.core import ParkingState, SafetyStateMachine


def test_happy_path_transitions():
    sm = SafetyStateMachine()
    for state in [
        ParkingState.VEHICLE_DETECTED,
        ParkingState.PLATE_RECOGNITION,
        ParkingState.VEHICLE_ENTERING,
        ParkingState.ALIGNMENT_GUIDE,
        ParkingState.PARKED,
        ParkingState.SAFETY_CHECK,
        ParkingState.READY_FOR_OPERATION,
        ParkingState.AI_STOP,
    ]:
        sm.transition(state)
    assert sm.current_state == ParkingState.AI_STOP


def test_illegal_transition_rejected():
    sm = SafetyStateMachine()
    with pytest.raises(ValueError):
        sm.transition(ParkingState.PARKED)


def test_cycle_returns_to_idle_after_operation():
    sm = SafetyStateMachine()
    for state in [
        ParkingState.VEHICLE_DETECTED,
        ParkingState.PLATE_RECOGNITION,
        ParkingState.VEHICLE_ENTERING,
        ParkingState.ALIGNMENT_GUIDE,
        ParkingState.PARKED,
        ParkingState.SAFETY_CHECK,
        ParkingState.READY_FOR_OPERATION,
        ParkingState.AI_STOP,
        ParkingState.IDLE,
        ParkingState.VEHICLE_DETECTED,
    ]:
        sm.transition(state)
    assert sm.current_state == ParkingState.VEHICLE_DETECTED


def test_every_active_state_can_abort_to_idle():
    for origin in ParkingState:
        if origin is ParkingState.IDLE:
            continue
        sm = SafetyStateMachine()
        sm.current_state = origin
        sm.transition(ParkingState.IDLE)
        assert sm.current_state == ParkingState.IDLE


def test_idle_person_watch_edge():
    sm = SafetyStateMachine()
    sm.transition(ParkingState.HUMAN_DETECTED)
    sm.transition(ParkingState.IDLE)
    assert sm.current_state == ParkingState.IDLE


def test_ready_regresses_to_safety_check_on_person():
    sm = SafetyStateMachine()
    sm.current_state = ParkingState.READY_FOR_OPERATION
    sm.transition(ParkingState.SAFETY_CHECK)
    sm.transition(ParkingState.HUMAN_DETECTED)
    assert sm.current_state == ParkingState.HUMAN_DETECTED


def test_ai_stop_cannot_skip_into_active_flow():
    sm = SafetyStateMachine()
    sm.current_state = ParkingState.AI_STOP
    with pytest.raises(ValueError):
        sm.transition(ParkingState.VEHICLE_DETECTED)
