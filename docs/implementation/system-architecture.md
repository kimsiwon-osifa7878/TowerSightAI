# System Architecture Guide

This guide defines the stable implementation shape for TowerSightAI. The source behavior is `docs/주차기_AI_안전감시_시스템_설계안.md`.

## Runtime Target

- Primary target: Ubuntu edge computer in the parking-machine site.
- Cameras: four Tapo-C310 RTSP streams over LAN or WiFi.
- Accelerator: Hailo-8 M.2, using HailoRT/TAPPAS GStreamer elements.
- Display: 55-inch or larger operator HMI display that can also present safety guidance when needed.
- External control: PLC integration through a replaceable adapter.

## Core Modules

- `config`: loads `.env` and typed settings, validates required camera and Hailo fields.
- `camera`: builds RTSP/GStreamer sources, monitors stream health, exposes frames or stream endpoints.
- `inference`: runs Hailo pipelines and converts raw Hailo metadata into normalized detection events.
- `ai_stages`: implements vehicle, plate, alignment, person, obstacle, and cabin-occupancy decisions.
- `state_machine`: owns parking lifecycle state and conservative OK/NG gating.
- `plc`: sends vehicle, human, safety, and error events through a mockable adapter.
- `ui`: renders state-aware camera views, guidance, safety status, settings, and calibration.
- `calibration`: stores per-camera guide geometry, ROIs, stop zones, and tolerances.
- `sensors`: receives raw LD2410 binary frames from one LAN ESP32 client, parses them off the UI thread, and exposes timestamp-aligned audit snapshots without entering the safety decision path.
- `storage`: appends versioned hourly JSONL audit shards, captures event-scoped snapshots/H.264 MKV evidence outside the UI thread, synchronizes a file-level SHA-256 manifest to Synology SFTP, and applies verified-upload retention.

## State Flow

The product state machine must follow the design document unless the design is explicitly revised:

```text
IDLE
VEHICLE_DETECTED
PLATE_RECOGNITION
VEHICLE_ENTERING
ALIGNMENT_GUIDE
PARKED
SAFETY_CHECK
HUMAN_DETECTED
READY_FOR_OPERATION
AI_STOP
```

Allowed implementation may use sub-states, but public PLC/UI states must map back to these names.

## Safety Gate

The final OK signal is allowed only when all conditions are true:

- Vehicle is inside calibrated parking bounds.
- Vehicle is stopped and aligned within configured tolerance.
- Plate has been recognized or handled by the approved fallback path.
- PLC has received the parked/plate event.
- No person is detected in the parking machine.
- No possible occupant is detected inside the vehicle.
- No dangerous obstacle is detected.
- Camera streams and calibration are healthy.
- CAR-IN state still allows AI confirmation.

Any unknown, stale, or low-confidence input blocks OK and emits NG or a waiting state.

## PLC Event Boundary

Initial implementation should define a PLC adapter interface before binding a real protocol. Required logical events:

- `vehicle_parked`
- `human_detected`
- `human_clear`
- `in_vehicle_occupancy_check`
- `safety_check_complete`
- `safety_status_ng`
- `ai_stopped`

Tests must use a fake PLC adapter and assert exact event ordering for OK and NG flows.

## Failure Handling

- Camera loss: mark stream unhealthy, show UI error, send or hold NG, never OK.
- Invalid calibration: block alignment OK and safety OK.
- Hailo pipeline error: stop affected inference path, surface error, block OK.
- Low confidence: retry when appropriate, otherwise remain NG.
- PLC communication failure: keep UI in error/NG and retry through the adapter policy.

## Implementation Notes

- Keep hardware services behind interfaces so local tests can run on non-Ubuntu development machines.
- Log every safety-relevant decision with state, camera ID, confidence, and reason.
- Persist only sanitized operational state. Do not persist credentials outside the configured secret mechanism.
- NAS archive availability is operational telemetry only and must never relax or authorize the PLC safety gate.
- LD2410/ESP32 availability and values are raw audit context only. Missing, malformed, or stale sensor data must be recorded explicitly and must not alter AI, state-machine, or PLC decisions.
