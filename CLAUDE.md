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
| `INTENT.md` | Agreed working style with the user, decision rationale, field history, open items |
| `AGENTS.md` | Agent rules, architecture boundaries, UI verification checklist |
| `docs/주차기_AI_안전감시_시스템_설계안.md` | Product/behavior spec: flow, states, PLC payloads, safety principles |
| `DESIGN.md` | Driver (user-mode) display design contract, color tokens, per-state layouts |
| `PLAN.md` | Current UI-first work queue |
| `README.md` | Usage-focused operator manual (Korean): install, run, console pages, troubleshooting |
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
- `docs/implementation/testing-strategy.md` manual checklist still names the legacy HEFs
  (`yolov5m_vehicles.hef`, `yolov5s_personface_reid.hef`) in the expected log content — the runtime uses
  `yolov8m.hef` with label filtering.
- Current suite size: **489 passed** (`pytest -q`, hardware-free). Update this figure when it drifts.

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
- **The LD2410 radar is verification data, not a safety input.** It is recorded to raw data for the offline
  camera-vs-radar accuracy study only. It must never reach the process engine, user mode, the safety gate, or
  the parking machine's operation; person presence comes from the cameras alone.
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
│   ├── hailo_health.py        # 60s device health: PCIe/driver/node/chip-response/AER RxErr → log + UI
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
│   ├── checkerboard.py        # towersightai-checkerboard: printable calibration board
│   ├── analyze.py             # towersightai-analyze: sites / days / sync / index / serve
│   └── event_video_recorder.py# H.264 passthrough MKV segment recorder subprocess
├── process/
│   ├── engine.py              # ParkingProcessEngine: continuous inbound cycle (pure Python, injected clock)
│   └── settings_store.py      # OperatorRuntimeSettings ⇄ data/operator-settings.json (gitignored)
├── ui/
│   ├── pyqt_app.py            # OperatorWindow, workspace pages, camera/detection/purpose/LPR/NAS/health workers (~4k lines)
│   ├── driver_view.py         # DriverView, OperatorEntryHotspot (2 s hold), driver stylesheet
│   ├── audio.py               # AudioAlertPlayer: warning tone, silent degrade without QtMultimedia
│   └── model.py               # OperatorDisplayModel / DriverDisplayModel + the safety gate + copy overrides
├── state_machine/core.py      # ParkingState enum + ALLOWED transition map (cyclic since the process engine)
├── plc/adapter.py             # PLCAdapter Protocol, FakePLCAdapter, SimulatorPLCAdapter
├── storage/
│   ├── raw_data.py            # RawDataManager, PersonWindowSampler, schema v2 JSONL records
│   ├── hourly_writer.py       # bounded hourly shards + atomic gzip publication
│   ├── evidence.py            # EvidenceCoordinator: JPEG snapshots + MKV clips for real events
│   ├── archive.py             # manifest v2 (SHA-256 per file) + Synology SFTP atomic upload
│   ├── connection_test.py     # operator NAS write check into <folder>/connectiontest/ (diagnostic only)
│   ├── file_transfer.py       # operator file relay into <folder>/transfer/ (SHA-256 verified, relay only)
│   └── hailo_incident.py      # Hailo failure evidence bundle → <folder>/hailo-incidents/ (read-only)
├── calibration/
│   ├── checkerboard.py        # CheckerboardSpec + printable PDF/SVG/PNG (no external deps)
│   ├── intrinsics.py          # CAPTURE_POSES, detect_checkerboard, calibrate_intrinsics, IntrinsicsSessionStore
│   ├── ground.py              # extrinsics: 4 clicked pallet corners + stopper landmark → camera pose
│   └── share.py               # publish/fetch calibration results through the NAS (bench → site)
├── sensors/ld2410.py          # LD2410 binary frame parser, ring buffer, one-client TCP service
├── analyze/                   # OFFLINE analysis dashboard (dev/verification only, never runs on site):
│   ├── config.py              #   sites.json (per-site NAS address), AnalysisPaths under data/analysis/
│   ├── nas_reader.py          #   read-only SFTP mirror (legacy raw/<day> and raw/<host>/<day>), SHA-256 verified
│   ├── loader.py / digest.py  #   JSONL shards → per-day digest (frames, radar samples, media, coverage)
│   ├── episodes.py            #   camera/radar episode replay (engine-equivalent rules) + pairing
│   ├── labels.py / metrics.py #   reviewer labels (append-only JSONL), precision / mutual recall / sweeps
│   ├── server.py              #   stdlib HTTP JSON API + static SPA (127.0.0.1 only)
│   └── static/                #   index.html, app.css, app.js (no external deps)
├── diagnostics.py             # DiagnosticsService: settings/hailo/image/camera/plc/full smoke
└── runtime_logging.py         # runtime log config, credential redaction, run IDs, run-status files

