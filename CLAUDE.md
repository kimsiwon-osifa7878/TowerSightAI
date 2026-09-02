# CLAUDE.md

TowerSightAI is a safety-first AI monitoring system for a car parking machine (주차기). Four Tapo-C310 RTSP
cameras plus a Hailo-8 M.2 accelerator on an Ubuntu edge device watch vehicle entry, plate recognition,
parking alignment, person presence, and in-vehicle occupancy, and only signal OK to a PLC when every required
safety condition is proven.

The repository is an **implementation prototype**, not a production safety release. Final PLC OK is still
blocked by design.

---

## 1. Read First

Source-of-truth documents, in priority order:

| File | What it fixes |
|---|---|
| `AGENTS.md` | Agent rules, architecture boundaries, UI verification checklist |
| `docs/주차기_AI_안전감시_시스템_설계안.md` | Product/behavior spec: flow, states, PLC payloads, safety principles |
| `DESIGN.md` | Driver (user-mode) display design contract, color tokens, per-state layouts |
| `PLAN.md` | Current UI-first work queue |
| `README.md` | Operator manual: install, commands, `.env` keys, raw/NAS archive, troubleshooting |
| `docs/implementation/*.md` | Per-area guides (architecture, camera/config, hailo, ai-stages, ui/calibration, testing, roadmap) |
| `docs/hailo8-ubuntu-installation.md` | Verified Ubuntu 24.04 + Hailo-8 install path (Korean) |

`refers/` is hardware-tested reference code (legacy TAPPAS `hailopython` experiments, LD2410 experiments,
`multi_stream_detection_rtsp.sh`). **Do not edit it** unless explicitly asked, and never copy its hardcoded
RTSP URLs, credentials, or host paths into product code.

### Known-stale documentation (verify against code before trusting)

- `docs/implementation/camera-and-config.md` and `models/hailo/README.md` still show the legacy
  `models/hailo/**` YOLOv5 layout (`yolov5m_vehicles.hef`, `yolov5s_personface_reid.hef`, JSON configs, crop
  `.so`). **That path is dead.** The live stack is Hailo Apps + `yolov8m.hef` with label filtering — see
  `.env.example` and `docs/implementation/hailo-gstreamer.md`.
- `docs/implementation/testing-strategy.md` manual checklist still names the legacy HEFs in the expected log
  content.
- `README.md` claims "51 passed". Actual current result is **182 passed** (`pytest -q`, ~2 s).

---

## 2. Non-Negotiable Safety Rules

These override convenience, refactors, and UI polish.

- **Default to NG.** Low confidence, missing/stale frames, unknown PLC state, invalid or missing calibration,
  Hailo failure, disabled birdview, simulated input, or any possible person/occupant/obstacle → NG, never OK.
- **Final OK requires all of:** vehicle parked inside calibrated bounds and stopped, plate handled, no person
  in the machine, no in-vehicle occupant, no dangerous obstacle, healthy camera streams, healthy inference,
  valid calibration, known PLC state, and CAR-IN still in the correct pre-operation state.
- **CAR-IN closed / machine operating → AI stops** (`AI_STOP`) or returns to standby.
- **Simulation is never authorization.** UI tests, `차량 진입 시뮬레이션`, driver-test panel, fake adapters,
  the HTML prototype, LD2410 console, diagnostics, and NAS sync success must never make
  `can_show_final_ok` true and must never emit real PLC events. Diagnostics default to `safe_to_operate=False`.
- **Never hide uncertainty behind UI success.** Driver text and PLC signals must reflect the conservative state.
- **Never commit secrets.** No real RTSP URLs with credentials, PLC secrets, NAS credentials, `.env`, or local
  Hailo install paths in code, docs, tests, logs, or screenshots. `.env.example` holds placeholders only.
  Logs and printed pipelines must redact credentials (`redact_rtsp`, `redact_sensitive_text`).
- **Product code targets Ubuntu.** Hardware tests are opt-in (`RUN_HARDWARE_TESTS=1`) and skippable.

---

## 3. Repository Layout (as built)

