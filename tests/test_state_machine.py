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
