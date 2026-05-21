# Camera and Configuration Guide

TowerSightAI uses four Tapo-C310 cameras. Camera network details are site-specific and must be configured through `.env` or an equivalent deployment secret mechanism.

## Camera Roles

- `ceiling`: ceiling center or upper camera, used for bird's-eye position and alignment.
- `front`: CAR-IN front camera, used for vehicle entry, plate recognition, and front UI.
- `rear_side`: rear and one-side view, used for people and obstacle detection.
- `opposite_side`: opposite side view, used for people and obstacle detection.

Use stable camera IDs in code and UI. Do not rely on list order alone.

## Environment Variables

Use placeholders in `.env.example`; never commit real credentials.

```dotenv
APP_ENV=development
LOG_LEVEL=INFO

TAPPAS_WORKSPACE=/opt/hailo/tappas
HAILO_HEF_PATH=${TAPPAS_WORKSPACE}/apps/h8/gstreamer/resources/hef/yolov5m_wo_spp_60p.hef
HAILO_POSTPROCESS_SO=${TAPPAS_WORKSPACE}/apps/h8/gstreamer/libs/post_processes/libyolo_hailortpp_post.so
HAILO_NETWORK_NAME=yolov5

CAMERA_1_ID=ceiling
CAMERA_1_ROLE=ceiling
CAMERA_1_RTSP_URL=rtsp://user:password@192.168.0.10:554/stream1
CAMERA_1_USERNAME=user
CAMERA_1_PASSWORD=password
CAMERA_1_ROTATION_DEGREES=90

CAMERA_2_ID=front
CAMERA_2_ROLE=front
CAMERA_2_RTSP_URL=rtsp://user:password@192.168.0.11:554/stream1
CAMERA_2_USERNAME=user
CAMERA_2_PASSWORD=password

CAMERA_3_ID=rear_side
CAMERA_3_ROLE=rear_side
CAMERA_3_RTSP_URL=rtsp://user:password@192.168.0.12:554/stream1
CAMERA_3_USERNAME=user
CAMERA_3_PASSWORD=password

CAMERA_4_ID=opposite_side
CAMERA_4_ROLE=opposite_side
CAMERA_4_RTSP_URL=rtsp://user:password@192.168.0.13:554/stream1
CAMERA_4_USERNAME=user
CAMERA_4_PASSWORD=password

CALIBRATION_PATH=data/calibration/site.json
PLC_ENDPOINT=tcp://127.0.0.1:502
UI_FULLSCREEN=true
UI_CAMERA_RESOLUTION=1280x720
```

If the RTSP URL already embeds credentials, keep separate username/password only when needed by setup tools. Product logs must redact credentials from URLs.

## RTSP and GStreamer

Tapo-C310 streams should be consumed through GStreamer. The common low-latency source pattern is:

```text
rtspsrc location=<rtsp-url> latency=<ms> !
rtph264depay ! h264parse ! decodebin !
videoconvert ! video/x-raw,format=RGB
```

For preview-only and operator-display paths, apply `CAMERA_N_ROTATION_DEGREES` before scaling, then scale decoded frames to `UI_CAMERA_RESOLUTION`; the default is `1280x720`. `90` means CCW 90 degrees and `270` means CW 90 degrees. The baseline equipment profile sets the ceiling birdview camera to `90` and the remaining cameras to `0`. The Hailo live detection path must use the same rotation setting before model resizing so the AI input stream matches the operator-visible stream. Apply width/height caps separately from RGB conversion to keep GStreamer negotiation compatible with RTSP decoders. Health-check paths should stay minimal and only verify that a fresh frame can be decoded. `appsink sync=false drop=true max-buffers=<small>` is acceptable for preview-only paths. For Hailo inference paths, normalize to the model input size and format required by the HEF after the configured rotation.

Tapo-C310 cameras are specified at 15 fps, so a healthy `stream1` preview may report about 15 fps even when the GStreamer pipeline is working correctly. Optimize preview paths for low latency and stable freshness rather than assuming a 30 fps source. Use `drop-on-latency=true`, small/leaky preview queues, and `appsink sync=false drop=true max-buffers=1` for preview-only frame capture. TCP remains the default transport for reliability; UDP may be tested on a stable LAN when lower latency matters more than packet-loss recovery.

## Health Checks

Each camera must expose:

- Last frame timestamp.
- Connection state.
- Decode/pipeline errors.
- Effective FPS or recent frame count.
- Redacted source URL.

Camera loss or stale frames block final OK.

## Configuration Validation

Startup must fail fast or enter NG/error when:

- Required camera role is missing.
- Duplicate camera IDs exist.
- Required RTSP URL is missing.
- Calibration file is missing in production mode.
- Hailo paths are missing on a hardware runtime.

Development mode may allow fake camera sources, but the UI must clearly show that it is not a production safety state.

## Security Rules

- Never write real `.env` values into documentation, logs, tests, or screenshots.
- Do not reuse credentials from `refers/`.
- Redact passwords before displaying pipeline strings.
- Prefer loading secrets at process startup instead of passing them through command-line history.
