# CLAUDE.md

TowerSightAI is a parking-machine AI safety monitoring system for Ubuntu edge devices with four Tapo-C310 RTSP cameras and a Hailo-8 M.2 accelerator.

## Read First

Before implementing or changing behavior, read:

- `AGENTS.md`
- `docs/주차기_AI_안전감시_시스템_설계안.md`
- `docs/implementation/system-architecture.md`
- `docs/implementation/camera-and-config.md`
- `docs/implementation/hailo-gstreamer.md`
- `docs/implementation/ai-stages.md`
- `docs/implementation/ui-and-calibration.md`
- `docs/implementation/testing-strategy.md`
- `docs/implementation/implementation-roadmap.md`

Use `refers/` only as hardware-tested reference code. Do not copy hardcoded RTSP URLs, usernames, passwords, host paths, or experimental bugs into product code.

## Non-Negotiable Safety Rules

- If AI confidence is low, camera input is missing, calibration is invalid, PLC state is unknown, or a person/obstacle may exist, report NG and do not send final OK.
- Final OK requires all required checks to pass: valid parked position, plate handled, no person in the parking machine, no in-vehicle occupant, no dangerous obstacle, active camera streams, and CAR-IN still in the correct pre-operation state.
- CAR-IN closed or parking-machine operation started means AI analysis stops or returns to standby.
- Never hide uncertainty behind UI success messages. Driver-facing messages and PLC signals must reflect the conservative safety state.

## Implementation Direction

- Target runtime is Ubuntu with GStreamer, HailoRT/TAPPAS, and Hailo-8 over PCIe.
- Treat `.env` and deployment config as the source of truth for camera URL, camera role, camera credentials, TAPPAS paths, model paths, thresholds, PLC endpoint, UI mode, and calibration file paths.
- Build around the state flow from the design document: `IDLE`, `VEHICLE_DETECTED`, `PLATE_RECOGNITION`, `VEHICLE_ENTERING`, `ALIGNMENT_GUIDE`, `PARKED`, `SAFETY_CHECK`, `HUMAN_DETECTED`, `READY_FOR_OPERATION`, `AI_STOP`.
- Keep modules separated: config, camera ingest, Hailo inference, detection normalization, decision/state machine, PLC adapter, UI, calibration, tests.
- Use adapter interfaces for PLC and hardware-specific services so unit tests can run without a live PLC, RTSP cameras, or Hailo device.

## Hailo and GStreamer

- Base single-stream inference on `refers/detection.py` and `refers/callback_template.py`.
- Base multi-stream inference on official Hailo TAPPAS multi-stream detection patterns and `refers/multi_stream_detection_rtsp.sh`.
- Preferred multi-camera shape: `rtspsrc -> rtph264depay -> decode/scale/convert -> hailoroundrobin -> hailonet -> hailofilter -> hailopython/callback -> hailostreamrouter -> per-camera handling/compositor/UI`.
- Keep `TAPPAS_WORKSPACE`, HEF path, postprocess `.so`, thresholds, batch size, and sink choices configurable.
- Use `qos=false` for Hailo Python/postprocess elements unless there is a measured reason to change it.

## UI and Calibration

- The driver UI is for a 55-inch or larger display inside the parking-machine entry area.
- UI must change by state: front camera before entry, front plus bird's-eye during alignment, and safety status after parking.
- Provide on-site settings for camera status, RTSP configuration references, thresholds, PLC connection, and calibration.
- Calibration must persist per-camera guide geometry, ROI zones, lane centerline, stop zone, tolerance values, and version metadata.

## Testing Expectations

- Every implementation stage must include tests for normal, NG, and uncertain cases.
- Add unit tests for config parsing, state transitions, decision logic, calibration persistence, and pipeline string construction.
- Add integration tests with fake camera frames, fake Hailo detections, and fake PLC adapters.
- Hardware smoke tests should be explicit and skippable when RTSP cameras or Hailo-8 are unavailable.
