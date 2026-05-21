# UI and Calibration Guide

The UI is an operator-centered HMI for validating camera, AI, calibration, and PLC readiness from one console. It must still behave as a safety display: unknown, stale, missing, simulated, or low-confidence inputs never look safe and never authorize PLC OK.

## UI-First Development Rule

New features should be added in this order:

1. Add the operator UI button, status row, panel, or test slot.
2. Connect it to `EMPTY`, fake, or simulation behavior that cannot change final OK.
3. Add UI and fake-data tests.
4. Connect real camera, Hailo, calibration, AI-stage, or PLC logic.
5. Keep the UI result auditable through the test screen or diagnostics log.

## Operator Dashboard

The app starts on the operator dashboard. There is no separate driver screen in the current UI direction.

Default layout:

- Ceiling birdview and front camera are the primary first-screen views.
- Ceiling birdview is shown as a vertical tile and the frame is displayed CCW 90 degrees.
- The birdview tile must not draw default lane/stop guide lines unless calibration mode or a specific overlay mode is active.
- The front view stays wide and is used for vehicle entry context.
- Safety status, state, PLC status, camera health, AI inference state, and clock remain visible.
- Camera loss, stale frames, Hailo errors, and PLC unknown state must be visible as NG or blocked states.

The all-camera layout remains available from the sidebar for inspection of ceiling, front, rear-side, and opposite-side cameras.

## Sidebar and Feature Slots

The sidebar is a collapsible operator control surface.

Current connected entries:

- `운영 대시보드`
- `전체 카메라`
- `이전 AI Detection`
- `차량 전용 검출`
- `번호판 이미지 LPR`
- `사람 존재 감지`
- `차량 진입 시뮬레이션`

`이전 AI Detection` is a regression-isolation control. It should bypass runtime model selection and launch the previous multistream detection path that uses `HAILO_HEF_PATH`, the shared detection event directory, and the same camera rotation map as the visible UI.

Purpose-specific AI controls should use fixed, known-compatible TAPPAS example model sets:

- `차량 전용 검출`: front camera only, Hailo LPR example `yolov5m_vehicles`.
- `번호판 이미지 LPR`: image-set test using `tmp/car_number-test` and the Hailo LPR example vehicle, plate, and OCR models.
- `사람 존재 감지`: currently streaming cameras using the TAPPAS person detector path. It must infer only whether a person exists; do not run Re-ID embedding, gallery matching, or same-person tracking.

These controls are for integration diagnosis and staged feature development. They must show running/error/log status in the operator status strip and keep final OK blocked.

Unimplemented feature slots must be labeled `EMPTY`. Pressing an `EMPTY` button should only show a message such as “not connected yet” and must not alter safety state, PLC state, calibration state, or final OK.

## UI Test and Simulation Behavior

UI test and simulation actions are for implementation verification only.

- Vehicle-entry simulation may draw test overlays and update instruction text.
- Fake camera, fake detection, fake PLC, and fake AI-stage actions must be visually marked or described as test-only.
- Test actions must not send real PLC events.
- Test actions must not make `can_show_final_ok` true.
- Diagnostic results default to `safe_to_operate=False`.

## State-Oriented Display Behavior

The UI should expose the design-document states but does not need a separate screen for each state.

- Before entry: show front and birdview context with waiting/detection status.
- During entry and alignment: show front and ceiling birdview together with one clear instruction.
- After parking: show safety checks, person/obstacle status, in-vehicle occupancy status, and final OK/NG/WAIT.
- Error and stop states: show the blocking subsystem and reason.

Never present a blocked state as safe.

## Settings UI

On-site settings should eventually support:

- Camera status and redacted RTSP source display.
- Camera role assignment.
- Hailo/TAPPAS path status.
- PLC connection status.
- Confidence thresholds.
- Calibration file load/save.
- Fullscreen display setting.

Settings must not reveal camera passwords.

## Calibration UI

Calibration is required for accurate alignment and safety zones. It should be reached from the sidebar after the UI shell is stable.

Provide per-camera calibration workflows:

- Select camera.
- Draw or adjust lane centerline only in calibration mode.
- Draw side boundaries.
- Draw stop zone.
- Draw danger/safety ROIs.
- Draw vehicle cabin/window ROIs for front and side cameras.
- Save calibration with timestamp, site ID, camera ID, and version.
- Review and activate calibration explicitly.

Invalid, missing, unreviewed, or stale calibration blocks final OK.

## Calibration Data Shape

Use a structured JSON file or equivalent typed format:

```json
{
  "site_id": "site-placeholder",
  "version": 1,
  "updated_at": "2026-04-29T11:30:00+09:00",
  "cameras": {
    "ceiling": {
      "lane_centerline": [[0.5, 0.1], [0.5, 0.9]],
      "left_boundary": [[0.2, 0.1], [0.2, 0.9]],
      "right_boundary": [[0.8, 0.1], [0.8, 0.9]],
      "stop_zone": [[0.3, 0.7], [0.7, 0.7], [0.7, 0.9], [0.3, 0.9]],
      "tolerances": {"lateral": 0.05, "forward": 0.05}
    }
  }
}
```

Coordinates should be normalized unless a module has a documented reason to use pixels.

## UI Testing

- Tests for dashboard startup, sidebar open/close, and all-camera switching.
- Tests that `EMPTY` buttons are safe no-ops.
- Tests that vehicle-entry simulation remains test-only and blocks final OK.
- Tests that purpose-specific AI buttons create the expected fixed-model pipeline and do not use arbitrary HEF selection.
- Tests for camera tile layout, birdview rotation policy, and stale/NG display.
- Tests for Korean operator instruction selection.
- Tests that password values are redacted.
- Tests for calibration save/load validation.
- Tests that error/NG states cannot display final OK styling.
