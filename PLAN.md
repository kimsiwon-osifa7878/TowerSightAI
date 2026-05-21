# TowerSightAI UI-First Implementation Plan

This file captures the next implementation work from the current prototype state. The near-term development mode is UI-first: add the operator UI control, status, and test slot first; connect it to fake or empty behavior safely; then wire in real camera, Hailo, AI-stage, and PLC logic.

The safety rule is unchanged. UI tests, simulations, and EMPTY buttons never authorize PLC OK.

## Immediate Priority

1. Stabilize the operator UI shell.

   Current state:
   - The app starts on the operator dashboard.
   - The dashboard prioritizes the ceiling birdview and front camera.
   - Ceiling birdview is displayed as a vertical tile and rotated CCW 90 degrees.
   - A collapsible sidebar owns navigation and feature slots.
   - Unimplemented feature slots are labeled `EMPTY`.

   Next work:
   - Keep the dashboard layout stable across fullscreen and windowed modes.
   - Add UI-only checks for sidebar open/close, dashboard/all-camera switching, and EMPTY actions.
   - Ensure every empty or simulation action leaves final OK blocked.
   - Keep camera tiles from resizing when long status text appears.

2. Build the in-UI test hub.

   Current state:
   - The test screen can run settings, Hailo installation, sample image, per-camera frame, PLC simulator, and full hardware smoke diagnostics.
   - Diagnostic results are recorded with `safe_to_operate=False`.

   Next work:
   - Add UI-only tests for layout, sidebar controls, and simulation buttons.
   - Add fake-data tests for camera health, detection events, and PLC events.
   - Add a live multistream diagnostic that runs for a short duration and reports silent streams.
   - Store sanitized summaries in `artifacts/diagnostics/`.

3. Improve camera and AI visualization.

   Current state:
   - Live AI Detection uses a single Hailo multistream GStreamer process.
   - Active streams are selected from cameras whose runtime status is `정상 수신`.
   - Detection boxes are drawn as fresh overlays and expire after a short TTL.

   Next work:
   - Show preview health and inference health separately per camera.
   - Track last detection timestamp per camera.
   - Display `AI stale` or `AI no events` when events stop.
   - Preserve and display the last GStreamer stderr tail in the UI test/diagnostic log.
   - Replace the fixed two-attempt retry loop with a supervised watchdog.

## UI-First Feature Buildout

4. Calibration workflow.

   Next work:
   - Add a sidebar entry for calibration when the UI shape is ready.
   - Support camera selection, normalized geometry editing, save/revert, and validation.
   - Start with UI-only/fake persistence tests before connecting production calibration.
   - Block final OK when calibration is missing, invalid, or unreviewed.

5. Stage simulation and fake event playback.

   Next work:
   - Add UI controls that inject fake vehicle, alignment, person, obstacle, and occupancy states.
   - Clearly mark all fake states as test-only.
   - Use these fake states to verify operator instructions and NG/WAIT/READY styling.
   - Keep PLC OK blocked unless the real safety gate later approves all prerequisites.

6. AI stage logic behind UI-observable outputs.

   Next work:
   - Add vehicle entry and alignment decisions.
   - Add person/obstacle fusion across healthy cameras.
   - Add plate and in-vehicle occupancy interfaces before final model selection.
   - Expose every stage as a UI-visible result: `PASS`, `WAIT`, `RETRY`, `NG`, or `ERROR`.

## PLC And Safety Gate

7. Conservative final OK.

   Next work:
   - Centralize final OK prerequisites in one safety gate.
   - Require healthy cameras, healthy inference, valid calibration, known PLC state, and completed stage decisions.
   - Add tests for every success, failure, and uncertainty path.

8. Real PLC adapter.

   Next work:
   - Confirm real PLC protocol and event schema.
   - Implement a real adapter behind the existing mockable boundary.
   - Add simulator or fake-server integration tests.
   - Assert event ordering for NG, human detected, human clear, parked, and final OK.

## Field Hardening

9. Ubuntu target deployment.

   Next work:
   - Add service/runbook notes for HailoRT, TAPPAS venv, GStreamer plugins, desktop session, and network setup.
   - Define log locations and rotation.
   - Add a site acceptance checklist that starts from the operator UI test hub.

10. Observability.

    Next work:
    - Add structured logs for camera status, Hailo process lifecycle, detection counts, UI test actions, state transitions, and PLC events.
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
