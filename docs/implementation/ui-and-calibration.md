# UI and Calibration Guide

The UI is a driver-facing safety display for a 55-inch or larger monitor. It must be readable from inside or near the vehicle and must change with the AI state.

## Display Modes

## Before Entry

State: `IDLE`, `VEHICLE_DETECTED`, `PLATE_RECOGNITION`

- Show front camera view prominently.
- Show waiting, vehicle detection, and plate recognition status.
- Do not show OK styling before safety checks are complete.

## During Entry and Alignment

State: `VEHICLE_ENTERING`, `ALIGNMENT_GUIDE`

- Show front view and ceiling bird's-eye view together.
- Overlay lane centerline, side bounds, stop zone, and vehicle position.
- Show one clear instruction at a time:
  - "차량을 오른쪽으로 조금 이동해 주세요."
  - "차량을 왼쪽으로 조금 이동해 주세요."
  - "조금 더 앞으로 진입해 주세요."
  - "차량을 조금 후진해 주세요."
  - "정상 위치에 주차되었습니다."

## After Parking

State: `PARKED`, `SAFETY_CHECK`, `HUMAN_DETECTED`, `READY_FOR_OPERATION`

- Show safety check screen.
- Show parking-machine person status.
- Show in-vehicle occupancy status.
- Show final OK/NG/waiting state.
- Show next driver instructions, such as engine off and side mirrors folded.

## Error and Stop States

State: `AI_STOP` or error/NG substates

- Show that AI monitoring is stopped or blocked.
- Show camera, calibration, Hailo, or PLC fault reason when available.
- Never present a blocked state as safe.

## Settings UI

On-site settings must support:

- Camera status and redacted RTSP source display.
- Camera role assignment.
- Hailo/TAPPAS path status.
- PLC connection status.
- Confidence thresholds.
- Calibration file load/save.
- Fullscreen display setting.

Settings must not reveal camera passwords.

## Calibration UI

Calibration is required for accurate alignment and safety zones. Provide per-camera calibration workflows:

- Select camera.
- Draw or adjust lane centerline.
- Draw side boundaries.
- Draw stop zone.
- Draw danger/safety ROIs.
- Draw vehicle cabin/window ROIs for front and side cameras.
- Save calibration with timestamp, site ID, camera ID, and version.

Calibration changes must be reviewable before activation. Invalid or missing calibration blocks final OK.

## Calibration Data Shape

Use a structured JSON file or equivalent typed format:

```json
{
  "site_id": "site-placeholder",
  "version": 1,
  "updated_at": "2026-04-29T11:30:00+09:00",
  "cameras": {
    "ceiling": {
      "lane_centerline": [[0.5, 0.1], [0.5, 0.9]],
      "left_boundary": [[0.2, 0.1], [0.2, 0.9]],
      "right_boundary": [[0.8, 0.1], [0.8, 0.9]],
      "stop_zone": [[0.3, 0.7], [0.7, 0.7], [0.7, 0.9], [0.3, 0.9]],
      "tolerances": {"lateral": 0.05, "forward": 0.05}
    }
  }
}
```

Coordinates should be normalized unless a module has a documented reason to use pixels.

## UI Testing

- Render tests for each state.
- Tests for Korean driver instruction selection.
- Tests that password values are redacted.
- Tests for calibration save/load validation.
- Tests that error/NG states cannot display final OK styling.
