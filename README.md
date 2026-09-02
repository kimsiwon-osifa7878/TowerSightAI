# TowerSightAI

TowerSightAI is a safety-first AI monitoring system for a parking machine. It combines four RTSP camera streams, Hailo-8 object detection, conservative state handling, PLC adapter boundaries, and a PyQt6 operator console.

The current repository is still an implementation prototype, not a production safety release. The core rule is unchanged: unknown, stale, missing, low-confidence, simulated, or unhealthy inputs must block final OK and keep the system in NG/wait.

## Product Workflow And Display Intent

TowerSightAI is intended to support the full parking-machine journey without shifting the safety burden to the driver or operator:

```text
vehicle approach
  -> parking position guidance
  -> plate recognition
  -> driver exit guidance
  -> parking-machine and in-vehicle person checks
  -> operation remains blocked until every required safety condition is verified
```

The installed display has two distinct modes:

- **User mode** is the default driver-facing guidance surface. It prioritizes edge-to-edge live camera context, one large current instruction, and an unmistakable stop/blocked state. Ceiling birdview alignment is shown only when `BIRDVIEW_MODE=ceiling`.
- **Operator mode** retains camera inspection, AI diagnostics, settings, and test controls. Development telemetry and model details belong here rather than on the driver-facing screen.

The visual direction for user mode is a near-black automotive surround-view display with a 50%-transparent top instruction overlay, compact bottom status, and prominent red blocking states. The references are a product and visual guide, not proof that any safety function is implemented or validated.

The current implementation covers entry-side camera and Hailo integration only in part. Driver approach, vehicle movement, and exit-complete confirmation during the outbound flow remain future product work and must not be inferred from the current UI.

## Current Status

Implemented:

- Typed `.env` loading and validation for four camera roles: `ceiling`, `front`, `rear_side`, `opposite_side`.
- RTSP preview pipeline generation with redacted source handling.
- PyQt6 application that starts in driver-facing user mode and enters the operator dashboard through a hidden two-second hold in the top-right corner.
- Configurable birdview mode: `disabled` hides and stops the ceiling source, while `ceiling` restores the existing ceiling/front layout.
- Collapsible sidebar with connected actions and an LD2410 live raw console.
- Runtime camera capture for active cameras, with disconnected active cameras shown as NG.
- Hailo installation checks and sample image smoke test using `data/samples/test-car.png`.
- Hailo callback normalization into JSONL detection events.
- Live AI inference overlays on camera frames.
- Live Hailo multistream detection using one GStreamer pipeline:

  ```text
  RTSP sources -> Hailo Apps multisource pipeline -> buffer callback -> JSONL events -> UI overlays
  ```

- Bounding-box correction for the difference between source frame resolution and YOLO 640x640 letterboxed inference input.
- Actual received frame resolution display in each camera tile.
- Purpose-specific Hailo buttons for vehicle-only detection, LPR image checks, and person-presence detection.
- Person-presence detection uses the Hailo Apps detector's `person` class only; Re-ID embedding and gallery matching are intentionally not used.
- Fatal Hailo/GStreamer log detection that stops stuck `gst-launch` processes instead of leaving the UI in a loading state.
- Fake PLC adapter and state-machine core used by tests.
- Hourly JSONL raw-data shards for application/AI start, vehicle entry, plate results, raw per-camera detections, and 0.5-second person-presence samples through five seconds after clear. Closed shards are gzip-compressed.
- Event evidence capture: JPEG snapshots plus H.264-passthrough MKV clips for real vehicle/person events, and a high-quality image for a real plate result. Simulation never creates authoritative evidence.
- Strict-host-key Synology SFTP synchronization using a versioned, file-level SHA-256 manifest, atomic remote publication, retry markers, and local deletion only after a verified upload is at least 14 days old.

Known gaps:

- Stage-specific AI decisions are not complete: vehicle alignment, plate recognition, person/obstacle safety fusion, and in-vehicle occupancy still need production logic.
- Calibration editing and validation UI is not implemented yet.
- Real PLC protocol adapter is not implemented yet.
- Final OK remains blocked until the missing safety prerequisites are implemented and verified.