tests/          # 450 hardware-free unit/UI/fake-data tests (conftest forces QT_QPA_PLATFORM=offscreen)
tools/          # verify_operator_ui_screenshot.sh, verify_operator_ui_rotation.py
*_autorun.sh    # boot autostart: systemd *user* service (GUI needs graphical-session.target)
vehicle_box_test/  # 3D vehicle-box LAB (not a pytest suite, not imported by towersightai/)
data/samples/   # sanitized sample images (test-car.png)
docs/design/    # approved visual contracts (driver prototype, operator console proposals A/B)
artifacts/      # runtime logs, detections, raw JSONL/media  (gitignored)
models/, tmp/   # gitignored
```

There is **no `ai_stages/` module yet**. `calibration/` holds only checkerboard generation and the intrinsics
measurement tooling; there is still no site (extrinsics) calibration, no calibration validity check, and no
calibration UI beyond the 카메라 캘리브레이션 (intrinsics) and 지면 기준점 (extrinsics) measurement pages —
both write measurement files only and neither marks calibration valid. `data/field-media/{front,rear_side,opposite_side}/`
(per-camera folders, gitignored) is where real site photos/videos for the 3D vehicle-box work are collected.

`vehicle_box_test/` is the **3D vehicle-box lab**, not a test suite — `pytest` never runs it and
`towersightai/` never imports it (the owner's rule is: prove the hypothesis in the lab, only then
change the app). It pulls vehicle-bearing media out of the NAS archive read-only, fits a ground
homography from the site's real dimensions (turntable Ø6100, rails 2106, pallet 5350×2200) **without
a checkerboard**, extracts the vehicle silhouette with Lab-space background subtraction (no Hailo),
and writes the verdict into each image — dimensions when it works, a **Korean failure reason** when
it does not — plus a local `out/report.html`. Read `vehicle_box_test/README.md` (how to run) and
`vehicle_box_test/CONTEXT.md` (intent, what the first pass proved and what it blocked on) before
touching it. Its `data/` and `out/` are gitignored and contain **readable licence plates — never
publish or share them**. It needs Pillow (Korean text on images); `towersightai/` does not.

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

`state_machine/core.py` enforces legal transitions and raises `ValueError` on anything else. Since the
process engine, `ALLOWED` is cyclic: the happy path is still `IDLE → … → READY_FOR_OPERATION → AI_STOP`,
but every non-IDLE state carries a conservative abort edge back to `IDLE`, `IDLE → HUMAN_DETECTED` covers
the idle person watch, `READY_FOR_OPERATION → SAFETY_CHECK` covers a person reappearing after OK-send, and
`AI_STOP → IDLE` closes the continuous cycle. Public PLC/UI states must always map back to the ten
design-document names.

### Process engine (the redefined field flow)

`towersightai/process/engine.py` — `ParkingProcessEngine`, the first real consumer of
`SafetyStateMachine`. Pure Python (no Qt), injected clock, driven by `OperatorWindow`: 1 Hz `tick(now)`
plus `observe_detections/observe_radar/observe_lpr_attempt/observe_monitoring_health/observe_camera_health`.
Each tick returns an `EngineOutput` (public state, driver copy key, plate, audio cue, simulated PLC
requests, raw-event requests, LPR loop control, uncertainty reason). The cycle: IDLE person watch on
ceiling+front+rear_side (opposite_side is **excluded** in IDLE — it sees outside the open door) →
opposite_side vehicle trigger (operator-tunable confidence ≥0.6 × ≥5 consecutive frames, release on lost
evidence) → **direction classification** (`entry_classify`, public state stays IDLE so nothing is announced
yet): a plate read, or a front-camera box that is narrow and upright, means 입고; a box that fills the frame
side-on means 출고 (`vehicle_exiting`, public IDLE, driver copy `출고중` only, person watch suppressed —
a person beside a car being retrieved is normal). No plate and no confident shape is an **error**, never an
assumed entry. Measured shapes (구로 신안타워 2026-09-16): exiting w 0.92-1.00 / w/h 3.3-4.4, entering
w ~0.54 / w/h ~1.57; width is primary because a partly visible entering car can show a large aspect ratio.
Retrievals are recorded as `vehicle_exit_started` / `vehicle_exit_ended`, never `vehicle_entered`, so entry
statistics and plate hit rate stay clean → 1 Hz front-camera FastALPR gated by the 차량진입선 / vehicle-entry line near the top (only
plate bboxes **below** it count as "entering" and feed the vote) and majority vote. The read window runs
`read_timeout_seconds` from the **front camera's first sight of the car** (not the opposite_side trigger),
capped by `arrival_timeout_seconds` when the car never arrives, and a stationary car only ends the vote
once a read landed or `min_read_seconds` passed — field data 2026-09-16 recognized 1 of 9 entries because
the vote ended after 1-2 reads or timed out before the car reached the front camera. The winning read
carries its frame path and box so the entry gets `plate_image` + `plate_crop` evidence → front trapezoidal
wheel-guide alignment (wide bottom, narrow top for the front-camera perspective; bbox-stability parked
heuristic, 3D box is future work) →
parked instruct → 10 s no-person countdown → simulated `vehicle_parked`+plate via `FakePLCAdapter` →
60 s machine-operation window (person watch now includes opposite_side; warns, never aborts) → IDLE.
Outbound (출고) has no PLC contract or AI stage; since 2026-09-16 the engine only *recognizes* a retrieval so it is not mistaken for an entry. Rules the tests pin: every PLC payload carries
`simulated: True`; uncertainty (monitoring dead, front/rear_side camera NG) aborts any entry back to IDLE
with a `vehicle_session_end`; the **LD2410 radar is not an engine input at all** (owner decision
2026-09-16: verification-only sensor, recorded to raw data for the offline camera-vs-radar study, never
reaching the engine, the driver display, or any operating/safety decision — person presence is cameras
only); the engine never computes an OK — `can_show_final_ok` stays the only gate.

Hosting facts: the engine's inference is the combined `process_monitoring` purpose task (one Hailo child,
person+vehicle labels, child min-confidence fixed at 0.2 — operator thresholds are applied in the parent, so
settings changes never restart the child and the Tapo RTSP session budget holds). It auto-starts when any
camera streams, pauses via the existing pending-task auto-switch when a manual test task starts, and resumes
(10 s cooldown) when it stops; the 감시 설정 page's `프로세스 감시 일시중지` disables it. Operator-tunable
values live in `data/operator-settings.json` (`process/settings_store.py`, atomic writes, corrupt file →
defaults + warning) — deliberately **not** `.env`. The 1 Hz plate loop is `PeriodicFrontLprWorker` +
`FastAlprSession` (persistent CPU ONNX model over preview frames; no extra RTSP session). Driver copy for
engine phases comes from `DRIVER_COPY_OVERRIDES` in `ui/model.py` via `build_driver_display(copy_key=...)`;
the `alignment_front_guide` key suppresses the birdview-off DANGER rule for `ALIGNMENT_GUIDE` because
alignment is front-guide-driven (final OK stays blocked by the gate regardless). Warning audio is
`ui/audio.py` (screen warnings never depend on it).

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

`VEHICLE_BOX_*` (all mm, blank = default): pallet 5350×2200, rail inner width 2106, bay heights 1600/1900,
vehicle limits L5205 × W2000 (2100 with mirrors) × H1550/1850, wheel track ≤2000, plus the side-camera role
mapping (`VEHICLE_BOX_FRONT_LEFT_CAMERA=rear_side`, `VEHICLE_BOX_REAR_RIGHT_CAMERA=opposite_side`). Defaults
come from the 구로 신안타워 approval drawing (`refers/…승인도…pdf` J001); `VehicleEnvelopeConfig` rejects limits
that exceed the bay and non-side or duplicate camera roles. This is the site geometry for the planned 3D
vehicle-box stage (INTENT.md §4 확정안) — **no estimation code exists in `towersightai/` yet**; the
offline experiments live in `vehicle_box_test/` and reuse `VehicleEnvelopeConfig` for the same numbers
(plus the turntable Ø6100 from drawing J002, which has no `.env` key).

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
  `cct-xs-v2-global-model`), not the TAPPAS LPR HEFs. That OCR model has **no Hangul**, so it returns a Latin
  look-alike for the middle character and can shift the digits around it (field 2026-09-17: 213가9135 read as
  `2137I913`). Only the **trailing four digits** are trusted: `FastAlprSession._read_tails` majority-votes the
  tail over the whole-plate text, a padded re-read, and right-hand crops at five cut points (~105 ms on top of
  the ~111 ms detect+read), and a read without four trailing digits is rejected rather than guessed. The whole
  text is kept beside it as `plate_text` for audit only.
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

Workspace pages: `전체 카메라` (landing; camera grid + `사람 감지`/`차량 감지` start buttons that mirror the task pages),
`차량 감지`, `사람 감지`,
`번호판 인식` (정면 카메라 인식 + 이미지 LPR), `레이더 (LD2410)`, `NAS 연결 확인` (`storage/connection_test.py`),
`NAS 파일 전송` (`storage/file_transfer.py`; picked files → `<folder>/transfer/`, remote-access file relay),
`카메라 캘리브레이션` (13-pose checkerboard capture aimed by an **on-screen A4-landscape target box** — the
board must sit inside the box and hold there 3 s (`CALIBRATION_DWELL_SECONDS`) before a frame is kept, so no
left/right wording and no instant snaps — then `cv2.calibrateCamera` → `data/calibration/intrinsics/`;
`결과 확인` re-checks a saved measurement with a Korean quality checklist plus a straight-grid
before/after undistortion image; measurement file only, `reviewed=false`, never marks calibration valid),
`지면 기준점` (**extrinsics**: the operator clicks the four pallet-deck corners and then the base of the
orange stopper frame — that landmark fixes which end is the entry without any left/right wording — and
`calibration/ground.py` undistorts the clicks, tries every corner assignment, keeps the best reprojection
and reports where the camera sits and how it points **relative to the pallet**, with the projected pallet
and a 500 mm grid drawn over the live tile to judge the fit; saved to `data/calibration/ground/<camera>.json`,
`reviewed=false`, `safe_to_operate=false`).
**Sharing between machines**: the checkerboard can only be held in front of a camera on the bench, so the site
device cannot measure intrinsics and the `지면 기준점` page refuses to solve without them. `calibration/share.py`
publishes the *result* JSONs (never the capture sessions) to
`${SYNOLOGY_NAS_FOLDER}/calibration/<source_host>/<kind>/<camera>.json` over the archive's strict-host-key SFTP
with SHA-256 verification and atomic `.part`→rename, and fetches them back on the other machine; `NAS로 공유` /
`NAS에서 가져오기` on the 카메라 캘리브레이션 page drive it off-thread. A fetched file keeps its original
`source_host`, and `reviewed`/`safe_to_operate` are forced false on arrival whatever the file claims — so a bench
measurement is labelled `△ 이 장비가 아니라 …에서 측정한 값` instead of passing as this camera's own. The same
result JSONs are now tracked in git (`data/calibration/intrinsics/sessions/` and `*-verify.png` stay ignored), so
a `git pull` is the second route,
`시스템 점검` (DiagnosticsService off-thread + Hailo 장치 상태 패널), `실행 로그` (runtime log tail + filter), `주차 프로세스 테스트`
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
  and 0.5 s `person_sample` rows continuing 5 s past clear. Plate recording is complete per entry:
  `plate_recognized` carries `recognized`/`reads`/`reason` and is written for 미인식 and aborted votes too,
  and every 1 Hz front-camera read is a `plate_attempt` row (`accepted` plus a rejection reason such as
  `above_entry_line`) so the analysis dashboard can measure LPR hit rate.
- Media filenames carry the **local** date-time plus a zone suffix — `20260916-154634-702684_kr-plate-crop-front.jpg`
  (`_local` when `RAW_DATA_TIMEZONE` is not Asia/Seoul). Until 2026-09-16 the name used UTC while the day
  folder used local time, so a 15:46 KST capture was named `064634` and names did not sort chronologically
  inside a day folder; files uploaded before that change keep the old UTC names.
- `RAW_MEDIA_ENABLED=true` captures JPEG snapshots and H.264-**passthrough** silent MKV clips (5 s pre-roll,
  10 s vehicle post-roll, 5-minute clip parts) for **real** events only. Media bytes never enter JSONL —
  `media_artifact_created` stores relative path, size, SHA-256, capture time, metadata. Failures are explicit
  `media_capture_failed` events.
- `storage/archive.py` uploads completed days to `${SYNOLOGY_NAS_FOLDER}/raw/<source_host>/YYYY-MM-DD/` (per-host
  since 2026-09-10 — dev and field boxes used to overwrite each other's shards in a shared `raw/YYYY-MM-DD/`;
  the analysis reader still reads that legacy layout) over strict-host-key
  SFTP: per-file SHA-256 manifest v2, `.part` upload → verify → atomic rename, manifest published last.
  Local days are deleted only after a verified upload and 14 days.
- `LD2410_TCP_ENABLED=true` accepts **one** ESP32 client sending raw LD2410 frames (`F4 F3 F2 F1` header).
  Each `person_sample` embeds the newest frame at or before the sample time: ≤1 s = `fresh`, older buffered =
  `stale`, none = `unavailable`; future frames are never selected.
- **Hailo failure evidence → NAS** (`storage/hailo_incident.py`, 2026-09-17): when the health monitor turns
  `degraded`/`error`, `HailoIncidentReporter` (run on the health worker thread) collects read-only evidence —
  PCIe link speed + AER counters for endpoint and upstream port, driver/device node, device holders, kernel
  messages, a **frozen tail copy** of the runtime log, the newest inference child log — and uploads it to
  `<SYNOLOGY_NAS_FOLDER>/hailo-incidents/<host>-<UTC stamp>/`. One bundle per transition into a bad status,
  then at most one per `HAILO_INCIDENT_MIN_INTERVAL_SECONDS` (default 1800) while it stays bad. It runs only
  when `RAW_DATA_ENABLED=true` **and** `HAILO_INCIDENT_UPLOAD_ENABLED=true` **and** a NAS host is set, so an
  unconfigured or test host can never make the health thread dial out. Every health snapshot also becomes a
  `hailo_health` row in the daily JSONL (durable; healthy rows thinned to one per 10 min, every bad row kept)
  so the archived day carries the failure timeline. Read-only and diagnostic only: nothing here removes,
  rescans, reloads, kills, or restarts anything, and it never touches the safety gate.
- Radar raw logging (spec `docs/implementation/radar-raw-logging.md`, 2026-09-10): `RawDataManager.tick()` also
  records a 1 Hz `ld2410_sample` (provider snapshot minus `raw_hex`; `fresh` always, `stale` only on a new
  `received_at`, `unavailable` never, provider error once) and drives `RadarWindowTracker`
  (`idle → confirming → open → closing`): present = `fresh` + `target_status != 0`, unknown never counts as
  absent; `radar_window_started` after `RAW_DATA_RADAR_WINDOW_MIN_SECONDS` (backdated to the first present
  sample), `radar_window_closed` after `RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS` measured from the last present
  sample (`cleared` / `radar_unavailable` / `service_stopped` / `application_stopped`). The window events are
  durable, the sample is not. While a radar window is open, a 1 Hz
  `radar_sample` records the per-camera person state at that instant — including seconds where a camera
  person window is also open, because skipping those blanks out exactly the agreeing seconds (field data
  2026-09-16: 16 of 18 radar windows had a camera person window and still reported 0 % agreement) (the mirror of `person_sample`, which
  carries the radar snapshot) for up to `RAW_DATA_RADAR_SAMPLE_SECONDS` — the two together make every
  presence claim comparable from both sides, which is the whole point of the camera-vs-radar study.
  `PersonWindowSampler.camera_state(at)` is the shared per-camera view and its latest detections now
  survive window close. Evidence: `radar` snapshots (stamped with the live clock, **not** the backdated window time — the
  frame-freshness check rejected every camera otherwise) + a clip capped at `RAW_MEDIA_RADAR_CLIP_MAX_SECONDS`,
  `radar_end` snapshot on close, skipped with `person_window_active` while a camera person window owns the
  media and `radar_evidence_throttled` inside `RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS`. Analysis only.

**All of this is audit/telemetry only — no exceptions.** Archive success and media capture must
never relax, authorize, or influence the safety gate, AI, or the state machine. The LD2410 is a
**verification-only** sensor (owner decision 2026-09-16): its values are recorded for the offline
camera-vs-radar comparison and never feed the process engine, the driver display, or any operating
decision. `tests/test_process_engine.py` and `tests/test_pyqt_app.py` pin that the engine has no radar
input and that an LD2410 frame leaves the engine untouched. Evidence extensions for the
engine: a `managed: true` `vehicle_entered` opens a clip that stays open until `vehicle_session_ended`
(parking start) instead of the fixed 10 s post-roll, and `person_window_closed` now also captures a
`person_end` snapshot. NAS upload mode `immediate` (operator setting) triggers a debounced current-day
sync when the engine returns to IDLE; `scheduled` keeps the day-granularity behavior.

---

## 9. Commands

```bash
pytest -q                                     # 489 passed, hardware-free
./install_autorun.sh | ./start_autorun.sh | ./stop_autorun.sh | ./uninstall_autorun.sh  # 부팅 자동 실행 등록/시작/중지/해제
./run.sh                                      # fullscreen operator UI (uses .venv + .env)
./run-window.sh                               # windowed
towersightai-operator-ui --env .env --windowed
LOG_LEVEL=DEBUG towersightai-operator-ui --env .env    # per-run IDs, resolved paths, PIDs, exit codes
towersightai-check-settings --env .env [--check-hailo|--health-check-cameras|--preview-cameras --dry-run]
towersightai-ai-diagnostics --env .env --output artifacts/runtime/ai-diagnostics.txt
towersightai-sync-raw-data --env .env [--include-current-day]
towersightai-checkerboard [--paper A4|A3] [--square-mm 25]   # printable board → data/calibration/checkerboard/
towersightai-analyze sites import-env <site> --env .env        # register a NAS site for the analysis dashboard
towersightai-analyze sync --site <site> --from YYYY-MM-DD --to YYYY-MM-DD
./run-dashboard.sh                                            # = towersightai-analyze serve --open (dev/verification only)
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

