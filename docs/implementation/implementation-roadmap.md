# Implementation Roadmap

This roadmap breaks the work into stable phases. Each phase should leave the repository in a testable state.

## Phase 1: Project Skeleton

- Choose backend and UI project structure.
- Add `.env.example` with placeholders only.
- Add typed configuration models.
- Add test runner and base CI commands.
- Add fake PLC and fake camera interfaces.

Done when config tests pass and no hardware is required.

## Phase 2: Camera Ingest

- Implement camera config loading.
- Build RTSP GStreamer source strings.
- Add preview/appsink path for low-latency frame capture.
- Add camera health tracking.
- Add redacted pipeline logging.

Done when four fake or test RTSP sources can be represented and health states are testable.

## Phase 3: Hailo Inference

- Implement single-stream Hailo smoke path.
- Implement multi-stream Hailo pipeline builder.
- Add `hailopython` callback module or Hailo Apps callback adapter.
- Normalize detections by camera ID.
- Add skippable hardware smoke tests.

Done when pipeline strings are tested and target hardware can run a smoke test.

## Phase 4: State Machine and PLC Adapter

- Implement design-document states.
- Add conservative safety gate.
- Add fake PLC adapter.
- Add event ordering tests.
- Add real PLC adapter only after the protocol is confirmed.

Done when OK/NG flows are fully covered by unit tests.

## Phase 5: UI and Calibration

- Implement state-aware driver UI.
- Add settings screen.
- Add calibration drawing, validation, and persistence.
- Add UI tests for state displays and secret redaction.

Done when an operator can configure cameras/calibration without editing source code.

## Phase 6: AI Stage Logic

- Add vehicle detection decision module.
- Add plate recognition adapter.
- Add alignment decision module.
- Add person/obstacle decision module.
- Add in-vehicle occupancy adapter.
- Add fusion tests across all cameras.

Done when all final OK prerequisites are represented and tested.

## Phase 7: Field Hardening

- Add watchdogs and restart policy.
- Add deployment notes for Ubuntu, Hailo, TAPPAS, GStreamer, display, and network.
- Add structured logs and safety audit traces.
- Add site acceptance checklist.

Done when the system can be installed, calibrated, smoke-tested, and operated at a site.
