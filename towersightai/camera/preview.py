from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence


class CameraInspectionLike(Protocol):
    index: int
    id: str | None
    role: str | None
    rtsp_url: str | None
    configured: bool



@dataclass(frozen=True)
class CameraHealthResult:
    camera_id: str
    role: str
    healthy: bool
    frame_received: bool
    error: str | None
    checked_at: datetime


def build_display_preview_pipeline(rtsp_url: str, latency_ms: int = 100) -> str:
    return (
        f"rtspsrc location={rtsp_url} latency={latency_ms} protocols=tcp ! "
        "rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
        "fpsdisplaysink video-sink=autovideosink sync=false text-overlay=true"
    )


def build_health_check_pipeline(rtsp_url: str, latency_ms: int = 100) -> str:
    return (
        f"rtspsrc location={rtsp_url} latency={latency_ms} protocols=tcp ! "
        "rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
        "video/x-raw,format=RGB ! fakesink sync=false num-buffers=1"
    )


def configured_camera_command(
    camera: CameraInspectionLike,
    *,
    preview: bool,
    latency_ms: int = 100,
    gst_launch: str = "gst-launch-1.0",
) -> list[str]:
    if not camera.configured or not camera.rtsp_url:
        raise ValueError(f"Camera {camera.index} is not fully configured")
    pipeline = (
        build_display_preview_pipeline(camera.rtsp_url, latency_ms=latency_ms)
        if preview
        else build_health_check_pipeline(camera.rtsp_url, latency_ms=latency_ms)
    )
    return [gst_launch, "-q", *pipeline.split()]


def run_camera_health_check(
    camera: CameraInspectionLike,
    *,
    timeout_seconds: int = 10,
    latency_ms: int = 100,
    gst_launch: str = "gst-launch-1.0",
) -> CameraHealthResult:
    checked_at = datetime.now(timezone.utc)
    camera_id = camera.id or f"camera_{camera.index}"
    role = camera.role or "unknown"

    if not camera.configured or not camera.rtsp_url:
        return CameraHealthResult(camera_id, role, False, False, "camera is not fully configured", checked_at)
    if shutil.which(gst_launch) is None:
        return CameraHealthResult(camera_id, role, False, False, f"{gst_launch} not found", checked_at)

    command = configured_camera_command(camera, preview=False, latency_ms=latency_ms, gst_launch=gst_launch)
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return CameraHealthResult(camera_id, role, False, False, "timed out waiting for a frame", checked_at)
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or "GStreamer health check failed").strip()
        return CameraHealthResult(camera_id, role, False, False, error, checked_at)

    return CameraHealthResult(camera_id, role, True, True, None, checked_at)


def launch_camera_previews(
    cameras: Sequence[CameraInspectionLike],
    *,
    latency_ms: int = 100,
    gst_launch: str = "gst-launch-1.0",
) -> list[subprocess.Popen[str]]:
    if shutil.which(gst_launch) is None:
        raise FileNotFoundError(f"{gst_launch} not found")

    processes: list[subprocess.Popen[str]] = []
    for camera in cameras:
        if not camera.configured:
            continue
        command = configured_camera_command(camera, preview=True, latency_ms=latency_ms, gst_launch=gst_launch)
        processes.append(subprocess.Popen(command, text=True))
    return processes
