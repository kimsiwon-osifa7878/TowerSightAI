from __future__ import annotations

import re

from towersightai.config.settings import CameraConfig, CameraResolution, parse_camera_resolution


def redact_rtsp(rtsp_url: str) -> str:
    return re.sub(r"//([^:/]+):([^@]+)@", r"//***:***@", rtsp_url)


def build_preview_pipeline(
    camera: CameraConfig,
    latency_ms: int = 100,
    resolution: CameraResolution | tuple[int, int] | str = CameraResolution(),
    transport: str = "tcp",
) -> str:
    if transport not in ("tcp", "udp"):
        raise ValueError(f"unsupported RTSP transport: {transport}")
    camera_resolution = _as_resolution(resolution)
    return (
        f"rtspsrc location={camera.rtsp_url} latency={latency_ms} protocols={transport} "
        "drop-on-latency=true ! "
        "rtph264depay ! h264parse ! decodebin ! "
        "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        "videoscale ! "
        f"video/x-raw,{camera_resolution.caps} ! videoconvert ! "
        "video/x-raw,format=RGB ! appsink sync=false drop=true max-buffers=1"
    )


def _as_resolution(resolution: CameraResolution | tuple[int, int] | str) -> CameraResolution:
    return CameraResolution() if resolution == "" else parse_camera_resolution(resolution)