Implemented: typed config + `.env` loading; RTSP preview with rotation/redaction and an all-camera FFmpeg
fallback (pip cv2 has no GStreamer); PyQt6 driver surface + the proposal-B developer console (sectioned
sidebar, workspace pages, shared camera grid); Hailo Apps multistream + purpose tasks (차량/사람 감지,
번호판 인식) with letterbox bbox correction, fatal-log handling, child-exit retry, and automatic
task-to-task switching; Hailo device-health monitor (pill + 시스템 점검 panel +
`towersightai.hailo.health` log); DiagnosticsService wired to the 시스템 점검 page; runtime-log viewer;
NAS connection check; LD2410 console; hourly raw JSONL + media evidence + verified Synology SFTP archive;
fake/simulator PLC adapters and the transition-only state machine.

Open gaps (see `INTENT.md` §5 for immediate field items and `PLAN.md` for the queue):

1. Centralize the final-OK prerequisites in one safety gate shared by UI and PLC paths.
2. Stage AI decisions: alignment/parking-position, plate handling, person + obstacle fusion, in-vehicle
   occupancy (all behind interfaces).
3. Calibration workflow — no module or UI yet; missing/invalid/unreviewed calibration must block final OK.
   The `vehicle_box_test/` first pass put a number on why this matters: without checkerboard intrinsics the
   diagonal camera's recovered pose disagrees with the ground plane by 16 %, which is what stops the 3D
   cuboid from being drawn at all.
