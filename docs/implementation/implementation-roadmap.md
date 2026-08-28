# Implementation Roadmap

This roadmap breaks the work into stable phases. Each phase should leave the repository in a testable state. From the current prototype forward, development is UI-first: create the operator UI surface and test slot first, connect fake or empty behavior safely, then wire in production logic.

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

## Phase 4: State Machine and PLC Adapter Boundary

- Implement design-document states.
- Add conservative safety gate shape.
- Add fake PLC adapter.
- Add event ordering tests.
- Add real PLC adapter only after the protocol is confirmed.

Done when OK/NG flows can be represented by tests without live PLC hardware.

## Phase 5: Operator UI Shell and In-UI Test Harness

- Use separate driver-facing user mode and operator mode in one PyQt6 application.
- Start in the dark, camera-first user mode.
- Enter operator mode through the hidden two-second top-right mouse/touch hold.
- Show ceiling birdview and front camera as the primary first screen.
- Provide a collapsible sidebar for navigation and feature slots.
- Keep development state controls in the operator-only user-screen test panel.
- Keep diagnostic entries explicit and safety-neutral; the current sidebar uses the connected LD2410 console instead of empty placeholders.
- Keep the in-UI test screen as the first field-verification hub.

Done when user mode has no development controls, mode switching is non-accidental, and the operator can switch dashboard/all-camera/test/LD2410 views while final OK remains blocked.

## Phase 6: Camera and AI Visualization Panels

- Show preview health and inference health separately.
- Show detection counts, stale event status, and last error summaries in the UI.
- Add fake detection playback before connecting more production AI decisions.
- Add a live multistream diagnostic test for connected cameras.

Done when camera and AI health can be verified from the operator UI without reading logs.

## Phase 7: Calibration Workflow

- Add calibration entry from the sidebar.
- Support selected-camera editing for lane, stop-zone, and ROI geometry.
- Add save, revert, review, and activation states.
- Validate calibration before activation.
- Keep final OK blocked for missing, invalid, or unreviewed calibration.

Done when an operator can configure and validate camera calibration without editing source code.

## Phase 8: Stage Simulation and Fake Event Playback

- Add UI controls for vehicle entry, alignment, person, obstacle, plate, and occupancy test scenarios.
- Mark every simulated state as test-only.
- Use fake events to drive UI instructions and NG/WAIT/READY styling.
- Keep fake events separate from real PLC authorization.

Done when each safety-relevant stage can be visually tested in the UI before production logic is connected.

## Phase 9: Real AI and PLC Integration Through the UI

- Connect vehicle detection and alignment logic.
- Connect person/obstacle fusion.
- Connect plate and in-vehicle occupancy adapters.
- Connect real PLC protocol behind the adapter boundary.
- Route all stage outputs through the central safety gate.

Done when final OK is possible only through real healthy inputs, valid calibration, known PLC state, and passing stage decisions.

## Phase 10: Field Hardening

- Add watchdogs and restart policy.
- Add deployment notes for Ubuntu, Hailo, TAPPAS, GStreamer, display, and network.
- Add structured logs and safety audit traces.
- Add site acceptance checklist driven from the operator UI test hub.

Done when the system can be installed, calibrated, smoke-tested, and operated at a site.
