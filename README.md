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
- Runtime camera capture for all configured cameras, with disconnected cameras shown as NG.
- Hailo installation checks and sample image smoke test using `data/samples/test-car.png`.
- Hailo callback normalization into JSONL detection events.
- Live AI inference overlays on camera frames.
- Live Hailo multistream detection using one GStreamer pipeline:

  ```text
  RTSP sources -> hailoroundrobin -> hailonet -> hailofilter -> hailostreamrouter -> per-camera hailopython callbacks -> JSONL events -> UI overlays
  ```

- Bounding-box correction for the difference between source frame resolution and YOLO 640x640 letterboxed inference input.
- Actual received frame resolution display in each camera tile.
- Purpose-specific Hailo buttons for vehicle-only detection, LPR image checks, and person-presence detection.
- Person-presence detection uses the TAPPAS person detector only; Re-ID embedding and gallery matching are intentionally not used.
- Fatal Hailo/GStreamer log detection that stops stuck `gst-launch` processes instead of leaving the UI in a loading state.
- Fake PLC adapter and state-machine core used by tests.

Known gaps:

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

## Hailo Models And Resources

### Supported Hailo Stack

The current pipelines and paths are based on the Hailo-8 layout from **TAPPAS 3.31.0 with the HailoRT 4.20 series**. The [official TAPPAS 3.31.0 README](https://github.com/hailo-ai/tappas/blob/v3.31.0/README.rst) identifies HailoRT 4.20 as the compatible runtime.

Newer TAPPAS and Hailo Apps releases changed application and model-resource layouts. Copying only these HEF files into a newer installation is not a supported migration: the matching postprocess libraries, JSON configurations, GStreamer elements, and HailoRT ABI must also be compatible. Supporting the current Hailo Apps layout requires a separate code migration.

### Required Files

TowerSightAI keeps deployment-local model resources under `models/hailo/` in this project. Set `HAILO_MODEL_DIR` to that directory; the default assumes commands are run from the repository root.

| Function | Required file |
|---|---|
| Previous/general AI detection | `${HAILO_MODEL_DIR}/general/yolov5m_wo_spp_60p.hef` |
| Previous/general AI postprocess | `${HAILO_MODEL_DIR}/postprocess/libyolo_hailortpp_post.so` |
| Vehicle-only detection model | `${HAILO_MODEL_DIR}/vehicle_detection/yolov5m_vehicles.hef` |
| Vehicle-only detection config | `${HAILO_MODEL_DIR}/vehicle_detection/configs/yolov5_vehicle_detection.json` |
| Person-presence model | `${HAILO_MODEL_DIR}/person_presence/yolov5s_personface_reid.hef` |
| Person-presence config | `${HAILO_MODEL_DIR}/person_presence/configs/yolov5_personface.json` |
| Person-presence postprocess | `${HAILO_MODEL_DIR}/postprocess/libyolo_post.so` |
| Person-presence crop helper | `${HAILO_MODEL_DIR}/postprocess/cropping_algorithms/libwhole_buffer.so` |

Each active Hailo function has its own explicit `.env` path. The defaults place those files below `HAILO_MODEL_DIR`, but each path can be overridden independently. `TAPPAS_WORKSPACE` is still required for the compatible TAPPAS runtime and virtual environment, but model files are no longer loaded from the TAPPAS installation tree.

The general-AI model selector exposes only `HAILO_HEF_PATH`. Vehicle and person HEFs are intentionally excluded because they require different JSON/postprocess pairings and must run only through their purpose-specific pipelines.

### Install Or Copy The Resources

Preferred installation:

1. Download a mutually compatible Hailo-8 HailoRT/TAPPAS package set from the [Hailo Developer Zone](https://hailo.ai/developer-zone/).
2. Install HailoRT, its PCIe driver, and TAPPAS 3.31.0 by following the vendor instructions. Allow the TAPPAS installer/downloader to install its example resources rather than downloading arbitrary HEFs individually.
3. Set `TAPPAS_WORKSPACE` to the installed workspace root. `/opt/hailo/tappas` is this project's example runtime path, not a mandatory Hailo installation path.
4. Copy the downloaded resources into this project's `models/hailo/` tree with the commands below.

From the TowerSightAI repository root, copy the required resources out of the compatible TAPPAS workspace:

```bash
export PROJECT_ROOT="$PWD"
export TAPPAS_WORKSPACE=/opt/hailo/tappas
export HAILO_MODEL_DIR="$PROJECT_ROOT/models/hailo"

mkdir -p \
  "$HAILO_MODEL_DIR/general" \
  "$HAILO_MODEL_DIR/vehicle_detection/configs" \
  "$HAILO_MODEL_DIR/person_presence/configs" \
  "$HAILO_MODEL_DIR/postprocess/cropping_algorithms"

cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/resources/hef/yolov5m_wo_spp_60p.hef" \
  "$HAILO_MODEL_DIR/general/"

cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/general/license_plate_recognition/resources/yolov5m_vehicles.hef" \
  "$HAILO_MODEL_DIR/vehicle_detection/"
cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/general/license_plate_recognition/resources/configs/yolov5_vehicle_detection.json" \
  "$HAILO_MODEL_DIR/vehicle_detection/configs/"

cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/general/multi_person_multi_camera_tracking/resources/yolov5s_personface_reid.hef" \
  "$HAILO_MODEL_DIR/person_presence/"
cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/general/multi_person_multi_camera_tracking/resources/configs/yolov5_personface.json" \
  "$HAILO_MODEL_DIR/person_presence/configs/"

cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/libs/post_processes/libyolo_hailortpp_post.so" \
  "$HAILO_MODEL_DIR/postprocess/"
cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/libs/post_processes/libyolo_post.so" \
  "$HAILO_MODEL_DIR/postprocess/"
cp "$TAPPAS_WORKSPACE/apps/h8/gstreamer/libs/post_processes/cropping_algorithms/libwhole_buffer.so" \
  "$HAILO_MODEL_DIR/postprocess/cropping_algorithms/"
```

If the target computer cannot download the TAPPAS resources, run the same copy from another computer with the same Hailo-8, HailoRT, and TAPPAS versions, then transfer the completed `models/hailo/` directory to the same location inside the target TowerSightAI checkout. Model binaries and shared libraries under this directory are ignored by Git. Do not mix resources from different TAPPAS releases or commit them to the repository.

Configure `.env` with the actual paths:

```dotenv
TAPPAS_WORKSPACE=/opt/hailo/tappas
HAILO_MODEL_DIR=models/hailo
HAILO_HEF_PATH=${HAILO_MODEL_DIR}/general/yolov5m_wo_spp_60p.hef
HAILO_POSTPROCESS_SO=${HAILO_MODEL_DIR}/postprocess/libyolo_hailortpp_post.so
HAILO_NETWORK_NAME=yolov5
HAILO_VEHICLE_DETECTION_HEF_PATH=${HAILO_MODEL_DIR}/vehicle_detection/yolov5m_vehicles.hef
HAILO_VEHICLE_DETECTION_CONFIG_PATH=${HAILO_MODEL_DIR}/vehicle_detection/configs/yolov5_vehicle_detection.json
HAILO_VEHICLE_DETECTION_POSTPROCESS_SO=${HAILO_MODEL_DIR}/postprocess/libyolo_hailortpp_post.so
HAILO_PERSON_PRESENCE_HEF_PATH=${HAILO_MODEL_DIR}/person_presence/yolov5s_personface_reid.hef
HAILO_PERSON_PRESENCE_CONFIG_PATH=${HAILO_MODEL_DIR}/person_presence/configs/yolov5_personface.json
HAILO_PERSON_PRESENCE_POSTPROCESS_SO=${HAILO_MODEL_DIR}/postprocess/libyolo_post.so
HAILO_PERSON_PRESENCE_CROP_SO=${HAILO_MODEL_DIR}/postprocess/cropping_algorithms/libwhole_buffer.so
FAST_ALPR_DETECTOR_MODEL=yolo-v9-t-384-license-plate-end2end
FAST_ALPR_OCR_MODEL=cct-xs-v2-global-model
```

### FastALPR Models

`번호판 이미지 LPR` does **not** use the TAPPAS `tiny_yolov4_license_plates.hef` or `lprnet.hef` pipeline. The current implementation uses [FastALPR](https://github.com/ankandrew/fast-alpr) on the CPU with:

- Detector: `yolo-v9-t-384-license-plate-end2end`
- OCR: `cct-xs-v2-global-model`

Installing this project with `python -m pip install -e ".[ui]"` installs `fast-alpr[onnx]`. `FAST_ALPR_DETECTOR_MODEL` and `FAST_ALPR_OCR_MODEL` select the two active FastALPR models. FastALPR prepares its ONNX models when it is first initialized, so the first `번호판 이미지 LPR` run may require internet access. On an offline deployment, initialize FastALPR once while online under the same deployment user and Python environment, then preserve that user's resulting model cache. This path is independent of `TAPPAS_WORKSPACE`.

### Verify The Installation

Run these commands inside the TowerSightAI virtual environment on the Ubuntu/Hailo target:

```bash
source .venv/bin/activate
export TAPPAS_WORKSPACE=/opt/hailo/tappas
export HAILO_MODEL_DIR="$PWD/models/hailo"

hailortcli fw-control identify

for element in hailonet hailofilter hailopython hailoroundrobin hailostreamrouter; do
  gst-inspect-1.0 "$element" >/dev/null || {
    echo "Missing GStreamer element: $element" >&2
    exit 1
  }
done

required_files=(
  "$HAILO_MODEL_DIR/general/yolov5m_wo_spp_60p.hef"
  "$HAILO_MODEL_DIR/postprocess/libyolo_hailortpp_post.so"
  "$HAILO_MODEL_DIR/vehicle_detection/yolov5m_vehicles.hef"
  "$HAILO_MODEL_DIR/vehicle_detection/configs/yolov5_vehicle_detection.json"
  "$HAILO_MODEL_DIR/person_presence/yolov5s_personface_reid.hef"
  "$HAILO_MODEL_DIR/person_presence/configs/yolov5_personface.json"
  "$HAILO_MODEL_DIR/postprocess/libyolo_post.so"
  "$HAILO_MODEL_DIR/postprocess/cropping_algorithms/libwhole_buffer.so"
)

for file in "${required_files[@]}"; do
  test -f "$file" || {
    echo "Missing Hailo resource: $file" >&2
    exit 1
  }
done

towersightai-check-settings --env .env --check-hailo
```

`towersightai-check-settings --check-hailo` checks the Hailo device, required GStreamer elements, and every active general, vehicle, and person Hailo HEF/config/postprocess path. The explicit `required_files` loop remains useful as a copy-time check before launching the application.

After the checks pass, run the hardware image smoke test:

```bash
RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke \
  --env .env \
  --image data/samples/test-car.png \
  --check-installation \
  --run
```

These checks validate installation and inference wiring only. They never authorize PLC OK. If an AI button still fails, inspect:

- `artifacts/runtime/detections/` for previous/general AI detection.
- `artifacts/runtime/purpose-ai/vehicle_detection/vehicle.gst.log` for vehicle-only detection.
- `artifacts/runtime/purpose-ai/person_presence/person_presence.gst.log` for person-presence detection.
- `artifacts/runtime/purpose-ai/lpr_image/lpr.gst.log` for FastALPR image LPR.

## Environment

Create a site-local `.env` from the placeholder file:

```bash
cp .env.example .env
```

Important values:

- `TAPPAS_WORKSPACE`: TAPPAS workspace path.
- `HAILO_MODEL_DIR`: project-local Hailo resource root; defaults to `models/hailo`.
- `HAILO_HEF_PATH`, `HAILO_POSTPROCESS_SO`, `HAILO_NETWORK_NAME`: previous/general AI model mapping.
- `HAILO_VEHICLE_DETECTION_*`: vehicle-only HEF, JSON config, and postprocess mapping.
- `HAILO_PERSON_PRESENCE_*`: person-presence HEF, JSON config, postprocess, and crop helper mapping.
- `FAST_ALPR_DETECTOR_MODEL`, `FAST_ALPR_OCR_MODEL`: CPU-side plate detector and OCR model selection.
- `HAILO_NETWORK_NAME`: defaults to `yolov5`.
- `CAMERA_1_*` through `CAMERA_4_*`: camera ID, role, RTSP URL, optional username/password, and `CAMERA_N_ROTATION_DEGREES`.
- `CALIBRATION_PATH`: calibration JSON path.
- `PLC_ENDPOINT`: PLC or simulator endpoint.
- `UI_CAMERA_RESOLUTION`: preview/display capture resolution, for example `1920x1080` or `1280x720`.

Camera rotation is part of the equipment configuration. Set `CAMERA_N_ROTATION_DEGREES` to one of `0`, `90`, `180`, or `270`; `90` means CCW 90 degrees and `270` means CW 90 degrees. The default site profile uses `CAMERA_1_ROTATION_DEGREES=90` for the ceiling birdview camera and `0` for the other cameras. The operator UI rotation buttons update the runtime value, and AI inference uses the same rotated stream that the operator sees.

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
- `이전 AI Detection` starts the previous working multistream launch path for regression isolation.
- `차량 전용 검출` runs the Hailo LPR example vehicle detector (`yolov5m_vehicles`) on the front camera.
- `번호판 이미지 LPR` runs the Hailo LPR example models against sanitized images in `tmp/car_number-test`.
- `사람 존재 감지` runs the TAPPAS person detector on currently streaming cameras.
- `차량 진입 시뮬레이션` is UI-only and keeps PLC OK blocked.
- `EMPTY` buttons are safe no-op feature slots.

Disconnected cameras remain visible as NG tiles and are not used as inference targets. Currently connected streams are selected from runtime camera status, not from static `.env` presence.

## Purpose AI Checks

Purpose-specific AI buttons use fixed TAPPAS example model sets and always keep PLC OK blocked:

- `차량 전용 검출`: `${HAILO_MODEL_DIR}/vehicle_detection/yolov5m_vehicles.hef`.
- `번호판 이미지 LPR`: CPU-side FastALPR with `yolo-v9-t-384-license-plate-end2end` and `cct-xs-v2-global-model`; it does not use the legacy TAPPAS LPR HEFs.
- `사람 존재 감지`: `${HAILO_MODEL_DIR}/person_presence/yolov5s_personface_reid.hef` with `yolov5_personface_letterbox`; it does not run Re-ID embedding, gallery matching, or same-person tracking.

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
4. Click `사람 존재 감지` and confirm the status strip shows the purpose AI task.
5. Click `차량 진입 시뮬레이션` and confirm it is visibly test-only.
6. Click an `EMPTY` button and confirm it only shows a not-connected message.
7. Confirm simulation and `EMPTY` actions do not show final OK.

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
