# Hailo and GStreamer Guide

TowerSightAI uses Hailo-8 M.2 for real-time edge inference. Hailo-8 M.2 provides up to 26 TOPS and supports Linux edge deployments through HailoRT/TAPPAS.

## References

- `refers/detection.py`: clean single-stream Hailo pipeline reference.
- `refers/callback_template.py`: `hailopython` function contract.
- `refers/multi_stream_detection_rtsp.sh`: vendor-style multi-RTSP pipeline launcher.
- Official Hailo TAPPAS multi-stream detection examples.

Treat `refers/multi.py` and `refers/test01.py` as experimental. Validate every pipeline segment before product use.

## Environment

Required runtime pieces on the Ubuntu target:

- Hailo-8 M.2 detected over PCIe.
- HailoRT installed and compatible with TAPPAS.
- TAPPAS workspace available through `TAPPAS_WORKSPACE`.
- GStreamer with Hailo plugins: `hailonet`, `hailofilter`, `hailopython`, `hailooverlay`, `hailoroundrobin`, `hailostreamrouter`.
- Model HEF and postprocess `.so` configured by environment.

The Hailo/TAPPAS virtual environment can follow the shape from `refers/venv.sh`:

```sh
source "$TAPPAS_WORKSPACE/hailo_tappas_venv/bin/activate"
```

## Single-Stream Pattern

Use this for focused development and hardware smoke tests:

```text
source -> decode/scale/convert -> RGB 640x640 ->
hailonet hef-path=<hef> batch-size=1 <thresholds> ->
hailofilter function-name=<network> so-path=<postprocess-so> qos=false ->
hailopython module=<callback> qos=false ->
hailooverlay -> display/appsink
```

The callback module must export `run(video_frame: VideoFrame)` and may export `close()`.

## Multi-Stream Pattern

Use one shared inference line for the four RTSP cameras:

```text
camera_0 source -> preprocess -> robin.sink_0
camera_1 source -> preprocess -> robin.sink_1
camera_2 source -> preprocess -> robin.sink_2
camera_3 source -> preprocess -> robin.sink_3

hailoroundrobin name=robin ->
hailonet ->
hailofilter ->
hailopython/callback ->
hailostreamrouter name=router ->
per-camera outputs -> UI/compositor/event bus
```

`hailoroundrobin` tags frames by input pad. `hailostreamrouter` must map those tags back to camera IDs so downstream logic knows which camera produced each detection.

## Callback Output

Normalize every Hailo detection into an internal event:

```json
{
  "camera_id": "front",
  "label": "person",
  "confidence": 0.91,
  "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
  "timestamp": "2026-04-29T11:33:00+09:00",
  "source": "hailo"
}
```

The state machine must not parse raw Hailo objects directly.

## Model Policy

- Initial object detection may use YOLO HEF paths from TAPPAS references.
- Plate OCR and in-vehicle occupancy may require separate models or CPU-side modules. Keep those behind interfaces until the model is selected.
- Model thresholds must be configurable and tested.
- Low confidence must become NG or retry, never OK.

## Pipeline Testing

Unit tests should assert generated pipeline strings without requiring Hailo hardware:

- Correct number of RTSP sources.
- No hardcoded credentials.
- HEF and postprocess paths come from config.
- `hailoroundrobin`, `hailonet`, `hailofilter`, `hailopython`, and `hailostreamrouter` are present.
- Camera IDs map to router outputs.

Hardware smoke tests should run only when explicitly enabled and should print redacted pipeline details.
