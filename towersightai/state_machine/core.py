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


ALLOWED = {
    ParkingState.IDLE: {ParkingState.VEHICLE_DETECTED},
    ParkingState.VEHICLE_DETECTED: {ParkingState.PLATE_RECOGNITION},
    ParkingState.PLATE_RECOGNITION: {ParkingState.VEHICLE_ENTERING},
    ParkingState.VEHICLE_ENTERING: {ParkingState.ALIGNMENT_GUIDE},
    ParkingState.ALIGNMENT_GUIDE: {ParkingState.PARKED},
    ParkingState.PARKED: {ParkingState.SAFETY_CHECK},
    ParkingState.SAFETY_CHECK: {ParkingState.HUMAN_DETECTED, ParkingState.READY_FOR_OPERATION},
    ParkingState.HUMAN_DETECTED: {ParkingState.SAFETY_CHECK},
    ParkingState.READY_FOR_OPERATION: {ParkingState.AI_STOP},
    ParkingState.AI_STOP: set(),
}


class SafetyStateMachine:
    def __init__(self) -> None:
        self.current_state = ParkingState.IDLE

    def transition(self, new_state: ParkingState) -> None:
        if new_state not in ALLOWED[self.current_state]:
            raise ValueError(f"Illegal transition: {self.current_state} -> {new_state}")
        self.current_state = new_state
