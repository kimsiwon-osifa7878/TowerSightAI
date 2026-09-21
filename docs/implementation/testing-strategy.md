# Testing Strategy Guide

Every implementation stage needs tests. Safety logic must be testable without live cameras, Hailo-8, or PLC hardware. The current operator UI is the primary field-verification surface for dashboard, camera, and purpose-AI flows.

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
- Operator dashboard, sidebar, LD2410 console, and simulation behavior.
- Purpose-specific Hailo pipeline builders for vehicle detection, LPR image tests, and person-presence detection.
- Fatal Hailo/GStreamer log handling so stuck `gst-launch` processes do not leave the UI loading indefinitely.
- Daily raw JSONL schema, 0.5-second person sampling, five-second clear tail, completed-day NAS selection, upload retry, and verified 14-day retention.
- Hailo device-health snapshots: fake-sysfs collection (missing PCIe device, unloaded driver, hung chip
  with the cold-boot hint, RxErr growth degradation), pill/panel rendering, and the rule that health
  output never changes `can_show_final_ok`.
- NAS connection-check payload contents, `connectiontest` remote path construction, missing-settings handling, and failure reporting through an injected uploader instead of a live SFTP session.
- 번호판 뒷자리 4자리: the engine votes on `tail_digits` (not the whole OCR string), keeps the whole text as `plate_text` for audit, rejects a read without four trailing digits instead of guessing, and a stationary car waits for `min_reads_for_vote` agreeing reads before the vote closes (`tests/test_process_engine.py`); `extract_tail_digits` covers the Hangul-as-letter and Hangul-as-digit cases (`tests/test_hailo_events.py`).
- 입고/출고 판별: a side-on front box becomes `vehicle_exiting` (public IDLE, `출고중` copy, LPR stopped, no `vehicle_entry`), a person during a retrieval is not reported, the retrieval ends when the car is gone, an unclear direction without a plate aborts as an error instead of an entry, a plate read confirms an entry regardless of shape, and a trigger with no front vehicle returns to IDLE silently (`tests/test_process_engine.py`).
- Media filenames: the local date-time with a `_kr` suffix (`_local` outside Asia/Seoul), matching the day folder's timezone (`tests/test_evidence.py`).
- Plate read timing: the window runs from the front camera's first sight of the car, a car that never arrives ends at the arrival cap, a stationary car cannot end the vote before a read lands or `min_read_seconds` passes, and the decided plate carries the highest-confidence read's frame path and box for the crop (`tests/test_process_engine.py`, `tests/test_evidence.py`).
- Plate recording: every 1 Hz front-camera read is kept as `plate_attempt` with its accept/reject reason, a recognized vote records its size, and 미인식 or an entry aborted mid-vote still writes an explicit `plate_recognized` row (`tests/test_process_engine.py`, `tests/test_raw_data.py`).
- Radar is verification-only: the engine exposes no radar input and holds no radar state, and an LD2410 frame reaching the operator console leaves the engine untouched and person-possible false (`tests/test_process_engine.py`, `tests/test_pyqt_app.py`).
- Radar-vs-camera comparison: while a radar window is open and no camera person window is active, a 1 Hz `radar_sample` records per-camera person state with the radar snapshot, bounded by `RAW_DATA_RADAR_SAMPLE_SECONDS` and disabled at 0; `camera_state` survives a closed person window (`tests/test_raw_data.py`), and the digest/episode layer summarizes camera agreement per radar window (`tests/test_analyze_core.py`).
- Analysis dashboard (`tests/test_analyze_core.py`, `tests/test_analyze_server.py`): synthetic raw day → digest (frames, radar samples, media, coverage), camera/radar episode replay (debounce, confidence gate, camera subset, confirm/clear, gates, stale = unknown), pairing/agreement, media linkage (vehicle clips excluded), stable episode ids, append-only labels + reviewer κ, precision / mutual recall / false-per-hour / sweeps, digest cache invalidation, sites store (password never in public dict, blank keeps stored), fake-SFTP NAS reader (both layouts, SHA-256 rejection, missing manifest → partial), and every HTTP route through `AnalysisApp.dispatch` (static, media traversal block, label round-trip, CSV, background sync). A test pins that the package never imports engine/state-machine/PLC/UI modules.
- LD2410 split/coalesced binary parsing, malformed-frame recovery, past-only 0.5-second timestamp alignment, freshness/expiry, TCP idle timeout, and client reconnection.
- Boot autostart scripts (`test_autorun_scripts.py`): the generated unit launches `run.sh` with absolute paths, binds to `graphical-session.target`, restarts always with no start limit (the key must sit in `[Unit]` or systemd ignores it, which `systemd-analyze verify` now pins), install does not start, stop does not unregister, start/stop refuse when nothing is registered, and uninstall is idempotent. The tests use a fake `systemctl` and a temp unit dir, so no service is installed on the test machine.
- Hailo failure evidence (`test_hailo_incident.py`): the bundle captures link speed / AER counters for endpoint and upstream port, driver and node state, keyword-filtered kernel messages, identify output, a frozen log tail and the child log; it is still written when every source is missing; RTSP credentials never appear in any collected file; the app-log copy does not grow after collection; the reporter uploads once per transition into `error`/`degraded`, throttles repeats to `HAILO_INCIDENT_MIN_INTERVAL_SECONDS`, re-uploads after recovery, stays off unless `RAW_DATA_ENABLED` + `HAILO_INCIDENT_UPLOAD_ENABLED` + a NAS host are all set, reports build/upload failures instead of raising, and uses a remote folder isolated from the operator `transfer/` relay. UI (`test_pyqt_app.py`): healthy `hailo_health` rows are thinned while bad rows are always recorded, the upload result is shown on 시스템 점검, the worker emits the report off the UI thread, and final OK stays blocked throughout.
- Radar raw logging (`test_raw_data.py`): 1 Hz `ld2410_sample` outside person windows, stale de-duplication and `unavailable` suppression, interval 0 disables sampling and windows, one `provider_error` row then recovery, `person_sample.ld2410` keeps `raw_hex`, bounded catch-up after a stall; `RadarWindowTracker` opens after 3 s backdated to the first present sample, closes 5 s after the last present sample with aggregates, treats stale/unavailable as unknown (`radar_unavailable`), starts a new id on re-detection, force-closes with `service_stopped` / `application_stopped`, and the window events are durable while the sample is not. Evidence (`test_evidence.py`): `radar` snapshot + 30 s-capped clip, `radar_end` snapshot, `person_window_active`, `radar_evidence_throttled`, disabled flag and `simulated` do nothing. Config/env: defaults and validation of the six `RAW_*RADAR*` / `RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS` keys. UI: LD2410 `stopped` calls `close_radar_window("service_stopped")` and final OK stays blocked.

