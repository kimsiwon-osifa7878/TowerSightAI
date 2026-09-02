# AGENTS.md

This repository implements TowerSightAI, an AI safety monitoring system for a parking machine. All agents must preserve the safety-first behavior described in `docs/주차기_AI_안전감시_시스템_설계안.md`.

## Required Context

Read these before implementation work:

- `CLAUDE.md` (repository map, runtime facts, gotchas)
- `INTENT.md` (agreed working style, decision rationale, open field items)
- `docs/주차기_AI_안전감시_시스템_설계안.md`
- `docs/implementation/system-architecture.md`
- `docs/implementation/camera-and-config.md`
- `docs/implementation/hailo-gstreamer.md`
- `docs/implementation/ai-stages.md`
- `docs/implementation/ui-and-calibration.md`
- `docs/implementation/testing-strategy.md`
- `docs/implementation/implementation-roadmap.md`

Reference hardware experiments:

- `refers/detection.py`: single-stream Hailo pipeline pattern.
- `refers/callback_template.py`: `hailopython` callback contract.
- `refers/multi_stream_detection_rtsp.sh`: official-style multi-RTSP Hailo pipeline shape.
- `refers/test02.py`: low-latency RTSP-to-appsink preview pattern.
- `refers/multi.py` and `refers/test01.py`: experimental references only; verify before reuse.

## Project Rules

- Default to NG when uncertain. Never send PLC OK on missing camera frames, low confidence, invalid calibration, unknown PLC state, or possible human/obstacle presence.
- Do not commit or introduce real camera credentials, RTSP URLs with passwords, PLC secrets, `.env`, or local Hailo install paths.
- Keep all deployment-specific values in `.env`, `.env.example`, or typed config files. `.env.example` must contain placeholders only.
- Product code must target Ubuntu. Windows may be used for editing and non-hardware tests only.
- Keep `refers/` unchanged unless the user explicitly asks to edit reference code.

## Architecture Boundaries

- Camera ingest owns RTSP/GStreamer connectivity and health.
- Hailo inference owns HEF/postprocess configuration and normalized detection events.
- AI stage logic owns vehicle, plate, alignment, person, obstacle, and in-vehicle occupancy decisions.
- State machine owns legal transitions and conservative gating.
- PLC adapter owns external communication and must be mockable.
- UI owns driver display, settings, camera preview, and calibration interaction.

## Driver Display Direction

- Keep user mode and operator mode as separate surfaces within the same application.
- User mode keeps two operator entry points: the hidden two-second top-right hold and a visible bottom-right `운영자 모드` service button for on-site use. Operator mode owns the return path (`사용자 화면`) and application exit (`프로그램 종료`, confirmation required) in its menu. These are mode/lifecycle controls only and must never carry diagnostics, change safety state, or emit PLC events.
- User mode is the parking-machine display seen by the driver. Prioritize live camera context, ceiling birdview alignment, one current instruction, and the blocking reason.
- Operator mode owns diagnostics, model details, camera inspection, settings, calibration, and test controls. Do not expose local paths, inference counters, or development state buttons on the driver-facing surface.
- Use the provided ParkIO concept guide for product flow and visual direction only. It is not a verified safety specification and does not prove that alignment, occupancy, obstacle, exit, or PLC behavior is implemented.
- The user-mode visual language is bright white/navy/cyan for normal guidance. Reserve red for stop, person detection, camera/AI faults, and other blocking conditions. Never rely on color without a status word and required action.
- Show one driver action at a time. Prefer `진입`, `정지`, `오른쪽 이동`, `왼쪽 이동`, `전진`, `후진`, or `주차기 밖으로 이동` over diagnostic prose.
- State-specific camera priority must follow the state model: front camera for approach, front plus ceiling for entry, ceiling birdview for alignment, and all relevant cameras for safety/person checks.
- Prototype, fake, and simulated screens must remain visibly non-authoritative and cannot present or emit final PLC OK.
- Outbound driver approach, vehicle movement, and exit-complete confirmation are future concepts until corresponding state, AI, and PLC contracts are explicitly implemented and tested.

## Implementation Order

Follow `docs/implementation/implementation-roadmap.md` as the source of truth for implementation order. In short:

1. Configuration schema and `.env.example`.
2. Camera ingest and health checks.
3. Hailo multi-stream inference and callback result normalization.
4. State machine and PLC adapter interface.
5. Driver UI and calibration tools.
6. Stage-specific AI decision logic.
7. Hardware smoke scripts and deployment notes.

## Test Rules

- Add tests with each feature. Do not leave major behavior untested.
- Unit tests should run without live RTSP cameras, Hailo-8, or PLC.
- Hardware tests must be clearly marked and skippable.
- Every state transition that can affect PLC OK/NG must have tests for success, failure, and uncertainty.

## UI-Centered Verification

For changes that affect the operator UI layout, buttons, status text, camera surfaces, overlays, sidebar behavior, diagnostics screen, or UI-first test flows, verification must include a real UI run when a GUI-capable Ubuntu desktop environment is available.

Required post-implementation checks:

1. Run `pytest -q`.
2. Launch the operator UI in windowed mode.
3. Capture the default operator dashboard screenshot.
4. Click `메뉴` and capture the sidebar-open screenshot.
5. Verify key UI flows by clicking:
   - `전체 카메라`
   - `주차 프로세스 테스트` (and `차량 진입 시뮬레이션` inside it)
   - `레이더 (LD2410)`
   - `실행 로그`
6. Confirm the UI still keeps final OK blocked for simulation and LD2410 console actions.
7. Store screenshots under `tmp/operator-ui-verification/` and do not commit them.

Use the existing screenshot helper as the baseline command:

```bash
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```

If the current environment cannot run a GUI, state that clearly in the final report and include the reason, such as missing display session, missing `xdotool`, missing `gnome-screenshot`, or unavailable PyQt/OpenCV runtime. UI screenshot verification is implementation verification only; it is not product safety approval and must never imply PLC OK.

## External References

- Hailo-8 M.2: https://hailo.ai/products/ai-accelerators/hailo-8-m2-ai-acceleration-module/
- TAPPAS multi-stream detection: https://github.com/hailo-ai/tappas/tree/master/apps/h8/gstreamer/general/multistream_detection
- Hailo `hailopython`: https://github.com/hailo-ai/tappas/blob/master/docs/elements/hailo_python.rst
- Hailo `hailoroundrobin`: https://github.com/hailo-ai/tappas/blob/master/docs/elements/hailo_roundrobin.rst
- Hailo `hailostreamrouter`: https://github.com/hailo-ai/tappas/blob/master/docs/elements/hailo_stream_router.rst
