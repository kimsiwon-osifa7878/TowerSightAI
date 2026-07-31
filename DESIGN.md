# TowerSightAI Display Design

## 1. Purpose

TowerSightAI is an industrial safety display for a parking machine. The display must help a driver park accurately, make hazards obvious, and keep the machine blocked whenever the system cannot prove that operation is safe.

The application keeps two modes:

- **User mode**: the driver-facing display installed at the parking machine.
- **Operator mode**: camera inspection, diagnostics, settings, calibration, and test controls.

User mode is the focus of this redesign. Operator mode keeps its existing dark diagnostic direction until it is redesigned separately.

## 2. Reference Analysis

The local `SPSS_ParkIO_안내자료.pdf` is a product and visual reference. It is intentionally excluded from Git and must not be treated as a verified safety specification.

The reference communicates five useful ideas:

1. The product journey begins before the vehicle enters and continues through position guidance, plate recognition, driver exit, and person checks.
2. The installed screen gives one immediate movement or stop instruction instead of exposing AI internals.
3. Front, ceiling, rear-side, and opposite-side views reduce blind spots; camera priority changes with the parking stage.
4. Birdview is the primary alignment evidence, with lane bounds, vehicle position, movement direction, and a stop target shown together.
5. A person or an unverified condition changes the display into an explicit blocked state and prevents machine operation.

The reference also depicts driver approach, outbound vehicle movement, and exit completion. These remain future TowerSightAI concepts until matching state-machine, AI-stage, and PLC contracts exist.

## 3. Current UI Findings

The current user mode already provides ceiling and front camera surfaces, Korean instructions, plate output, and safe test controls. Its main gaps are:

- Development state buttons compete with the driver instruction.
- Camera content is large, but the current movement and safety stage are not visually encoded over it.
- Birdview does not yet communicate calibrated lane, centerline, stop zone, or vehicle offset.
- Camera, AI, and PLC diagnostics use operator language on the driver surface.
- Person detection and camera failure are text warnings rather than distinct action-oriented layouts.
- The dark user surface does not match the requested bright ParkIO-like installed display.

The redesign changes presentation and information hierarchy. It must not weaken `can_show_final_ok`, state-machine rules, or PLC boundaries.

## 4. Design Principles

### One action per screen

The largest text is the action the driver must take now:

- `천천히 진입해 주세요`
- `오른쪽으로 이동해 주세요`
- `왼쪽으로 이동해 주세요`
- `조금 더 전진해 주세요`
- `정지해 주세요`
- `차량에서 내려 주차기 밖으로 이동해 주세요`
- `사람이 감지되었습니다. 주차기 밖으로 이동해 주세요`

Diagnostic prose, model names, event counts, paths, and raw confidence values stay in operator mode.

### Evidence before decoration

Camera imagery occupies most of the screen. Guides and detections are explicit data layers over the image. Empty decorative cards, gradients, illustrations, and marketing copy are not used.

### Blocked is never ambiguous

`UNKNOWN`, stale frames, camera loss, Hailo failure, invalid calibration, possible person, possible occupant, and possible obstacle are blocked states. They use a status word, reason, required action, and red visual treatment. Color alone is insufficient.

### Simulation is never authorization

HTML prototypes, UI simulations, fake frames, and manual test states display `DESIGN PROTOTYPE · PLC OUTPUT BLOCKED`. They never show an operative final OK.

## 5. Visual System

### Color tokens

| Token | Value | Use |
|---|---:|---|
| `canvas` | `#eef3f7` | user-mode background |
| `surface` | `#ffffff` | camera and information surfaces |
| `navy-900` | `#082f49` | headings and primary text |
| `navy-700` | `#0f4c6e` | secondary text and borders |
| `cyan-500` | `#06b6d4` | guides, progress, active stage |
| `cyan-100` | `#cffafe` | active-stage background |
| `amber-500` | `#f59e0b` | waiting or retry |
| `red-600` | `#dc2626` | stop, person, fault, blocked |
| `red-050` | `#fff1f2` | blocked-state background |
| `green-600` | `#16803a` | verified subsystem only |
| `ink` | `#10212b` | normal body text |