## UI-Only Tests

Run without real hardware:

- App starts in driver-facing user mode with no development buttons.
- The hidden top-right hotspot requires a completed two-second hold and cancels on early release or pointer exit.
- The visible bottom-right `운영자 모드` button enters operator mode in one press, stays inside the view and clear of the instruction panel and bottom strip at every supported size, and is absent from the operator-mode driver preview.
- The operator `프로그램 종료` action is ignored while operator mode is locked, keeps the application running when the confirmation is declined, and never changes `can_show_final_ok`.
- `NAS 연결 확인` reports missing `SYNOLOGY_NAS_*` settings without contacting the NAS, runs without a camera, collects a two-second clip when a camera is streaming, ignores a second click while running, and keeps final OK blocked for both success and failure results.
- `NAS 파일 전송` shows the single `transfer/` target folder, refuses to send with no selection or missing `SYNOLOGY_NAS_*` settings, ignores duplicate picks and a second click while running, uploads the selected files on a worker thread, clears the list only after success, and keeps final OK blocked.
- `카메라 캘리브레이션` preselects the front-left vehicle-box camera and shows only that tile, refuses to start without a streaming camera, captures a frame after two steady checkerboard detections (or on `지금 촬영`), resets on a frame without a board, skips poses, refuses `측정 실행` below 10 samples, runs the calibration worker off-thread, writes the intrinsics JSON with `reviewed=false`, ends the session when the camera changes, and keeps final OK blocked. `tests/test_hailo_health.py` builds a fake `/proc` to check device-holder detection (own child vs orphan), the degraded `HAILO 점유됨` state, the conflict explanation, and SIGTERM→SIGKILL of foreign holders only; `tests/test_hailo_apps_child_watchdog.py` and `tests/test_event_video_recorder_guard.py` cover the children's ppid watchdog and death-signal arming; orphan recorder/inference detection is covered in `test_hailo_health.py`; the RTSP 400 backoff, camera-set settle, and escalating monitoring cooldown are covered in `test_purpose_tasks.py` / `test_pyqt_app.py`; runner tests check that fatal messages name the holder PID and drop pipeline noise. `tests/test_calibration_intrinsics.py` renders synthetic distorted views through a known camera and checks that the recovered model undistorts to within 1.5 px.
- Every sidebar entry stays reachable through the scrollable menu on a short display.
- User and operator surfaces switch within the stack instead of appearing together.
- Sidebar opens and closes.
- Dashboard and all-camera layouts switch correctly.
- Ceiling birdview uses the vertical dashboard tile and CCW 90-degree display policy.
- Disabled birdview mode omits the ceiling tile and worker from every UI, shows stable one/three-camera layouts, and keeps final OK blocked.
- LD2410 console navigation, pause, and clear do not change safety state.
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

