# Testing Strategy Guide

Every implementation stage needs tests. Safety logic must be testable without live cameras, Hailo-8, or PLC hardware.

## Test Layers

## Unit Tests

Run on any developer machine:

- Config parsing and validation.
- RTSP URL redaction.
- Camera role mapping.
- GStreamer pipeline string builders.
- Hailo detection event normalization.
- State transitions.
- AI decision logic.
- Calibration geometry persistence and validation.
- UI state-to-message mapping.

## Integration Tests

Run without real hardware by using fakes:

- Fake camera source emits frames or timestamps.
- Fake Hailo callback emits detections.
- Fake PLC adapter records events.
- State machine drives UI state from simulated detections.
- Calibration fixtures drive alignment decisions.

## Hardware Smoke Tests

Run only on the Ubuntu target with explicit opt-in:

- Validate GStreamer can open each Tapo-C310 RTSP stream.
- Validate Hailo device and plugins are available.
- Run single-stream detection from `front` camera.
- Run four-stream Hailo pipeline with redacted logs.
- Verify UI fullscreen rendering on the target display.
- Verify PLC test endpoint or simulator communication.

Hardware tests must be skippable in CI and local non-hardware environments.

## Required Scenarios

Each stage that affects PLC OK must test:

- Happy path.
- Low confidence.
- Missing camera data.
- Invalid calibration when relevant.
- PLC failure when relevant.
- Recovery after temporary failure.

## State Machine Tests

Minimum transitions:

- `IDLE -> VEHICLE_DETECTED`
- `VEHICLE_DETECTED -> PLATE_RECOGNITION`
- `PLATE_RECOGNITION -> VEHICLE_ENTERING`
- `VEHICLE_ENTERING -> ALIGNMENT_GUIDE`
- `ALIGNMENT_GUIDE -> PARKED`
- `PARKED -> SAFETY_CHECK`
- `SAFETY_CHECK -> HUMAN_DETECTED`
- `SAFETY_CHECK -> READY_FOR_OPERATION`
- `READY_FOR_OPERATION -> AI_STOP`

Illegal transitions must be rejected or converted to NG/error according to policy.

## Pipeline Tests

Pipeline builder tests should assert:

- Four configured RTSP sources are included.
- Credentials are not logged in clear text.
- `TAPPAS_WORKSPACE`, HEF path, and postprocess path are configurable.
- Hailo elements are present in the expected order.
- Router outputs map back to camera IDs.
- Preview-only paths can run without Hailo elements.

## UI Tests

UI tests should assert:

- Correct camera layout for each state.
- Driver guidance messages match alignment result.
- Safety screen displays human and in-vehicle status.
- Settings pages redact secrets.
- Calibration cannot save structurally invalid geometry.

## Test Data

Keep fixtures sanitized:

- No real camera passwords.
- No real PLC secrets.
- Synthetic or anonymized frames only.
- Small fixtures suitable for source control.

## CI Policy

Default CI should run unit and non-hardware integration tests. Hardware smoke tests should require an environment flag such as `RUN_HARDWARE_TESTS=1`.