Green may describe a verified subsystem, but simulated or incomplete inputs must not produce a final green OK state.

### Typography

- Font: `Noto Sans CJK KR`, `Noto Sans KR`, or the available system sans-serif.
- Primary driver instruction: 48-64 px at 1920x1080.
- State and blocking reason: 24-32 px.
- Camera labels and compact health: 16-20 px.
- Letter spacing remains `0`.
- Long text wraps; font size does not scale directly with viewport width.

### Shape and icon use

- Panels use square or small-radius corners, maximum 8 px.
- Familiar directional symbols are used for movement; text always accompanies the symbol.
- Camera surfaces keep stable aspect ratios and dimensions when labels or alerts change.

## 6. Full HD Layout

The primary viewport is 1920x1080. The same hierarchy must remain intact at 1440x900.

```text
┌──────────────────────────────────────────────────────────────┐
│ Brand · current stage                    system/block status │  88 px
├──────────────────────────────────────────────────────────────┤
│                                                              │
│         state-specific primary and secondary cameras         │  flexible
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ direction symbol · one driver instruction · blocking reason  │ 132 px
├──────────────────────────────────────────────────────────────┤
│ approach · entry · alignment · exit vehicle · safety check   │  64 px
└──────────────────────────────────────────────────────────────┘
```

- Outer margin: 24-32 px.
- Camera workspace gap: 12-16 px.
- Header and instruction height remain stable across states.
- The stage progress row is informational. It is not a clickable driver control.
- Prototype-only state controls sit outside this product canvas.

## 7. State Layouts

| State | Camera layout | Primary instruction | Safety display |
|---|---|---|---|
| `IDLE` | front dominant; compact system readiness | wait for approaching vehicle | blocked until a real workflow begins |
| `VEHICLE_DETECTED` | front dominant, ceiling preview | enter slowly | wait |
| `PLATE_RECOGNITION` | front dominant, masked plate result | stop briefly or enter slowly according to state | wait/retry |
| `VEHICLE_ENTERING` | front 58%, ceiling 42% | current forward/stop guidance | wait |
| `ALIGNMENT_GUIDE` | ceiling 58%, front 42% | one lateral or fore/aft correction | wait/NG on invalid calibration |
| `PARKED` | ceiling and front, stopped vehicle emphasized | engine off, mirrors folded, exit vehicle | wait |
| `SAFETY_CHECK` | four-camera 2x2 evidence grid | leave the parking machine and wait | blocked until every required check passes |
| `HUMAN_DETECTED` | detecting camera 70%, other views summarized | stop and move outside | red blocked |
| fault/unknown | keep any available feed; fault panel dominates | stop and wait for assistance | red blocked |
| `AI_STOP` | camera analysis stopped indicator | machine operation state | no AI safety authorization shown |

`READY_FOR_OPERATION` may be designed later from real state inputs. The HTML prototype and manual UI simulations do not present final PLC OK.

## 8. Camera And Overlay Layers

Render overlays in this order:

1. Camera image.
2. Calibrated lane bounds and centerline.
3. Stop zone and danger/safety ROIs.
4. Vehicle footprint and offset vector.
5. Person, obstacle, or vehicle detection boxes.
6. Camera name and fresh/stale health.
7. Blocking scrim for lost or stale inputs.

### Birdview

- Show left/right lane bounds, centerline, stop zone, and vehicle footprint.
- Use cyan for valid guides, amber for near-limit position, and red for outside tolerance.
- Show only the current correction arrow. Do not show competing directions.
- Missing or invalid calibration replaces guidance with `캘리브레이션 확인 필요 · 동작 차단`.

### Person detection

- Use a red bounding box and `사람 감지` label.
- Promote the detecting camera to the primary surface.
- Show the camera location in human-readable Korean.
- Do not require multi-camera identity matching before blocking.