## Diagnostic Tests

Run through CLI or explicit hardware verification tooling:

- Settings validation.
- Hailo installation check.
- Hailo sample image inference.
- Per-camera frame receive checks.
- PLC simulator interface check.
- UI-only control checks.
- Fake event playback checks.
- Full hardware smoke sequence when explicitly selected.

Diagnostic results must default to `safe_to_operate=False`. A passing diagnostic means the test passed, not that PLC OK is authorized.

## UI Manual Verification

For operator UI changes, implementation verification should include a real UI run, button clicks, and screenshots when a GUI-capable Ubuntu desktop environment is available.

Baseline command:

```bash
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```

Manual or semi-automated checklist:

- Confirm the app starts in the dark, camera-first user mode.
- Hold the top-right hotspot for two seconds and confirm the operator dashboard replaces user mode.
- Return to user mode, press the bottom-right `운영자 모드` button, and confirm one press enters the operator dashboard.
- Click `프로그램 종료`, confirm the dialog appears, cancel it, and confirm the application is still running.
- With `BIRDVIEW_MODE=disabled`, confirm the front camera is the only dashboard tile and `버드뷰 OFF` is visible in the status strip.
- Confirm `전체 카메라` shows front, rear-side, and opposite-side without a ceiling tile.
- Confirm `메뉴` opens the sidebar.
- Click `전체 카메라` and confirm the active-camera grid page is visible with centered, ratio-correct tiles.
- On `전체 카메라`, click `사람 감지 시작`; confirm boxes appear on every streaming tile, then click `차량 감지 시작` and confirm the person task stops and the vehicle task starts automatically (both page buttons and the task pages' buttons show the same state).
- Open `차량 감지` and click `차량 감지 시작`; confirm the status strip shows the purpose task, the front camera is the only target, and `artifacts/runtime/purpose-ai/vehicle_detection/vehicle.gst.log` contains `yolov5m_vehicles.hef`.
- Open `번호판 인식` and run `이미지 LPR`; confirm `artifacts/runtime/purpose-ai/lpr_image/lpr.gst.log` contains FastALPR model names, per-image inference time, and OCR result or failure status.
- Open `사람 감지` and click `사람 감지 시작`; confirm `artifacts/runtime/purpose-ai/person_presence/person_presence.gst.log` contains `yolov5s_personface_reid.hef` and `yolov5_personface_letterbox`, but does not contain `repvgg_a0_person_reid_2048.hef` or `hailogallery`.
- Open `주차 프로세스 테스트` and click `차량 진입 시뮬레이션`; confirm only test overlay/instruction text changes.
- Click `레이더 (LD2410)` and confirm connection state plus continuous parsed/HEX rows are visible.
- Confirm simulation and LD2410 console actions do not show final OK and do not send PLC events.
- Save screenshots under `tmp/operator-ui-verification/` and keep them out of commits.

This UI verification is an implementation check, not a product safety approval. If the local environment cannot run the GUI, record the blocker in the final report.

The screenshot helper uses `xdotool` for `메뉴`, `전체 카메라`, `사람 감지`, `레이더 (LD2410)`, `실행 로그`, the user-screen test page, and the exit confirmation.

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
- Sidebar and LD2410 console actions are safe.
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