## Development Direction

Near-term work is UI-first:

1. Add the operator UI button, status row, panel, or test slot.
2. Connect it to fake, diagnostic, or simulation behavior that cannot change final OK.
3. Add UI and fake-data tests.
4. Connect real camera, Hailo, calibration, AI-stage, or PLC logic.

The LD2410 operator console is diagnostic-only. Viewing, pausing, or clearing it must not change safety state or send PLC events.

## Safety Rules

- Never send PLC OK on camera loss, missing frames, unknown PLC state, invalid calibration, Hailo failure, low confidence, simulated input, or possible human/obstacle presence.
- Keep deployment-specific values in `.env`; do not commit real RTSP credentials, PLC secrets, or local Hailo install paths.
- `.env.example` must contain placeholders only.
- Hardware tests must be opt-in or clearly manual.
- UI tests and simulations are implementation checks only and do not imply safe operation.

## Install

```bash
cd /home/erumtni/TowerSightAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[ui]" pytest
```

On the Ubuntu/Hailo target, do not install the Developer Zone's unconditional `Latest` runtime. TowerSightAI targets Hailo-8 and uses the officially compatible **HailoRT 4.23.0 + TAPPAS Core 5.1.0** stack. HailoRT 5.x combinations are for Hailo-10H.

For a fresh Ubuntu 24.04 installation, follow the guide below from Developer Zone account setup and package selection through selective `yolov8m` download, postprocess compilation, FastALPR preparation, and real Hailo inference verification:

- [Hailo-8 Ubuntu 설치 가이드](docs/hailo8-ubuntu-installation.md)

Runtime defaults are `~/hailo-apps`, Hailo-8 `yolov8m.hef`, and `libyolo_hailortpp_postprocess.so`. Vehicle detection filters the compatible COCO detector to vehicle labels, while person presence filters it to `person`. The legacy `hailopython`, purpose-specific YOLOv5 HEFs, JSON configs, and crop libraries are not active.

### FastALPR Models