4. Real PLC adapter behind the existing boundary, with event-ordering tests.
5. Field hardening: watchdogs, deployment runbook, structured safety audit traces.

Outbound flow (driver approach, vehicle movement, exit-complete confirmation) is a **future concept** — no
state, AI, or PLC contract exists for it yet.

## 12. Gotchas

- Two virtualenvs matter: the project `.venv` (UI, tests) and the Hailo Apps venv (`HAILO_APPS_PYTHON`) that
  actually executes `towersightai.cli.hailo_apps_detection`. Import errors there usually mean `PYTHONPATH`,
  not a code bug.
- The operator console exposes exactly two manual inference tasks, `사람 감지` and `차량 감지` (plus the automatic
  `프로세스 감시` that feeds the engine and image/front LPR). The old `이전 AI Detection` / general multistream
  path was removed from the UI on 2026-09-10; `inference/live_detection.py` keeps `LiveDetectionRunner` only as
  a tested library (runner helpers are shared with `purpose_tasks.py`).
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
- `tools/verify_operator_ui_screenshot.sh` clicks the sidebar by **pixel**, so its coordinates drift every
  time `SIDEBAR_SECTIONS` changes — they had silently drifted several rows before 2026-09-17 and were
  screenshotting the wrong pages. Re-measure by rendering `OperatorWindow` offscreen at the script's
  1920x1024 content canvas and reading each button's `mapTo(window, rect().center())`. Two traps: the
  sidebar **scrolls**, so the 시스템 section needs `scroll_sidebar_to_bottom` first (and `xdotool click
  --window` does not deliver wheel events to Qt — move the pointer in absolute coordinates and send the
  wheel globally); and `click_at` now refuses coordinates outside the canvas, because a click at y=1007 on
  a 900 px tall windowed run once landed on the browser behind the app. Run it with `fullscreen` on a
  1920x1080 screen for the full sweep.
