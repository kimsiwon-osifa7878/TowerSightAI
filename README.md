# TowerSightAI

TowerSightAI is a safety-first AI monitoring system for a parking machine. It combines four RTSP camera streams, Hailo-8 object detection, conservative state handling, PLC adapter boundaries, and a PyQt6 operator console.

The current repository is still an implementation prototype, not a production safety release. The core rule is unchanged: unknown, stale, missing, low-confidence, simulated, or unhealthy inputs must block final OK and keep the system in NG/wait.

## Current Status

Implemented:

- Typed `.env` loading and validation for four camera roles: `ceiling`, `front`, `rear_side`, `opposite_side`.
- RTSP preview pipeline generation with redacted source handling.
- PyQt6 operator console that starts on an operator dashboard.
- Dashboard layout with ceiling birdview and front camera as primary views.
- Ceiling birdview displayed as a vertical tile with CCW 90-degree frame rotation.
- Collapsible sidebar with connected actions and `EMPTY` feature slots.
- Test screen with scrollable test list and compact result rows.
- Runtime camera capture for all configured cameras, with disconnected cameras shown as NG.
- Hailo installation checks and sample image smoke test using `data/samples/test-car.png`.
- Hailo callback normalization into JSONL detection events.
- Live AI Detection overlays on camera frames.
- Live Hailo multistream detection using one GStreamer pipeline:

  ```text
  RTSP sources -> hailoroundrobin -> hailonet -> hailofilter -> hailostreamrouter -> per-camera hailopython callbacks -> JSONL events -> UI overlays
  ```

- Bounding-box correction for the difference between source frame resolution and YOLO 640x640 letterboxed inference input.
- Actual received frame resolution display in each camera tile.
- Fake PLC adapter and state-machine core used by tests.

Known gaps:

- Long-running Hailo multistream watchdog is still basic. If the `gst-launch` process exits, the UI worker currently retries only twice.
- Stage-specific AI decisions are not complete: vehicle alignment, plate recognition, person/obstacle safety fusion, and in-vehicle occupancy still need production logic.
- Calibration editing and validation UI is not implemented yet.
- Real PLC protocol adapter is not implemented yet.
- Final OK remains blocked until the missing safety prerequisites are implemented and verified.

## Development Direction

Near-term work is UI-first:

1. Add the operator UI button, status row, panel, or test slot.
2. Connect it to `EMPTY`, fake, or simulation behavior that cannot change final OK.
3. Add UI and fake-data tests.
4. Connect real camera, Hailo, calibration, AI-stage, or PLC logic.

`EMPTY` buttons are placeholders for future functionality. Pressing them must not change safety state or send PLC events.

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

On the Ubuntu/Hailo target, HailoRT/TAPPAS and the Hailo GStreamer plugins must also be installed. The code expects elements such as `hailonet`, `hailofilter`, `hailopython`, `hailoroundrobin`, and `hailostreamrouter`.

## Environment

Create a site-local `.env` from the placeholder file:

```bash
cp .env.example .env
```

Important values:

- `TAPPAS_WORKSPACE`: TAPPAS workspace path.
- `HAILO_HEF_PATH`: YOLO HEF file path.
- `HAILO_POSTPROCESS_SO`: Hailo postprocess `.so`.
- `HAILO_NETWORK_NAME`: defaults to `yolov5`.
- `CAMERA_1_*` through `CAMERA_4_*`: camera ID, role, RTSP URL, optional username/password.
- `CALIBRATION_PATH`: calibration JSON path.
- `PLC_ENDPOINT`: PLC or simulator endpoint.
- `UI_CAMERA_RESOLUTION`: preview/display capture resolution, for example `1920x1080` or `1280x720`.

The UI displays the actual received frame size in each camera tile, so you can verify whether the configured resolution is being applied.

## Run The Operator UI

```bash
source .venv/bin/activate
towersightai-operator-ui --env .env --windowed
```

The default entry screen is the operator dashboard.

Dashboard behavior:

- Ceiling birdview and front camera are shown first.
- The ceiling birdview tile is vertical and the frame is displayed CCW 90 degrees.
- The sidebar opens from the `메뉴` button.
- `전체 카메라` switches to the four-camera inspection layout.
- `테스트` opens the in-UI diagnostic screen.
- `AI Detection` starts live Hailo multistream detection for currently streaming cameras.
- `차량 진입 시뮬레이션` is UI-only and keeps PLC OK blocked.
- `EMPTY` buttons are safe no-op feature slots.

Disconnected cameras remain visible as NG tiles and are not used as AI Detection targets. Currently connected streams are selected from runtime camera status, not from static `.env` presence.

## AI Detection

In the operator UI sidebar, press `AI Detection`.

Behavior:

- Uses only cameras that are currently streaming normally.
- Runs a single Hailo multistream GStreamer process for those cameras.
- Writes normalized detection events to `artifacts/runtime/detections/multistream.jsonl`.
- Generates temporary per-camera callback wrappers under `artifacts/runtime/detections/`.
- Draws fresh detection boxes and labels on the corresponding camera tile.
- Keeps boxes visible only while events are fresh. The current overlay TTL is 1 second.

Hardware check performed on the target showed both active cameras producing events in the same multistream run:

```text
COUNTS {'front': 1158, 'ceiling': 2366}
```

If detections disappear after some time, check whether the GStreamer process exited. The current worker retries only twice; a stronger watchdog/restart policy is listed in `PLAN.md`.

## Operator Test Screen

Open the sidebar with `메뉴`, then press `테스트`.

Available tests include:

- Settings validation.
- Hailo installation check.
- Hailo sample image inference.
- Per-camera frame receive checks.
- PLC simulator interface check.
- Full hardware smoke sequence.

The test list is scrollable. Failure details are written to the right console while the left result rows stay compact, preventing layout shifts. Diagnostic results are not safety approval and default to `safe_to_operate=False`.

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
