# AI Stages Guide

This guide defines how each AI function should be implemented and tested. The design source is `docs/주차기_AI_안전감시_시스템_설계안.md`.

## Shared Decision Rules

- Every detector returns label, confidence, camera ID, timestamp, and optional geometry.
- Every stage exposes a clear result: `PASS`, `WAIT`, `RETRY`, `NG`, or `ERROR`.
- Stale frames, missing detections, invalid calibration, and low confidence must not produce `PASS`.
- Stage logic should be deterministic and unit-testable from synthetic events.

## 1. Vehicle Detection

Purpose:

- Detect an approaching vehicle from the front camera.
- Track vehicle presence during entry.
- Confirm the vehicle is the only expected large object after parking.

Inputs:

- Front camera detections.
- Optional ceiling camera detections for position.
- Camera health and timestamp freshness.

Tests:

- Vehicle appears from front camera.
- No vehicle remains `IDLE`.
- Low-confidence vehicle does not advance.
- Camera loss becomes NG/error.

## 2. Plate Recognition

Purpose:

- Detect license plate region.
- Run OCR.
- Store recognized plate and confidence for PLC handoff.

Implementation notes:

- Keep plate detection and OCR behind an interface.
- Retry when the plate is visible but confidence is below threshold.
- Provide a manual/admin fallback only if the product flow explicitly enables it.
- Do not send final OK if plate handling is incomplete.

Tests:

- Valid plate recognized and stored.
- OCR confidence too low triggers retry or NG.
- Empty/invalid plate blocks final OK.
- Manual fallback path records operator action when enabled.

## 3. Alignment and Parking Position

Purpose:

- Guide the driver into the calibrated lane and stop zone.
- Decide whether the vehicle is parked within tolerance.

Inputs:

- Ceiling camera bird's-eye detections.
- Front camera entry direction.
- Calibration: lane centerline, left/right bounds, stop zone, fore/aft tolerance.

Outputs:

- `move_right`
- `move_left`
- `move_forward`
- `move_backward`
- `parked_ok`
- `alignment_ng`

Tests:

- Vehicle left of lane prompts right movement.
- Vehicle right of lane prompts left movement.
- Vehicle before stop zone prompts forward movement.
- Vehicle beyond stop zone prompts backward movement.
- Invalid calibration blocks `parked_ok`.

## 4. Parking Complete

Purpose:

- Confirm the vehicle is aligned, stopped, and inside the stop zone.
- Trigger PLC vehicle/plate event before safety detection.

Required conditions:

- Alignment result is `parked_ok`.
- Vehicle movement is below configured threshold for the configured time window.
- Plate data is available or approved fallback is complete.

Tests:

- Stationary aligned vehicle reaches `PARKED`.
- Moving vehicle remains in guidance state.
- PLC event failure blocks progress to final OK.

## 5. People and Obstacle Detection

Purpose:

- Detect people and dangerous obstacles in the parking machine before operation.
- Use all four cameras to reduce blind spots.

Implementation notes:

- A single credible person detection is enough to block OK.
- The current UI-connected implementation is `사람 존재 감지`, a detector-only Hailo purpose task over currently streaming cameras.
- Do not use Multi-Camera Re-ID for safety gating. The system needs person existence, not identity continuity.
- Use configured ROIs to ignore irrelevant outside areas when appropriate.
- Continue sending or holding `human_present` while a person remains possible.

Tests:

- Person in any camera produces `HUMAN_DETECTED`.
- Obstacle in danger zone produces NG.
- Person outside calibrated safety zone follows configured policy.
- All clear across fresh streams can progress only after debounce.

## 6. In-Vehicle Occupancy

Purpose:

- Detect possible remaining occupants through front and side views.
- Prevent operation when a driver, child, passenger, or pet may remain inside.

Implementation notes:

- Treat this as a conservative classifier or detector over configured window/cabin ROIs.
- If visibility is poor or confidence is ambiguous, return NG or WAIT.
- Keep the model behind an interface because the first implementation may use a placeholder or separate model.

Tests:

- Occupant detection blocks OK.
- Poor visibility blocks OK.
- Clear cabin with valid confidence can pass.
- Missing side/front stream blocks OK.

## Final Safety Decision

The final decision must compose stage outputs instead of redoing raw detection logic. `READY_FOR_OPERATION` is allowed only when all required stages are `PASS` and all inputs are fresh.
