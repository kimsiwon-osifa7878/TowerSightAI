# TowerSightAI UI-First Implementation Plan

This file captures the next implementation work from the current prototype state. The near-term development mode is UI-first: add the operator UI control, status, and test slot first; connect it to fake or empty behavior safely; then wire in real camera, Hailo, AI-stage, and PLC logic.

The safety rule is unchanged. UI tests, simulations, and EMPTY buttons never authorize PLC OK.

## Process Engine (2026-09 field redefinition — implemented)

The continuous parking-process engine (`towersightai/process/`) now runs the redefined field flow:
IDLE person watch (birdview+front+left, right excluded) → opposite_side vehicle trigger
(operator-tunable confidence/streak) → 1 Hz front LPR with plate-zone line + majority vote →
front wheel-guide alignment → parked instruct → 10 s clear countdown → simulated PLC OK+plate →
60 s machine-operation window (person watch adds right camera) → back to IDLE. Outbound (출고)
was explicitly removed by the owner and stays a future concept. Operator-tunable values live in
`data/operator-settings.json` via the `감시 설정` page, not `.env`.

Follow-ups queued from that work:

- 3D vehicle box (직육면체) estimation — **design confirmed 2026-09-09** (INTENT.md §4):
  cameras 3/4 (front+left, rear+right diagonals; role mapping in `.env`) as the primary pair,
  plus the front camera (camera 2) as an optional third view when it streams: it observes the
  front face head-on, so it pins the vehicle width, lateral offset and yaw from the front-face
  centre line and the bumper bottom height, reusing the existing preview frames (no extra RTSP
  session). The box must be computable from 3/4 alone; the front view only tightens width /
  offset and cross-checks; disagreement between views widens the uncertainty (conservative).
  OpenCV silhouette (Hailo bbox is ROI only), tyre-contact → floor plane → side planes → box,
  multi-camera cross-check, Kalman smoothing, ±5 cm target incl. moving, origin = pallet
  centre. Drawing defaults live in `VehicleEnvelopeConfig` (`VEHICLE_BOX_*`). Field photos and
  videos for offline work go to `data/field-media/{front,rear_side,opposite_side}/`
  (per-camera folders, no filename prefix, gitignored).

  **Stage 1 first pass ran 2026-09-16 in `vehicle_box_test/`** — a lab folder deliberately kept
  out of `towersightai/` (owner's rule: verify the hypothesis there, only then update the app).
  See `vehicle_box_test/README.md` (how to run) and `vehicle_box_test/CONTEXT.md` (intent,
  findings). What it does: pulls the vehicle-bearing media out of the NAS archive by reading the
  event shards, extracts clip frames, builds per-camera median backgrounds, fits the ground
  homography from the turntable disc (Ø6100) + rails (2106) + pallet (5350×2200) with **no
  checkerboard**, extracts the vehicle silhouette by Lab-space background subtraction (no Hailo),
  lifts the tyre-contact line to world mm, and writes the verdict **into the image** — dimensions
  when it works, a Korean failure reason when it does not — plus `out/report.html`.
  Result: 18 of 173 measurable images judged; ground rectangle + front/rear ends + wheel lines
  only, **no 3D cuboid yet**. Blockers found, each with a known cause: the diagonal camera's pose
  recovery disagrees with the ground by 16 % (fisheye — this is the number that proves the
  checkerboard intrinsics are actually required); the front camera only sees ground over the far
  ~1.5 m so it cannot see a parked car's contact line; evidence clips (stream2) have a different
  FOV from snapshots (stream1) so the calibration does not transfer; one diagonal camera alone
  cannot measure width; `rear_side` is still uncalibrated.
  Next: calibrate `rear_side` → measure C310 intrinsics on the existing 카메라 캘리브레이션 page →
  undistort → pose → cuboid + height → report accuracy against the ±5 cm target.
  Stage 2: `vehicle_box` stage module returning PASS/WAIT/RETRY/NG/ERROR, operator page with
  overlay, size-limit check. Stage 3: replace the bbox-stability parked heuristic and drive
  directional alignment guidance. Calibration UI gates everything (unreviewed → final OK blocked).
- Polygon exclusion zone for the opposite_side trigger camera (door-open street traffic).
- Outbound (출고) flow once a PLC exit signal contract exists.
- Bundling `artifacts/runtime/purpose-ai/` task logs into the NAS day directory.
- Replace the parked/alignment heuristics with calibrated geometry once calibration lands.

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