- A leftover `operator_ui` process (e.g. a verify-script launch that survived SIGTERM) keeps camera RTSP
  sessions and starves later inference with RTSP 400. `pgrep -f operator_ui` before diagnosing "inference
  suddenly fails"; the verify script now force-kills after 10 s.
- An **orphaned inference child** (`Hailo Multisource App`, re-parented to systemd after the UI died without
  `killpg`) holds `/dev/hailo0` forever; every later child fails with `HAILO_OUT_OF_PHYSICAL_DEVICES(74)` and
  then segfaults in libgsthailo. `fuser /dev/hailo0` finds it (its cmdline is renamed, so `pgrep -f
  hailo_apps_detection` does not). Defences: the child arms `PR_SET_PDEATHSIG` + a ppid watchdog, the health
  monitor lists device holders (`HAILO 점유됨`), fatal messages name the holder PID, and 시스템 점검 has
  `고아 프로세스 종료` for non-child holders.
- The same orphan class exists for the **evidence recorder** (`cli/event_video_recorder.py`, spawned per camera
  when `RAW_MEDIA_ENABLED=true`): recorders left behind by a killed UI keep their RTSP session, and Tapo's
  per-camera session budget then rejects the inference child with `Bad Request (400)`. `ss -tn | grep :554`
  shows them. The recorder now arms `PR_SET_PDEATHSIG`, polls its ppid, and force-exits 5 s after a stop
  request if EOS never completes; the health scan (`find_orphaned_children`) lists both orphan kinds.