```text
towersightai/
├── config/
│   ├── settings.py            # frozen dataclasses: Settings, CameraConfig, CameraRole, BirdviewMode,
│   │                          #   CameraResolution, RawStorageConfig, LD2410Config + safety validation
│   └── env_loader.py          # .env parsing, ${VAR} expansion, settings_from_mapping, inspect_env
├── camera/
│   ├── pipeline.py            # build_preview_pipeline, rotation filters, redact_rtsp
│   └── preview.py             # health-check pipeline, run_camera_health_check, launch_camera_previews
├── inference/
│   ├── hailo_apps_runtime.py  # ACTIVE: command + env for the Hailo Apps subprocess, VEHICLE/PERSON labels
│   ├── live_detection.py      # LiveDetectionRunner: multistream run, JSONL tail, fatal-log watch, restart
│   ├── purpose_tasks.py       # PurposeInferenceRunner + vehicle / lpr_image / person_presence tasks
│   ├── callback.py            # Hailo buffer callback → normalized JSONL detection events
│   ├── events.py              # DetectionEvent, BoundingBox, normalize_hailo_detection(s)
│   ├── hailo_check.py         # installation/device/GStreamer-element checks
│   ├── image_smoke.py         # single sample-image Hailo pipeline
│   ├── model_discovery.py     # returns only the HEF paired with the configured postprocess
│   └── pipeline.py            # LEGACY TAPPAS hailopython multistream string (tested, not the runtime path)
├── cli/
│   ├── operator_ui.py         # towersightai-operator-ui
│   ├── check_settings.py      # towersightai-check-settings
│   ├── ai_diagnostics.py      # towersightai-ai-diagnostics (read-only evidence collector)
│   ├── hailo_image_smoke.py   # towersightai-hailo-image-smoke
│   ├── raw_data_sync.py       # towersightai-sync-raw-data
│   ├── hailo_apps_detection.py# runs INSIDE the Hailo Apps venv (not the project venv)
│   ├── fast_alpr_lpr.py       # CPU FastALPR ONNX plate detection + OCR
│   └── event_video_recorder.py# H.264 passthrough MKV segment recorder subprocess
├── ui/
│   ├── pyqt_app.py            # OperatorWindow + camera/detection/purpose/LPR QThread workers (~2.8k lines)
│   ├── driver_view.py         # DriverView, OperatorEntryHotspot (2 s hold), driver stylesheet
│   └── model.py               # OperatorDisplayModel / DriverDisplayModel + the safety gate
├── state_machine/core.py      # ParkingState enum + ALLOWED transition map
├── plc/adapter.py             # PLCAdapter Protocol, FakePLCAdapter, SimulatorPLCAdapter
├── storage/
│   ├── raw_data.py            # RawDataManager, PersonWindowSampler, schema v2 JSONL records
│   ├── hourly_writer.py       # bounded hourly shards + atomic gzip publication
│   ├── evidence.py            # EvidenceCoordinator: JPEG snapshots + MKV clips for real events
│   ├── archive.py             # manifest v2 (SHA-256 per file) + Synology SFTP atomic upload
│   └── connection_test.py     # operator NAS write check into <folder>/connectiontest/ (diagnostic only)
├── sensors/ld2410.py          # LD2410 binary frame parser, ring buffer, one-client TCP service
├── diagnostics.py             # DiagnosticsService: settings/hailo/image/camera/plc/full smoke
└── runtime_logging.py         # runtime log config, credential redaction, run IDs, run-status files

tests/          # 182 hardware-free unit/UI/fake-data tests
tools/          # verify_operator_ui_screenshot.sh, verify_operator_ui_rotation.py
data/samples/   # sanitized sample images (test-car.png)
docs/design/    # towersightai-ui-prototype.html (approved visual contract)
artifacts/      # runtime logs, detections, raw JSONL/media  (gitignored)
models/, tmp/   # gitignored
```

There is **no `ai_stages/` or `calibration/` module yet** even though the docs describe both.

---

## 4. Safety Gate — where it actually lives

Today the gate is a property on the UI model, not a domain service:

`towersightai/ui/model.py` → `OperatorDisplayModel.can_show_final_ok` requires
`state is READY_FOR_OPERATION` **and** `safety_status is READY` **and** `plc_state is CONNECTED` **and**
`hailo_healthy` **and** `calibration_valid` **and** `birdview_available` **and**
`not human_possible/occupant_possible/obstacle_possible` **and** no blocked camera tile.

`_safety_status_for_state()` returns `STOPPED` for `AI_STOP`, then `NG` for any blocked tile, disabled
birdview, non-`CONNECTED` PLC, unhealthy Hailo, invalid calibration, or any possible person/occupant/obstacle;
`READY` only in `READY_FOR_OPERATION`; otherwise `WAIT`.

