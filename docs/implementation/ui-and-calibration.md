# UI and Calibration Guide

The UI contains separate driver-facing user mode and operator mode in one PyQt6 application. It must behave as a safety display: unknown, stale, missing, simulated, or low-confidence inputs never look safe and never authorize PLC OK.

## UI-First Development Rule

New features should be added in this order:

1. Add the operator UI button, status row, panel, or test slot.
2. Connect it to fake, diagnostic, or simulation behavior that cannot change final OK.
3. Add UI and fake-data tests.
4. Connect real camera, Hailo, calibration, AI-stage, or PLC logic.
5. Keep the UI result auditable through the test screen or diagnostics log.

## User Mode

The app starts in user mode on the installed parking-machine display.

- Use an edge-to-edge, near-black camera canvas sized for a 50-inch 16:9 display viewed from about 6 m.
- Put one short action in a top overlay with a 50%-transparent background.
- Keep branding, stage, and blocking status small in a bottom strip.
- Do not expose development state buttons, model details, paths, or inference counters.
- A visually hidden `72 x 72 px` top-right hotspot enters operator mode only after a continuous two-second mouse or touch hold.
- A visible `운영자 모드` button in the bottom-right corner enters operator mode in one press. It exists because holding the hidden hotspot is impractical at the installed machine. It only switches surfaces: it must never expose diagnostics, change safety state, or emit PLC events, and it must stay out of the operator-mode driver preview.
- User-visible camera priority is front only while idle, then front plus ceiling birdview after vehicle detection and throughout entry, alignment, and safety guidance.
- Rear-side and opposite-side cameras continue running for person/obstacle safety checks but are shown only in the operator `전체 카메라` inspection view. Hidden safety cameras remain required inputs and can still block the user display and final OK.
- Front-camera health is the minimum requirement for user-mode diagnostic/basic flows. With only front healthy, vehicle, plate, and person tasks may run against front data, while missing side/ceiling safety coverage remains explicit NG and final PLC OK stays blocked.
- When `BIRDVIEW_MODE=disabled`, all ceiling birdview surfaces are omitted, the front camera becomes the only driver-facing camera, and alignment displays a stop instruction. This temporary deployment mode never authorizes final OK.

## Operator Dashboard

Operator mode is reached through the hidden user-mode hold or the service keyboard fallback.

Default operator layout:

- Ceiling birdview and front camera are the primary first-screen views.
- Ceiling birdview is shown as a vertical tile and the frame is displayed CCW 90 degrees.
- The birdview tile must not draw default lane/stop guide lines unless calibration mode or a specific overlay mode is active.
- The front view stays wide and is used for vehicle entry context.
- Safety status, state, PLC status, camera health, AI inference state, and clock remain visible.
- Camera loss, stale frames, Hailo errors, and PLC unknown state must be visible as NG or blocked states.

The all-camera layout remains available from the sidebar for inspection of ceiling, front, rear-side, and opposite-side cameras.

In disabled birdview mode, the dashboard shows only front and the all-camera layout shows front, rear-side, and opposite-side. The operator status strip must show `버드뷰 OFF · 최종 OK 차단`; the ceiling tile and rotation control remain hidden.

## Sidebar and Feature Slots

The sidebar is a collapsible, vertically scrollable operator control surface. The menu must stay reachable on short displays, so entries live in a scroll area rather than an unbounded column.

Current connected entries:

- `사용자모드`
- `전체 카메라`
- `카메라 설정`
- `이전 AI Detection`
- `차량 전용 검출`
- `번호판 이미지 LPR`
- `정면카메라LPR`
- `사람 존재 감지`
- `LD2410`
- `차량 진입 시뮬레이션`
- `NAS 연결 확인`
- `종료`

`사용자모드` returns to the driver-facing surface. `종료` closes the application and must ask for explicit
confirmation first; a declined confirmation changes nothing. Neither action changes safety state, calibration
state, or PLC output.

`NAS 연결 확인` writes one throwaway payload to `<SYNOLOGY_NAS_FOLDER>/connectiontest/check-<UTC timestamp>/`
using the same strict-host-key SFTP, atomic `.part` publication, and SHA-256 read-back verification as the
operational archive path, so a pass proves the real write path rather than a reachability ping. It always
uploads a summary JSON carrying `safe_to_operate: false`, and when a camera is streaming it also uploads a
two-second clip from that camera. Frame collection runs on a UI timer and the encode/upload runs off the UI
thread. Missing `SYNOLOGY_NAS_*` settings must be reported in the operator status row instead of attempting a
connection. The result is diagnostic evidence only and never authorizes final OK.

`이전 AI Detection` is a regression-isolation control. It should bypass runtime model selection and launch the previous multistream detection path that uses `HAILO_HEF_PATH`, the shared detection event directory, and the same camera rotation map as the visible UI.

Purpose-specific AI controls should use fixed, known-compatible TAPPAS example model sets:

- `차량 전용 검출`: front camera only, Hailo LPR example `yolov5m_vehicles`.
- `번호판 이미지 LPR`: image-set test using `tmp/car_number-test` and FastALPR ONNX. It must log per-image inference time and OCR results.
- `사람 존재 감지`: currently streaming cameras using the TAPPAS person detector path. It must infer only whether a person exists; do not run Re-ID embedding, gallery matching, or same-person tracking.

These controls are for integration diagnosis and staged feature development. They must show running/error/log status in the operator status strip and keep final OK blocked.

The `LD2410` entry opens a read-only serial-style console in the operator workspace. It shows connection state, parsed values, and original HEX for the latest 500 frames. Pause and clear affect presentation only; LD2410 remains raw audit context and cannot alter safety state, PLC state, calibration state, or final OK.

## UI Test and Simulation Behavior

UI test and simulation actions are for implementation verification only.

- Vehicle-entry simulation may draw test overlays and update instruction text.
- Driver-stage controls live in the operator-only `사용자 화면 테스트` panel.
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
- Tests that LD2410 console navigation and display controls are safety-neutral.
- Tests that vehicle-entry simulation remains test-only and blocks final OK.
- Tests that purpose-specific AI buttons create the expected fixed-model pipeline and do not use arbitrary HEF selection.
- Tests for camera tile layout, birdview rotation policy, and stale/NG display.
- Tests for Korean operator instruction selection.
- Tests that password values are redacted.
- Tests for calibration save/load validation.
- Tests that error/NG states cannot display final OK styling.
