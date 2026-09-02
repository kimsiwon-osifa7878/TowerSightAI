from __future__ import annotations

from enum import Enum


class ParkingState(str, Enum):
    IDLE = "IDLE"
    VEHICLE_DETECTED = "VEHICLE_DETECTED"
    PLATE_RECOGNITION = "PLATE_RECOGNITION"
    VEHICLE_ENTERING = "VEHICLE_ENTERING"
    ALIGNMENT_GUIDE = "ALIGNMENT_GUIDE"
    PARKED = "PARKED"
    SAFETY_CHECK = "SAFETY_CHECK"
    HUMAN_DETECTED = "HUMAN_DETECTED"
    READY_FOR_OPERATION = "READY_FOR_OPERATION"
    AI_STOP = "AI_STOP"


# Every non-IDLE state keeps a conservative abort edge back to IDLE: uncertainty
# (dead stream, dead inference, lost trigger) must always have a legal retreat.
# IDLE -> HUMAN_DETECTED covers the idle person watch; AI_STOP -> IDLE closes the
# continuous cycle (machine operation finished); READY_FOR_OPERATION -> SAFETY_CHECK
# covers a person reappearing between OK-send and machine start.
ALLOWED = {
    ParkingState.IDLE: {ParkingState.VEHICLE_DETECTED, ParkingState.HUMAN_DETECTED},
    ParkingState.VEHICLE_DETECTED: {ParkingState.PLATE_RECOGNITION, ParkingState.IDLE},
    ParkingState.PLATE_RECOGNITION: {ParkingState.VEHICLE_ENTERING, ParkingState.IDLE},
    ParkingState.VEHICLE_ENTERING: {ParkingState.ALIGNMENT_GUIDE, ParkingState.IDLE},
    ParkingState.ALIGNMENT_GUIDE: {ParkingState.PARKED, ParkingState.IDLE},
    ParkingState.PARKED: {ParkingState.SAFETY_CHECK, ParkingState.IDLE},
    ParkingState.SAFETY_CHECK: {
        ParkingState.HUMAN_DETECTED,
        ParkingState.READY_FOR_OPERATION,
        ParkingState.IDLE,
    },
    ParkingState.HUMAN_DETECTED: {ParkingState.SAFETY_CHECK, ParkingState.IDLE},
    ParkingState.READY_FOR_OPERATION: {
        ParkingState.AI_STOP,
        ParkingState.SAFETY_CHECK,
        ParkingState.IDLE,
    },
    ParkingState.AI_STOP: {ParkingState.IDLE},
}


class SafetyStateMachine:
    def __init__(self) -> None:
        self.current_state = ParkingState.IDLE

    def transition(self, new_state: ParkingState) -> None:
        if new_state not in ALLOWED[self.current_state]:
            raise ValueError(f"Illegal transition: {self.current_state} -> {new_state}")
        self.current_state = new_state