`build_driver_display()` additionally forces `can_show_final_ok = False` and a red `DANGER`/`TEST` tone when
input is simulated, a required camera role is blocked, or birdview is off during `ALIGNMENT_GUIDE`.

**When adding stage logic, do not add a second gate.** PLAN item 7 is to centralize these prerequisites in one
safety-gate object that both the UI and the PLC path consume. Preserve every existing condition when you move it.

### State machine

`state_machine/core.py` only enforces legal transitions (linear `IDLE → … → READY_FOR_OPERATION → AI_STOP`,
plus `SAFETY_CHECK ↔ HUMAN_DETECTED`); illegal transitions raise `ValueError`. It carries no evidence, no
timers, and no gating yet. Public PLC/UI states must always map back to the ten design-document names.

### PLC

Only `FakePLCAdapter` and `SimulatorPLCAdapter` exist. The real protocol is unconfirmed; implement it behind
the `PLCAdapter` Protocol and keep event ordering testable (`vehicle_parked`, `human_detected`, `human_clear`,
`in_vehicle_occupancy_check`, `safety_check_complete`, `safety_status_ng`, `ai_stopped`).

---

## 5. Configuration

`.env` (site-local, gitignored) → `load_settings_from_env()` → `settings_from_mapping()` → `Settings`.
`.env` supports `${VAR}` expansion against earlier keys. `Settings` is the single source of truth for camera
URLs/roles/credentials/rotation, Hailo Apps paths, model paths, thresholds, PLC endpoint, UI mode, calibration
path, raw-storage, and LD2410.

Validation that intentionally fails fast:

- All four `CameraRole` values (`ceiling`, `front`, `rear_side`, `opposite_side`) must be present with unique IDs.
- `HAILO_ARCH` ∈ `{hailo8, hailo8l}`; rotation ∈ `{0, 90, 180, 270}` (90 = CCW, 270 = CW).
- `APP_ENV=production` requires `CALIBRATION_PATH` to exist.
- `LD2410_TCP_ENABLED=true` requires `RAW_DATA_ENABLED=true` (raw-audit-only integration).
- `RAW_DATA_ENABLED=true` requires all `SYNOLOGY_NAS_*` values; host must be a bare hostname (no scheme/port/path).
- `RAW_DATA_SHARD_MINUTES` must divide 60.

`BIRDVIEW_MODE`: `ceiling` (default when absent) or `disabled`. `disabled` drops the ceiling camera from
`Settings.active_cameras`, hides its UI surfaces, and **permanently blocks final OK** (`버드뷰 OFF`).
The current site profile uses `BIRDVIEW_MODE=disabled` with `CAMERA_1_ROTATION_DEGREES=270`.
Any new birdview mode must be a validated enum value — unknown values must fail config, never imply a
synthetic birdview.

---

## 6. Hailo / GStreamer runtime

Pinned stack for Hailo-8: **HailoRT 4.23.0 + TAPPAS Core 5.1.0 + Hailo Apps release 26.03.1**, Python 3.12
bindings. HailoRT 5.x is Hailo-10H — do not install it here.

Active pattern (`docs/implementation/hailo-gstreamer.md`):

```text
RTSP sources → per-source stream ID → hailoroundrobin (non-blocking) → hailonet → hailofilter
  → Hailo Apps Python buffer callback → hailostreamrouter/headless sinks → TowerSightAI JSONL events
```

Key facts:

- Inference runs **out of process**: the parent (project `.venv`) spawns `HAILO_APPS_PYTHON -m
  towersightai.cli.hailo_apps_detection` with `PYTHONPATH` containing the project root and the Hailo Apps
  workspace (`hailo_apps_runtime_env`). `hailopython` is not used; the callback attaches to an `identity` pad.
- Default resources: `~/hailo-apps/resources/models/hailo8/yolov8m.hef`,
  `libyolo_hailortpp_postprocess.so`, network/function name `filter_letterbox`.
  Vehicle task filters `car/truck/bus/motorcycle`; person task filters `person` only.
  **No Re-ID, no gallery matching, no identity tracking** — the safety question is person *existence*.
- Plate recognition is a **separate CPU path**: FastALPR ONNX (`yolo-v9-t-384-license-plate-end2end` +
  `cct-xs-v2-global-model`), not the TAPPAS LPR HEFs.
