# TowerSightAI Implementation Plan

This file captures the next implementation work from the current prototype state.

## Immediate Priority

1. Harden live AI Detection runtime.

   Current state:
   - AI Detection now uses a single Hailo multistream GStreamer process.
   - Active streams are selected from cameras whose runtime status is `정상 수신`.
   - Hardware verification confirmed simultaneous `front` and `ceiling` events.
   - If `gst-launch` exits, the UI worker retries only twice.

   Next work:
   - Replace the fixed two-attempt retry loop with a supervised watchdog.
   - Show per-camera inference health separately from preview health.
   - Preserve and display the last GStreamer stderr tail in the test/diagnostic UI.
   - Mark AI Detection as degraded or stopped if events become stale.
   - Keep final OK blocked on stale inference events.

2. Add live multistream diagnostics.

   Next work:
   - Add a diagnostic test that starts live multistream detection for connected cameras for a short duration.
   - Assert that every selected camera emits at least one event, or report which stream is silent.
   - Store a sanitized summary in `artifacts/diagnostics/`.
   - Keep the test manual/hardware-only and never allow it to imply PLC OK.

3. Improve overlay lifecycle.

   Current state:
   - Overlay TTL is `1.0` second.
   - Boxes disappear quickly if event delivery pauses.

   Next work:
   - Track last detection timestamp per camera.
   - Display `AI stale` or `AI no events` when a stream is alive but inference events stop.
   - Consider a configurable TTL via `.env`, but keep conservative NG behavior for stale data.

## AI Stage Work

4. Vehicle entry and alignment.

   Next work:
   - Define calibration data loader for lane centerline, side bounds, and stop zone.
   - Convert ceiling/front detections into vehicle position estimates.
   - Add alignment decision outputs: left/right/forward/back/parked/unknown.
   - Add unit tests for success, failure, and uncertainty.

5. Person and obstacle safety.

   Next work:
   - Define label policy for person, vehicle, obstacle, and ignored classes.
   - Fuse detections across all healthy cameras.
   - Treat missing side cameras as NG or degraded according to the final safety policy.
   - Add tests that prove possible person/obstacle presence blocks OK.

6. Plate and in-vehicle occupancy.

   Next work:
   - Add interfaces before choosing final OCR/occupancy implementation.
   - Keep plate fallback policy explicit.
   - Add tests for recognized, unrecognized, low-confidence, and unavailable states.

## UI And Calibration

7. Calibration UI.

   Next work:
   - Add operator calibration mode.
   - Support draggable lane, stop-zone, and ROI geometry.
   - Save normalized coordinates with timestamp, site ID, camera ID, and version.
   - Validate calibration before activation.
   - Block final OK when calibration is missing or invalid.

8. Operator UX hardening.

   Next work:
   - Add explicit AI Detection health rows per stream.
   - Add a log/details drawer for recent Hailo and camera errors.
   - Keep right panel fixed width.
   - Keep all long text wrapped without resizing camera tiles.

## PLC And Safety Gate

9. PLC adapter.

   Next work:
   - Confirm real PLC protocol and event schema.
   - Implement a real adapter behind the existing mockable boundary.
   - Add integration tests with a simulator or fake server.
   - Assert event ordering for NG, human detected, human clear, parked, and final OK.

10. Conservative final OK.

    Next work:
    - Centralize final OK prerequisites in one safety gate.
    - Require healthy cameras, healthy inference, valid calibration, known PLC state, and completed stage decisions.
    - Add tests for every success, failure, and uncertainty path.

## Deployment And Field Hardening

11. Ubuntu target deployment.

    Next work:
    - Add service/runbook notes for HailoRT, TAPPAS venv, GStreamer plugins, desktop session, and network setup.
    - Define log locations and rotation.
    - Add a site acceptance checklist.

12. Observability.

    Next work:
    - Add structured logs for camera status, Hailo process lifecycle, detection counts, state transitions, and PLC events.
    - Redact all credentials in logs.
    - Keep safety-relevant decisions auditable.

## Current Known Commands

```bash
pytest -q
towersightai-operator-ui --env .env --windowed
towersightai-check-settings --env .env --check-hailo
RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke --env .env --image data/samples/test-car.png --check-installation --run
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```