`번호판 이미지 인식` does **not** use the TAPPAS `tiny_yolov4_license_plates.hef` or `lprnet.hef` pipeline. The current implementation uses [FastALPR](https://github.com/ankandrew/fast-alpr) on the CPU with:

- Detector: `yolo-v9-t-384-license-plate-end2end`
- OCR: `cct-xs-v2-global-model`

Installing this project with `python -m pip install -e ".[ui]"` installs `fast-alpr[onnx]`. `FAST_ALPR_DETECTOR_MODEL` and `FAST_ALPR_OCR_MODEL` select the two active FastALPR models. The installation guide initializes FastALPR once while online so both ONNX models are downloaded to the deployment user's cache before the UI is used. This path is independent of Hailo Apps.

Installation and inference checks never authorize PLC OK. If an AI button fails after following the installation guide, inspect:

- `artifacts/runtime/detections/` for previous/general AI detection.
- `artifacts/runtime/purpose-ai/vehicle_detection/vehicle.gst.log` for vehicle-only detection.
- `artifacts/runtime/purpose-ai/person_presence/person_presence.gst.log` for person-presence detection.
- `artifacts/runtime/purpose-ai/lpr_image/lpr.gst.log` for FastALPR image LPR.
- `artifacts/runtime/purpose-ai/front_camera_lpr/lpr.gst.log` for front-camera snapshot LPR.

### Collect AI Failure Results

Run the operator UI with diagnostic logging enabled:

```bash
LOG_LEVEL=DEBUG towersightai-operator-ui --env .env
```

The terminal and `artifacts/runtime/towersightai.log` show each AI run ID, resolved model/config/library paths, process ID, first result, event counts, exit code, and the relevant raw-log path. RTSP credentials are redacted.

Immediately after reproducing a failure, and before launching a different AI task, collect the latest evidence into one text file:

```bash
# Run this once after pulling the logging update so the new CLI is registered.
python -m pip install -e ".[ui]"

towersightai-ai-diagnostics --env .env \
  --output artifacts/runtime/ai-diagnostics.txt
```

If the console script is not yet on `PATH`, use the equivalent module command:

```bash
python -m towersightai.cli.ai_diagnostics --env .env \
  --output artifacts/runtime/ai-diagnostics.txt
```

The collector does not start a camera, GStreamer pipeline, Hailo inference, or FastALPR inference. It only reads the existing run-status files, JSONL events, log tails, configured resource metadata, executable locations, and FastALPR cache metadata. Missing files are reported as `missing` instead of aborting collection. Send the resulting `artifacts/runtime/ai-diagnostics.txt` when reporting the failure; the `.env` contents and RTSP credentials are not included.

## Environment

Create a site-local `.env` from the placeholder file:

```bash
cp .env.example .env
```

Important values:

- `HAILO_APPS_WORKSPACE`, `HAILO_APPS_RESOURCES`, `HAILO_APPS_PYTHON`: Hailo Apps checkout, resource tree, and its Python.
- `TAPPAS_WORKSPACE`: backward-compatible alias; set it to `${HAILO_APPS_WORKSPACE}`.
- `HAILO_MODEL_DIR`: architecture-specific Hailo Apps model directory.
- `HAILO_HEF_PATH`, `HAILO_POSTPROCESS_SO`, `HAILO_NETWORK_NAME`: previous/general AI model mapping.
- `HAILO_VEHICLE_DETECTION_*`: vehicle-task HEF and postprocess mapping.
- `HAILO_PERSON_PRESENCE_*`: person-task HEF and postprocess mapping.
- `FAST_ALPR_DETECTOR_MODEL`, `FAST_ALPR_OCR_MODEL`: CPU-side plate detector and OCR model selection.
- `HAILO_NETWORK_NAME`: defaults to `filter_letterbox` for the current YOLO postprocess.
- `CAMERA_1_*` through `CAMERA_4_*`: camera ID, role, preview RTSP URL, dedicated H.264
  `CAMERA_N_RECORD_RTSP_URL` (point it at `stream2`: preview and AI inference both hold `stream1`
  sessions, and Tapo refuses a third `stream1` session with RTSP 400), optional username/password,
  and `CAMERA_N_ROTATION_DEGREES`.
- `CALIBRATION_PATH`: calibration JSON path.
- `PLC_ENDPOINT`: PLC or simulator endpoint.
- `UI_CAMERA_RESOLUTION`: preview/display capture resolution, for example `1920x1080` or `1280x720`.
- `BIRDVIEW_MODE`: `disabled` for the current three-camera UI or `ceiling` for the existing ceiling-camera birdview.
- `LD2410_TCP_*`: enables the one-client ESP32 TCP listener (default `0.0.0.0:9000`), 30-second timestamp buffer, one-second freshness limit, and five-second invalid/idle-client timeout.
- `RAW_DATA_*`: local archive directory, hourly shard duration, 0.5-second sampling, five-second person-clear tail, Asia/Seoul day boundary, synchronization interval, and 14-day retention.
- `RAW_MEDIA_*`: snapshot quality/staleness, five-second video pre-roll, vehicle post-roll, recorder segment/clip-part duration, and the system Python containing GStreamer GI.
- `SYNOLOGY_NAS_*`: SFTP hostname, external port, dedicated account, password, SFTP-visible destination folder, and trusted `known_hosts` file. The host value must not include `https://` or a port.

Camera rotation is part of the equipment configuration. Set `CAMERA_N_ROTATION_DEGREES` to one of `0`, `90`, `180`, or `270`; `90` means CCW 90 degrees and `270` means CW 90 degrees. The default site profile uses `CAMERA_1_ROTATION_DEGREES=90` for the ceiling birdview camera and `0` for the other cameras. The Hailo Apps adapter transforms emitted bounding-box coordinates to the same orientation shown by the operator UI.

The UI displays the actual received frame size in each camera tile, so you can verify whether the configured resolution is being applied.

## Raw Data And NAS Archive

With `RAW_DATA_ENABLED=true`, records are appended to bounded `artifacts/raw/YYYY-MM-DD/events-YYYYMMDD-HHMM.jsonl` shards. A closed shard is atomically published as `.jsonl.gz`. Existing legacy `events.jsonl` files remain readable/uploadable and are not automatically rewritten or deleted.

With `RAW_MEDIA_ENABLED=true`, real vehicle entry saves the front-camera snapshot and a clip covering five seconds before through ten seconds after the event. A real person window saves snapshots and clips from every healthy active camera, covering five seconds before the first detection through the configured clear tail. A real plate result saves the high-quality LPR source image and, when FastALPR supplies a valid bounding box, a separate plate crop; it does not create video. Video is copied from the camera H.264 stream into silent MKV without re-encoding and is capped at five-minute parts. Media bytes are never embedded in JSONL: `media_artifact_created` records store only relative path, byte size, SHA-256, capture time, and metadata. Capture failures are explicit `media_capture_failed` events and do not alter PLC state.

The current day remains local and completed days are uploaded in the background to `${SYNOLOGY_NAS_FOLDER}/raw/YYYY-MM-DD/`. `manifest.json` schema v2 lists every JSONL/gzip/image/video artifact with its relative path, media type, size, and SHA-256. Changed or missing files are uploaded to `.part`, verified, atomically renamed, and the manifest is published last.

The person window starts on the first `person` detection. It records every active camera's latest person state every 0.5 seconds, treats a detection as stale after the configured interval, and continues absence samples for five seconds. When `LD2410_TCP_ENABLED=true`, the application also listens for raw LD2410 binary report frames from one ESP32 client. Each `person_sample` embeds the latest frame received at or before that sample time, including parsed distances/energy/gates and the original hex. Frames up to one second old are `fresh`, older buffered frames are `stale`, and missing frames are `unavailable`; future frames are never selected. LD2410 input is audit-only and cannot affect AI, the state machine, or PLC OK/NG.

The ESP32 should connect to this Ubuntu device's LAN address and `LD2410_TCP_PORT`, then forward complete or chunked LD2410 bytes beginning with `F4 F3 F2 F1` every 0.5 seconds. Reserve the Ubuntu device's address in the router DHCP configuration so the ESP32 target does not change. The listener accepts a reconnect after disconnect and closes a client that produces no valid frame for five seconds.

Failed or unverified uploads are never deleted locally. A local day is deleted recursively only when its exact manifest was verified on NAS and it reaches the configured 14-day age.

Run a completed-day sync manually with:

```bash
towersightai-sync-raw-data --env .env
```

For an explicit test after stopping every UI/raw writer, seal and upload today's archive immediately with:

```bash
towersightai-sync-raw-data --env .env --include-current-day
```

This storage path is an operational audit/archive function only. Its success or failure does not authorize PLC OK.

To check the NAS write path on site without waiting for a completed day, use the operator sidebar's
`NAS 연결 확인`. It uploads a summary JSON, plus a two-second camera clip when a camera is streaming, to
`${SYNOLOGY_NAS_FOLDER}/connectiontest/check-<UTC timestamp>/`, verifies every file by SHA-256 read-back, and
reports the remote path in the operator status row. These runs are diagnostic artifacts: delete the
`connectiontest` folder whenever you want, and never treat a pass as safety approval.

## Run The Operator UI

Start the fullscreen operator console. The script uses the repository `.venv`
and `.env` automatically:

```bash
./run.sh
```

For a normal desktop window:

```bash
./run-window.sh
```

The default entry screen is user mode. Press the `운영자 모드` button in the bottom-right corner to enter the
operator dashboard; this is the intended on-site entry point. Holding the invisible `72 x 72 px` area in the
top-right corner for two seconds with a mouse or touch input does the same thing, and releasing early or moving
outside that area cancels the transition. `Ctrl+Shift+O` remains a service-keyboard fallback.

From operator mode, `사용자 화면` in the sidebar returns to the driver display and `프로그램 종료` closes the application
after a confirmation dialog. Declining the confirmation leaves the application running. Neither control changes
safety state or sends a PLC event.

Operator console behavior (developer mode):

- User mode fills the display with state-priority cameras and overlays one large driver action at the top.
- The operator sidebar is grouped into `운영` / `진단` / `시스템` sections and navigates to workspace pages;
  task run/stop controls live inside their pages. It scrolls vertically so every entry stays reachable.
- `전체 카메라` is the landing page: the active-camera grid with centered, ratio-correct tiles, plus the
  `이전 AI Detection` regression-isolation toggle.
- With `BIRDVIEW_MODE=disabled` the grid shows front plus both side cameras; with `BIRDVIEW_MODE=ceiling`
  the ceiling birdview joins the grid using its configured rotation. Either way final OK stays blocked.
- `차량 감지` focuses the front camera and runs the configured Hailo Apps detector with vehicle labels only.
- `사람 감지` runs the detector on currently streaming cameras and keeps `person` only.
- `번호판 인식` offers `정면 카메라 인식` (front snapshot) and `이미지 LPR` (`tmp/car_number-test`), both FastALPR
  on CPU, with the latest result shown on the page.
- `레이더 (LD2410)` shows the latest 500 ESP32 frames as parsed values plus original hexadecimal bytes, with connection status and display-only pause/clear controls.
- `NAS 연결 확인` writes a test payload to `${SYNOLOGY_NAS_FOLDER}/connectiontest/check-<UTC timestamp>/` and
  verifies it by reading it back. When a camera is streaming it also uploads a two-second clip from that
  camera. It is diagnostic only and keeps PLC OK blocked.
- `시스템 점검` runs settings/Hailo/camera/PLC diagnostics off the UI thread; every result stays
  `safe_to_operate=False`. The page also shows a Hailo device-health panel refreshed every 60 s:
  PCIe presence, driver version, `/dev/hailo0`, chip responsiveness + temperature, and the AER RxErr
  counter whose growth precedes a hung device. The same health state is always visible as a `HAILO`
  pill in the status strip (green/amber/red) and is written to `towersightai.log` under
  `towersightai.hailo.health` — filter the `실행 로그` page with `hailo-health` to see the history.
- `실행 로그` tails `artifacts/runtime/towersightai.log` with a substring filter and follow mode.
- `주차 프로세스 테스트` hosts the driver-stage playback buttons and `차량 진입 시뮬레이션`; all of it is UI-only
  and keeps PLC OK blocked.
- `프로그램 종료` asks for confirmation and then closes the application. It never sends a PLC event.

Camera preview capture first tries the OpenCV GStreamer backend. When the installed `cv2` was built
without GStreamer (the pip `opencv-python` wheels are), every active camera falls back to a direct FFmpeg
RTSP capture; the configured rotation is applied in software on that path. The fallback and each per-camera
capture state transition are logged under `towersightai.camera.capture` in
`artifacts/runtime/towersightai.log`, so a camera that never opens is now visible in the log as
`camera-capture-status ... status=open-failed`.

Disconnected active cameras remain visible as NG tiles and are not used as inference targets. A disabled birdview is not counted as a failed active camera, but is shown separately as `버드뷰 OFF` and always blocks final OK. Currently connected streams are selected from runtime camera status, not from static `.env` presence.

## Purpose AI Checks

Purpose-specific AI buttons use the current Hailo Apps detection resource set and always keep PLC OK blocked:

- `차량 감지`: `HAILO_VEHICLE_DETECTION_HEF_PATH`, defaulting to Hailo-8 `yolov8m.hef`, with vehicle-label filtering.
- `번호판 이미지 인식`: CPU-side FastALPR with `yolo-v9-t-384-license-plate-end2end` and `cct-xs-v2-global-model`; it does not use the legacy TAPPAS LPR HEFs.
- `사람 감지`: `HAILO_PERSON_PRESENCE_HEF_PATH`, defaulting to Hailo-8 `yolov8m.hef`, with `person` filtering; it does not run Re-ID embedding, gallery matching, or same-person tracking.

Logs are written under `artifacts/runtime/purpose-ai/`. These buttons are implementation and integration checks only; they do not authorize PLC OK.

For regression debugging, `이전 AI Detection` bypasses purpose-specific controls and launches the previous multistream path that uses `HAILO_HEF_PATH`, shared `artifacts/runtime/detections/multistream.jsonl`, and the current UI camera rotation map.

The inference runner watches GStreamer logs for fatal Hailo conditions such as `HAILO_OUT_OF_PHYSICAL_DEVICES`, `Failed to create vdevice`, `CHECK_SUCCESS failed`, and `Caught SIGSEGV`. If one appears, the UI shows an inference error, terminates the process group, and keeps final OK blocked.

Recent hardware checks on the target verified:

```text
vehicle_detection: 2 events, no errors
person_presence: 138 events, no errors
```

If detections disappear after some time, check the relevant log under `artifacts/runtime/purpose-ai/` or `artifacts/runtime/detections/` and confirm the model matches the configured postprocess library and network name.

## Hailo Sample Image Smoke

The sample image smoke test uses:

```text
data/samples/test-car.png
```

CLI dry run:

```bash
towersightai-hailo-image-smoke --env .env --image data/samples/test-car.png --check-installation
```

Run on the Hailo target:

```bash
RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke \
  --env .env \
  --image data/samples/test-car.png \
  --check-installation \
  --run
```

This command is validation only. It never authorizes PLC OK.

## Settings And Camera Checks

```bash
towersightai-check-settings --env .env
towersightai-check-settings --env .env --check-hailo
towersightai-check-settings --env .env --health-check-cameras
towersightai-check-settings --env .env --preview-cameras --dry-run
```

The CLI redacts RTSP credentials before printing sources or pipelines.

## UI Screenshot Verification

On the Ubuntu desktop target:

```bash
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```

This launches the operator UI, captures the desktop, prints PNG metadata, and exits the app. Generated screenshots under `tmp/` are local verification artifacts and should not be committed.

For UI-centered changes, also verify the main button flows directly in the running UI:

1. Confirm the first screen is the operator dashboard.
2. Click `메뉴` and confirm the sidebar opens.
3. Click `전체 카메라` and confirm the four-camera view appears.
4. Click `사람 감지` and confirm the status strip shows the purpose AI task.
5. Click `차량 진입 시뮬레이션` and confirm it is visibly test-only.
6. Click `레이더 (LD2410)` and confirm live parsed values plus HEX appear without changing PLC or safety state.
7. Confirm simulation and LD2410 console actions do not show final OK.

If a GUI session is unavailable, record that the screenshot/button verification was not run and why. UI screenshot verification is not safety approval and never authorizes PLC OK.

## Tests

```bash
pytest -q
```

Current local result:

```text
51 passed
```

Unit and UI tests do not require live RTSP cameras, Hailo-8, or PLC hardware.

## Project Layout

```text
TowerSightAI/
├── data/samples/                  # sanitized sample images
├── docs/                          # design and implementation guides
├── refers/                        # Hailo/GStreamer reference code; do not edit unless requested
├── tests/                         # hardware-free unit and UI tests
├── tools/                         # local verification scripts
└── towersightai/
    ├── camera/                    # RTSP/GStreamer preview helpers
    ├── config/                    # typed settings and .env loader
    ├── inference/                 # Hailo checks, callbacks, smoke tests, live detection
    ├── plc/                       # PLC adapter boundary and fake adapter
    ├── state_machine/             # safety state machine
    └── ui/                        # PyQt6 operator console and UI model
```

## Development Notes

- Read `AGENTS.md` and the implementation docs before changing safety behavior.
- Keep `refers/` unchanged unless specifically asked.
- Add UI-visible controls and tests before connecting new production behavior.