- The callback maps `roi.get_stream_id()` (`src_N`) back to camera IDs and rotates bounding boxes to the
  UI orientation. Raw Hailo objects never reach the state machine.
- Bounding boxes are corrected from YOLO 640×640 letterbox space back to source resolution before drawing.
- Runners watch stderr for fatal patterns (`HAILO_OUT_OF_PHYSICAL_DEVICES`, `Failed to create vdevice`,
  `CHECK_SUCCESS failed`, `Caught SIGSEGV`, `HAILO_HEF_NOT_SUPPORTED`, missing `hailo*` elements) and kill the
  process group instead of leaving the UI spinning.
- Heartbeats are written at RTSP/queue/roundrobin/hailonet/postprocess/callback boundaries. **Detection counts
  are not frame-health counters** — never use them to decide a camera is alive. Stale post-inference output for
  a required camera, or no first heartbeat within 30 s, terminates the whole child and restarts it (max 3
  consecutive attempts, counter resets after 60 s healthy; exhaustion → `failed`). Recovery keeps NG.
- `qos=false` on Hailo Python/postprocess elements unless measured otherwise.
- `run.sh` deliberately unsets `GST_PLUGIN_PATH`/`LD_LIBRARY_PATH` and uses a private `GST_REGISTRY` so a
  legacy `/opt` TAPPAS stack cannot leak into the verified 5.1 runtime.

Runtime evidence lives under `artifacts/runtime/detections/` and
`artifacts/runtime/purpose-ai/{vehicle_detection,person_presence,lpr_image,front_camera_lpr}/`.

---

## 7. UI

One PyQt6 application, two surfaces in a `QStackedWidget`:

- **User mode (default entry)** — driver-facing, near-black edge-to-edge camera canvas for a 50"+ display
  viewed from ~6 m. One short action in a 50 %-transparent top overlay (`진입`, `정지`, `오른쪽 이동`,
  `왼쪽 이동`, `전진`, `후진`, `주차기 밖으로 이동`), compact bottom status strip. No dev buttons, model
  names, paths, or inference counters. Camera priority follows state: front while idle → front + ceiling
  birdview through entry/alignment/safety.
- **Operator mode** — the developer console: a sectioned scrollable sidebar (`SIDEBAR_SECTIONS` in
  `ui/pyqt_app.py`: 운영 / 진단 / 시스템) navigating a `QStackedWidget` of workspace pages. Entered via the
  visible bottom-right `운영자 모드` button (the on-site entry point), the invisible 72×72 px top-right hotspot
  held for 2 s (early release or pointer exit cancels), or `Ctrl+Shift+O`.

The operator visual system is the approved "패널 HMI" proposal (`docs/design/operator-console-proposals.html`
B안): panel surfaces `#151B24`/`#232C39` radius 12, amber accent `#F5A623` (`primary="true"` run buttons,
checked nav), instrument camera tiles drawn in `CameraSurface.paintEvent` only for `contain` mode — the
driver view (`cover`) stays chromeless cyan/navy.

Workspace pages: `전체 카메라` (landing; camera grid + `이전 AI Detection` toggle), `차량 감지`, `사람 감지`,
`번호판 인식` (정면 카메라 인식 + 이미지 LPR), `레이더 (LD2410)`, `NAS 연결 확인` (`storage/connection_test.py`),
`시스템 점검` (DiagnosticsService off-thread), `실행 로그` (runtime log tail + filter), `주차 프로세스 테스트`
(driver-stage playback + `차량 진입 시뮬레이션`). Camera pages share ONE camera grid
(`operator_camera_area`) that `_adopt_camera_area` reparents into the active page with an `all` or `front`
layout. Task run/stop buttons live on the pages, not in the sidebar; `프로그램 종료` sits behind
`_confirm_shutdown()`. None of these controls touch safety state or PLC output.

Threading: `CameraCaptureWorker`, `LiveDetectionWorker`, `PurposeInferenceWorker`, `FrontCameraLprWorker` each
run on a `QThread` and communicate by signals. OpenCV and PyQt imports are lazy so headless tests still run.
Detection overlays expire after `DETECTION_TTL_SECONDS`; first inference must appear within
`FIRST_INFERENCE_TIMEOUT_SECONDS`.

Display rules that tests enforce: camera tiles must not resize when long status text appears; disconnected
active cameras stay visible as NG tiles and are excluded from inference targets; the birdview tile draws no
default lane/stop guides outside calibration mode; error/NG states can never use final-OK styling.