- RTSP `Bad Request (400)` is a *session-budget* failure, not a stream fault: Tapo keeps a dropped session
  for tens of seconds, so a fast retry only burns another slot. The runner waits
  `rtsp_busy_restart_delay_seconds` (10 s) after a 400 exit, and the monitoring auto-start waits 4 s for the
  streaming camera set to settle (one launch with every camera instead of front-only + relaunch) and backs off
  30 s → 60 s → 120 s after failed runs.
- Launching the UI from a shell that still exports the legacy `/opt/hailo/tappas` `GST_PLUGIN_PATH` /
  `LD_LIBRARY_PATH` makes children fail with `g_once_init_leave` / `gst_buffer_get_meta: api != 0` assertions
  and zero events. `run.sh` strips them and `hailo_apps_runtime_env` now strips them for the child as well.
- pyhailort: a temporary `Device()` is treated as released before `.control` is used — hold it in a
  variable and `device.release()` (see `hailo_health.make_subprocess_temp_probe`).
- The analysis dashboard (`towersightai/analyze/`, `towersightai-analyze`) is a **development/verification tool**
  that runs on the analyst's PC against the NAS archive — never on the site device. It must stay read-only and
  must never import `process`, `state_machine`, `plc`, or `ui` (a test pins this). NAS credentials live in
  `data/analysis/sites.json` (gitignored); labels in `data/analysis/sites/<site>/labels.jsonl`. Until the radar
  raw enhancement (`ld2410_sample` / `radar_window_*`) is deployed, radar episodes come only from
  `person_sample.ld2410` and are flagged `partial`. Days are keyed by `(source_host, day)`; host roles (현장/개발) live in `sites.json` and the dashboard
  shows field hosts only by default.
