# Hailo Apps and GStreamer Guide

TowerSightAI uses Hailo-8 M.2 for real-time edge inference. The active integration follows the current [Hailo Apps](https://github.com/hailo-ai/hailo-apps) Python pipeline and callback API.

The files under `refers/` document the earlier TAPPAS/`hailopython` experiments and remain unchanged. They are historical references, not the active runtime contract.

## Environment

Required runtime pieces on the Ubuntu target:

- Hailo-8 M.2 detected over PCIe.
- A mutually compatible HailoRT, TAPPAS Core, and Hailo Apps installation.
- Hailo Apps checkout at `HAILO_APPS_WORKSPACE`, default `~/hailo-apps`.
- Resource tree at `HAILO_APPS_RESOURCES`, normally `~/hailo-apps/resources`.
- Hailo Apps virtual environment Python at `HAILO_APPS_PYTHON`.
- GStreamer elements `hailonet`, `hailofilter`, `hailoroundrobin`, and `hailostreamrouter`.
- The configured HEF, YOLO postprocess `.so`, and `libstream_id_tool.so`.

`hailopython` is not required. The adapter runs inside Hailo Apps Python and receives each `Gst.Buffer` through an `identity` pad callback.

## Active Multi-Stream Pattern

```text
RTSP sources
  -> per-source stream ID
  -> hailoroundrobin
  -> hailonet
  -> hailofilter
  -> Hailo Apps Python buffer callback
  -> hailostreamrouter/headless sinks
  -> TowerSightAI JSONL events
```

The callback reads `roi.get_stream_id()`, maps `src_N` back to the configured camera ID, normalizes detections, and transforms bounding-box coordinates to the UI rotation. Raw Hailo objects never enter the state machine.

## Model Policy

For Hailo-8, the current Hailo Apps default detection resource is:

- HEF: `resources/models/hailo8/yolov8m.hef`
- Postprocess: `resources/so/libyolo_hailortpp_postprocess.so`
- Postprocess function: `filter_letterbox`

General detection emits all accepted classes. The vehicle task keeps `car`, `truck`, `bus`, and `motorcycle`; the person task keeps `person`. Separate environment variables remain available for a future compatible HEF, but HEF and postprocess artifacts must come from a compatible Hailo software stack.

The legacy `yolov5m_vehicles.hef`, `yolov5s_personface_reid.hef`, JSON configs, crop helper, and `hailopython` callback are not active. FastALPR image/plate OCR remains a separate CPU path.

Low confidence, missing resources, no events, callback failures, or process failures remain NG/wait and never authorize PLC OK.

## Runtime Failure Handling

The adapter writes stdout and stderr to the task's raw log. Runners watch for Hailo device failures, unsupported HEFs, missing GStreamer elements, internal stream errors, and abnormal process exits. A failed or stuck process is terminated and reported to the UI while PLC OK remains blocked.

The diagnostic collector reads existing logs and status files only; it does not start inference or hardware checks.
