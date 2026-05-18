from __future__ import annotations

import re

from towersightai.config.settings import CameraConfig, CameraResolution, parse_camera_resolution


def redact_rtsp(rtsp_url: str) -> str:
    return re.sub(r"//([^:/]+):([^@]+)@", r"//***:***@", rtsp_url)


def build_preview_pipeline(
    camera: CameraConfig,
    latency_ms: int = 100,
    resolution: CameraResolution | tuple[int, int] | str = CameraResolution(),
) -> str:
    camera_resolution = _as_resolution(resolution)
    return (
        f"rtspsrc location={camera.rtsp_url} latency={latency_ms} ! "
        "rtph264depay ! h264parse ! decodebin ! videoconvert ! videoscale ! "
        f"video/x-raw,format=RGB,{camera_resolution.caps} ! appsink sync=false drop=true max-buffers=2"
    )


def _as_resolution(resolution: CameraResolution | tuple[int, int] | str) -> CameraResolution:
    return CameraResolution() if resolution == "" else parse_camera_resolution(resolution)
