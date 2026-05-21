# Testing Strategy Guide

Every implementation stage needs tests. Safety logic must be testable without live cameras, Hailo-8, or PLC hardware. From the current prototype forward, the operator UI test screen is also a field-verification hub.

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
- Operator dashboard, sidebar, EMPTY action, and simulation behavior.

## UI-Only Tests

Run without real hardware:

- App starts on the operator dashboard.
- Sidebar opens and closes.
- Dashboard and all-camera layouts switch correctly.
- Ceiling birdview uses the vertical dashboard tile and CCW 90-degree display policy.
- EMPTY buttons do not change safety state.
- Vehicle-entry simulation is visually testable but keeps final OK blocked.
- Long status text does not resize camera tiles unexpectedly.

## Fake-Data Integration Tests

Run without real hardware by using fakes:

- Fake camera source emits frames or timestamps.
- Fake Hailo callback emits detections.
- Fake PLC adapter records events.
- State machine drives UI state from simulated detections.
- Calibration fixtures drive alignment decisions.
- Fake AI-stage outputs drive UI PASS/WAIT/RETRY/NG/ERROR states.

Fake-data tests may update the UI, but they must not imply real safety approval.

## In-UI Diagnostic Tests

Run from the operator test screen:

- Settings validation.
- Hailo installation check.
- Hailo sample image inference.
- Per-camera frame receive checks.
- PLC simulator interface check.
- UI-only control checks.
- Fake event playback checks.
- Full hardware smoke sequence when explicitly selected.

Diagnostic results must default to `safe_to_operate=False`. A passing diagnostic means the test passed, not that PLC OK is authorized.

## Hardware Smoke Tests

Run only on the Ubuntu target with explicit opt-in:

- Validate GStreamer can open each Tapo-C310 RTSP stream.
- Validate Hailo device and plugins are available.
- Run single-stream detection from `front` camera.
- Run multi-stream Hailo detection for connected cameras.
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
- Simulated/fake input path remains blocked from final OK.

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

- Configured RTSP sources are included.
- Credentials are not logged in clear text.
- `TAPPAS_WORKSPACE`, HEF path, and postprocess path are configurable.
- Hailo elements are present in the expected order.
- Router outputs map back to camera IDs.
- Preview-only paths can run without Hailo elements.

## UI Tests

UI tests should assert:

- Correct operator dashboard startup.
- Correct camera layout for dashboard and all-camera modes.
- Sidebar and EMPTY actions are safe.
- Operator guidance messages match alignment result.
- Safety screen displays human, obstacle, and in-vehicle status.
- Settings pages redact secrets.
- Calibration cannot save structurally invalid geometry.
- Error/NG states cannot display final OK styling.

## Test Data

Keep fixtures sanitized:

- No real camera passwords.
- No real PLC secrets.
- Synthetic or anonymized frames only.
- Small fixtures suitable for source control.

## CI Policy

Default CI should run unit, UI-only, and non-hardware integration tests. Hardware smoke tests should require an environment flag such as `RUN_HARDWARE_TESTS=1`.
