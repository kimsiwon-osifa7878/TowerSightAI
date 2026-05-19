# TowerSightAI

TowerSightAI is a safety-first AI monitoring system for a parking machine. It combines four RTSP camera streams, Hailo-8 object detection, conservative state handling, PLC adapter boundaries, and a PyQt6 operator console.

The current repository is still an implementation prototype, not a production safety release. The core rule is unchanged: unknown, stale, missing, low-confidence, or unhealthy inputs must block final OK and keep the system in NG/wait.

## Current Status

Implemented:

- Typed `.env` loading and validation for four camera roles: `ceiling`, `front`, `rear_side`, `opposite_side`.
- RTSP preview pipeline generation with redacted source handling.
- PyQt6 operator console with driver view, operator view, and hardware test view.
- Runtime camera capture for all configured cameras, with disconnected cameras shown as NG.
- Operator unlock shortcut: `Ctrl+Shift+O`.
- Test screen with scrollable test list and compact result rows.
- Hailo installation checks and sample image smoke test using `data/samples/test-car.png`.
- Hailo callback normalization into JSONL detection events.
- Live AI Detection overlays on camera frames.
- Live Hailo multistream detection using one GStreamer pipeline:

  ```text
  RTSP sources -> hailoroundrobin -> hailonet -> hailofilter -> hailostreamrouter -> per-camera hailopython callbacks -> JSONL events -> UI overlays
  ```

- Bounding-box correction for the difference between source frame resolution and YOLO 640x640 letterboxed inference input.
- Actual received frame resolution display in each camera tile.
- Fixed-width operator side panel so long AI Detection status text does not resize the UI.
- Fake PLC adapter and state-machine core used by tests.

Known gaps:

- Long-running Hailo multistream watchdog is still basic. If the `gst-launch` process exits, the UI worker currently retries only twice.
- Stage-specific AI decisions are not complete: vehicle alignment, plate recognition, person/obstacle safety fusion, and in-vehicle occupancy still need production logic.
- Calibration editing and validation UI is not implemented yet.
- Real PLC protocol adapter is not implemented yet.
- Final OK remains blocked until the missing safety prerequisites are implemented and verified.

## Safety Rules

- Never send PLC OK on camera loss, missing frames, unknown PLC state, invalid calibration, Hailo failure, low confidence, or possible human/obstacle presence.
- Keep deployment-specific values in `.env`; do not commit real RTSP credentials, PLC secrets, or local Hailo install paths.
- `.env.example` must contain placeholders only.
- Hardware tests must be opt-in or clearly manual.

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

The default entry screen is the driver-facing view. To enter operator mode:

```text
Ctrl+Shift+O
```

Operator view contains:

- 2x2 camera grid: ceiling, front, rear side, opposite side.
- Right-side fixed-width status/control panel.
- Driver screen button.
- Test screen button.
- AI Detection toggle.

Disconnected cameras remain visible as NG tiles and are not used as AI Detection targets. Currently connected streams are selected from runtime camera status, not from static `.env` presence.

## AI Detection

In operator mode, press `AI Detection`.

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

Enter operator mode with `Ctrl+Shift+O`, then press `테스트`.

Available tests include:

- Settings validation.
- Hailo installation check.
- Hailo sample image inference.
- Per-camera frame receive checks.
- PLC simulator interface check.
- Full hardware smoke sequence.

The test list is scrollable. Failure details are written to the right console while the left result rows stay compact, preventing layout shifts.

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
49 passed
```

Unit tests do not require live RTSP cameras, Hailo-8, or PLC hardware.

## Project Layout

```text
TowerSightAI/
├── data/samples/                  # sanitized sample images
├── docs/                          # design and implementation guides
├── refers/                        # Hailo/GStreamer reference code; do not edit unless requested
├── tests/                         # hardware-free unit tests
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
- Add tests with behavioral changes.
- Treat all local camera/Hailo outputs under `artifacts/` and `tmp/` as disposable runtime artifacts.