`docs/design/towersightai-ui-prototype.html` is the approved visual contract for user mode.

---

## 8. Raw data, evidence, and NAS archive (audit-only)

- `RAW_DATA_ENABLED=true` appends schema-v2 JSONL to bounded shards
  `artifacts/raw/YYYY-MM-DD/events-YYYYMMDD-HHMM.jsonl`; closed shards are atomically published as `.jsonl.gz`.
  Records: application/AI start-stop, vehicle entry, plate results, raw per-camera detections, LD2410 status,
  and 0.5 s `person_sample` rows continuing 5 s past clear.
- `RAW_MEDIA_ENABLED=true` captures JPEG snapshots and H.264-**passthrough** silent MKV clips (5 s pre-roll,
  10 s vehicle post-roll, 5-minute clip parts) for **real** events only. Media bytes never enter JSONL —
  `media_artifact_created` stores relative path, size, SHA-256, capture time, metadata. Failures are explicit
  `media_capture_failed` events.
- `storage/archive.py` uploads completed days to `${SYNOLOGY_NAS_FOLDER}/raw/YYYY-MM-DD/` over strict-host-key
  SFTP: per-file SHA-256 manifest v2, `.part` upload → verify → atomic rename, manifest published last.
  Local days are deleted only after a verified upload and 14 days.
- `LD2410_TCP_ENABLED=true` accepts **one** ESP32 client sending raw LD2410 frames (`F4 F3 F2 F1` header).
  Each `person_sample` embeds the newest frame at or before the sample time: ≤1 s = `fresh`, older buffered =
  `stale`, none = `unavailable`; future frames are never selected.

**All of this is audit/telemetry only.** Archive success, LD2410 values, and media capture must never relax,
authorize, or influence the safety gate, AI, or the state machine.

---

## 9. Commands

```bash
pytest -q                                     # 182 passed, hardware-free
./run.sh                                      # fullscreen operator UI (uses .venv + .env)
./run-window.sh                               # windowed
towersightai-operator-ui --env .env --windowed
LOG_LEVEL=DEBUG towersightai-operator-ui --env .env    # per-run IDs, resolved paths, PIDs, exit codes
towersightai-check-settings --env .env [--check-hailo|--health-check-cameras|--preview-cameras --dry-run]
towersightai-ai-diagnostics --env .env --output artifacts/runtime/ai-diagnostics.txt
towersightai-sync-raw-data --env .env [--include-current-day]
RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke --env .env \
  --image data/samples/test-car.png --check-installation --run
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```

Install: `python -m pip install -e ".[ui]" pytest` (Python ≥ 3.11; the repo `.venv` is 3.12).
`towersightai-ai-diagnostics` starts no camera, pipeline, or inference — it only reads existing run-status
files, JSONL, log tails, and FastALPR cache metadata, and reports missing files as `missing`.

---

## 10. How to work in this repo

**UI-first order** (from `PLAN.md` and `docs/implementation/ui-and-calibration.md`):

1. Add the operator UI button / status row / panel / test slot.
2. Wire it to fake, diagnostic, or simulation behavior that cannot change final OK.
3. Add UI and fake-data tests.
4. Only then connect real camera, Hailo, calibration, AI-stage, or PLC logic.
5. Keep the result auditable from the test screen or diagnostics log.

**Module boundaries** — camera ingest owns RTSP/GStreamer health; inference owns HEF/postprocess and emits
normalized events; AI-stage logic owns vehicle/plate/alignment/person/obstacle/occupancy decisions; the state
machine owns legal transitions and conservative gating; the PLC adapter owns external comms and stays
mockable; the UI owns display, settings, preview, calibration interaction.

**Every AI stage returns one of `PASS` / `WAIT` / `RETRY` / `NG` / `ERROR`** and must be deterministic and
unit-testable from synthetic events. Stale frames, invalid calibration, and low confidence can never yield `PASS`.

**Tests are mandatory with each feature.** Cover normal, NG, and uncertain cases; every path that can affect
PLC OK/NG needs success, failure, and uncertainty tests. Unit/UI tests must run without RTSP cameras, Hailo-8,
or a PLC. Hardware tests must be explicitly marked and skippable. Fixtures stay sanitized.