### Camera and AI faults

- A stale image receives a translucent red scrim and timestamp/health reason.
- A missing image shows `영상 수신 불가`.
- Hailo failure shows `AI 판단 불가`.
- Both states instruct the driver to stop and keep operation blocked.

## 9. Mode And Interaction Rules

### User mode

- Starts as the installed driver-facing display.
- Contains no development state buttons, model selection, file paths, or inference counters.
- Uses a near-black, full-camera canvas modeled after modern surround-view parking displays. Camera content reaches every screen edge and all driver guidance is rendered as an overlay.
- Targets a 50-inch 16:9 display viewed from approximately 6 m. The current action uses a `52-86 px` responsive headline; camera names, health, and prototype diagnostics stay intentionally small.
- Keeps the driver action in a shallow top overlay with a 50%-transparent background so the camera remains visible. Branding, current stage, and diagnostic status share a compact strip at the bottom.
- Shows one short driver action at a time (`진입`, `정지`, `오른쪽 이동`, or `주차기 밖으로 이동`) with supporting text reduced to one line.
- Provides a visually hidden `72 x 72 px` hotspot in the top-right corner. Holding it for two seconds enters operator mode; a short press, pointer cancellation, or moving outside the hotspot does nothing.
- Uses state-machine and normalized AI-stage output, not raw detections, to select the instruction.

### Operator mode

- Retains all-camera inspection, camera settings, purpose-specific inference controls, diagnostics, simulations, and `EMPTY` slots.
- Follows the current PyQt dark industrial HMI direction: collapsible sidebar, persistent NG status, dashboard/all-camera layouts, camera settings, and a telemetry strip.
- Provides a visible `사용자모드` sidebar action for returning to the driver-facing surface.
- Owns manual state playback used for UI testing.
- Never lets fake or simulated input authorize PLC OK.

### Presentation contract

The future PyQt implementation should derive a driver presentation from existing domain state rather than storing independent display truth:

```text
stage
safety_status
headline
blocking_reason
primary_camera_roles
secondary_camera_roles
alignment_direction
masked_plate_text
camera_health
```

This is an internal UI contract, not a new PLC or external API.

## 10. HTML Prototype

`docs/design/towersightai-ui-prototype.html` is the approval artifact.

- It is a standalone HTML/CSS/JavaScript file with no network dependency.
- It uses only sanitized repository assets and abstract mock camera surfaces.
- It switches between representative states without starting cameras, Hailo, or PLC communication.
- It demonstrates the two-second operator entry gesture and a non-authoritative operator-mode mock without invoking product services.
- Operator actions only change local prototype presentation. AI controls, simulation, camera settings, and `EMPTY` actions remain explicitly marked as mock/not connected and keep final OK blocked.
- The state selector is clearly separated from the simulated product screen.
- Every state carries one persistent prototype/PLC-blocked notice without repeating it on every camera tile.
- Approval covers layout, hierarchy, colors, wording, camera priority, and overlay direction.

The reviewed prototype is the visual contract for the production PyQt6 user-mode implementation.

## 11. Acceptance Criteria

- The required driver action is understandable within one glance.
- Front, ceiling, and safety-camera priority changes match the current state.
- Birdview clearly communicates lane, center, stop zone, and correction direction.
- Person detection and fault states cannot be mistaken for safe operation.
- At 1920x1080 and 1440x900, text does not overlap cameras or controls.
- No real RTSP URL, password, PLC secret, local Hailo path, or real plate is present.
- A short press or aborted hold on the hidden operator hotspot cannot change mode; a completed two-second hold enters operator mode.
- Operator mode exposes the PyQt-equivalent menu set, can return visibly to user mode, and never implies that AI, camera settings, or PLC output actually ran.
- The prototype and every simulation remain visibly blocked from final PLC OK.
- Later PyQt work preserves all existing conservative safety gates and passes the repository UI verification checklist.
