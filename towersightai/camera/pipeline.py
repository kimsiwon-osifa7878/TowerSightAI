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
    rotation_degrees: int | None = None,
) -> str:
    if transport not in ("tcp", "udp"):
        raise ValueError(f"unsupported RTSP transport: {transport}")
    rotation = camera.rotation_degrees if rotation_degrees is None else rotation_degrees
    camera_resolution = display_resolution_for_rotation(_as_resolution(resolution), rotation)
    orientation = display_orientation_filter(rotation)
    return (
        f"rtspsrc location={camera.rtsp_url} latency={latency_ms} protocols={transport} "
        "drop-on-latency=true ! "
        "rtph264depay ! h264parse ! decodebin ! "
        "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        f"{orientation}"
        "videoscale ! "
        f"video/x-raw,{camera_resolution.caps} ! videoconvert ! "
        "video/x-raw,format=RGB ! appsink sync=false drop=true max-buffers=1"
    )


def display_orientation_filter(rotation_degrees: int) -> str:
    element = display_orientation_element(rotation_degrees)
    if not element:
        return ""
    return f"{element} ! "


def display_orientation_element(rotation_degrees: int) -> str:
    rotation = normalize_rotation_degrees(rotation_degrees)
    return {
        0: "",
        90: "videoflip method=counterclockwise",
        180: "videoflip method=rotate-180",
        270: "videoflip method=clockwise",
    }[rotation]


def display_resolution_for_rotation(resolution: CameraResolution, rotation_degrees: int) -> CameraResolution:
    if normalize_rotation_degrees(rotation_degrees) in {90, 270}:
        return CameraResolution(width=resolution.height, height=resolution.width)
    return resolution


def normalize_rotation_degrees(rotation_degrees: int) -> int:
    rotation = rotation_degrees % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("camera rotation must be one of 0, 90, 180, or 270 degrees")
    return rotation


def _as_resolution(resolution: CameraResolution | tuple[int, int] | str) -> CameraResolution:
    return CameraResolution() if resolution == "" else parse_camera_resolution(resolution)
