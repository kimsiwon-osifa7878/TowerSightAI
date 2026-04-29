from __future__ import annotations

import re

from towersightai.config.settings import CameraConfig


def redact_rtsp(rtsp_url: str) -> str:
    return re.sub(r"//([^:/]+):([^@]+)@", r"//***:***@", rtsp_url)


def build_preview_pipeline(camera: CameraConfig, latency_ms: int = 100) -> str:
    return (
        f"rtspsrc location={camera.rtsp_url} latency={latency_ms} ! "
        "rtph264depay ! h264parse ! decodebin ! videoconvert ! "
        "video/x-raw,format=RGB ! appsink sync=false drop=true max-buffers=2"
    )