- **Hailo auto-recovery**: the same re-enumerate also runs by itself once the health monitor has
  reported `error` continuously for `HAILO_AUTO_RECOVERY_AFTER_SECONDS` (default 120 s).
  `HailoAutoRecoveryPolicy` (pure, clock-injected) restarts that clock on every attempt and caps them
  at `HAILO_AUTO_RECOVERY_MAX_ATTEMPTS` per `HAILO_AUTO_RECOVERY_WINDOW_SECONDS` (3 per hour), so a
  dead M.2 or an unstable supply is never hidden by a retry storm. It lives inside the app, so any
  launch path (`run.sh`, the autorun service) gets it; `HAILO_AUTO_RECOVERY_ENABLED=false` turns it off.
  The incident bundle is uploaded when the status *first* turns error, before recovery changes the
  device state, so evidence always survives.
- **Hailo device recovery from the console**: `시스템 점검` has `Hailo 장치 복구`, which runs the PCIe
  re-enumerate that used to be typed over SSH (`tools/hailo_recover.sh`: stop holders → `modprobe -r
  hailo_pci` → PCI `remove` → `rescan` → `modprobe` → verify with `identify`). It needs one narrow sudoers
  entry installed once per machine (`sudo tools/install_hailo_recover.sh`, which copies the script to a
  root-owned `/usr/local/sbin/towersightai-hailo-recover` so the repo copy cannot become arbitrary root).
  The UI stops inference first (the driver cannot unload while a child holds `/dev/hailo0`) and the usual
  monitoring auto-start brings it back. Recovery is maintenance, never authorization: final OK stays blocked.
- **Calibration results never go into git.** They travel over the NAS (`calibration/share.py`,
  `<folder>/calibration/<source_host>/<kind>/<camera>.json`). Each machine measures its own site, so a
  tracked result collides with the local file and makes `git pull` abort on the field device with
  "untracked working tree files would be overwritten" (that blocked the site box on 2026-09-21).
  `data/calibration/{intrinsics,ground,checkerboard}/` are gitignored and `tests/test_repo_hygiene.py`
  fails if any `data/calibration/**.json` becomes tracked again.
- Korean UI strings are part of the contract; keep the exact labels tests assert on.