**UI-centered changes** (layout, buttons, status text, camera surfaces, overlays, sidebar, diagnostics) also
require a real GUI run when an Ubuntu desktop session is available: run `pytest -q`, launch windowed, capture
the dashboard and sidebar-open screenshots, exercise `전체 카메라` / `주차 프로세스 테스트` /
`차량 진입 시뮬레이션` / `레이더 (LD2410)`, and confirm final OK stays blocked. Store screenshots under
`tmp/operator-ui-verification/` and never commit them. If no GUI is available, say so explicitly and name the
blocker (no display session, missing `xdotool`/`gnome-screenshot`, no PyQt/OpenCV runtime). Screenshot
verification is implementation verification only — never product safety approval.

---

## 11. Status and next work

Implemented: typed config + `.env` loading and validation; RTSP preview pipelines with rotation and redaction;
PyQt6 user/operator surfaces with the 2 s hidden operator gesture; runtime camera capture and NG tiles; Hailo
installation checks and sample-image smoke; Hailo Apps multistream live detection with JSONL normalization,
overlays, letterbox bbox correction, fatal-log handling and supervised restart; purpose AI tasks (vehicle,
person-presence, image LPR, front-camera LPR); LD2410 TCP console; hourly raw JSONL + media evidence +
verified Synology SFTP archive with retention; fake/simulator PLC adapters and the transition-only state machine.

Open gaps (roughly `PLAN.md` order):

1. Centralize the final-OK prerequisites in one safety gate shared by UI and PLC paths.
2. Stage AI decisions: alignment/parking-position, plate handling, person + obstacle fusion across healthy
   cameras, in-vehicle occupancy (all behind interfaces).
3. Calibration workflow — no module or UI yet: per-camera lane centerline, boundaries, stop zone, danger/cabin
   ROIs, tolerances, versioned JSON with normalized coordinates, explicit review/activation; missing, invalid,
   or unreviewed calibration blocks final OK.
4. Real PLC adapter behind the existing boundary, with event-ordering tests.
5. Per-camera preview-health vs inference-health separation, last-detection timestamps, `AI stale` /
   `AI no events` display, GStreamer stderr tail in the UI log.
6. Stage simulation / fake event playback controls, clearly marked test-only.
7. Field hardening: watchdogs, deployment runbook, log rotation, structured safety audit traces, site
   acceptance checklist driven from the operator test hub.

Outbound flow (driver approach, vehicle movement, exit-complete confirmation) is a **future concept** — it has
no state, AI, or PLC contract yet and must not be inferred from the UI or the ParkIO reference material.

---

## 12. Gotchas

- Two virtualenvs matter: the project `.venv` (UI, tests) and the Hailo Apps venv (`HAILO_APPS_PYTHON`) that
  actually executes `towersightai.cli.hailo_apps_detection`. Import errors there usually mean `PYTHONPATH`,
  not a code bug.
- `towersightai/inference/pipeline.py` still builds the legacy TAPPAS `hailopython` string and is still under
  test — it is **not** the runtime path. Do not "fix" the live pipeline by editing it.
- `Settings` requires all four cameras even when `BIRDVIEW_MODE=disabled`; use `active_cameras`, not `cameras`,
  when selecting capture/inference targets.
- `SimulatorPLCAdapter.event_names` has unreachable code after its `return`; clean it up if you touch that file.
- Tapo cameras allow only two concurrent RTSP sessions per stream. Preview and inference both hold
  `stream1`, so `RAW_MEDIA_ENABLED=true` requires `CAMERA_N_RECORD_RTSP_URL=...stream2` — an empty record
  URL makes the evidence recorder take the second `stream1` slot and the inference session dies with
  `Bad Request (400)`. `PurposeInferenceRunner` retries transient child exits (non-zero, non-fatal) up to
  `max_consecutive_restarts`, but a persistent three-session conflict is a configuration error.
- pip `opencv-python` wheels are built without GStreamer, so `cv2.VideoCapture(..., CAP_GSTREAMER)` never
  opens on such installs. `CameraCaptureWorker._open_capture` falls back to a direct FFmpeg RTSP capture for
  every active camera (rotation applied in software); capture state transitions are logged under
  `towersightai.camera.capture`. `check-settings --health-check-cameras` uses the system `gst-launch-1.0`
  subprocess, so it can pass even when the in-process GStreamer backend is unavailable.
- `artifacts/`, `models/`, `tmp/`, `gstshark_*/`, `hailort*.log`, and `.env` are gitignored — never add them.
- Korean UI strings are part of the contract; keep the exact labels tests assert on.
