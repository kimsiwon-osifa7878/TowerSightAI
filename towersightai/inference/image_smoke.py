from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from towersightai.config.settings import Settings

NETWORK_WIDTH = 640
NETWORK_HEIGHT = 640
NETWORK_FORMAT = "RGB"
SAMPLE_IMAGE_BUFFERS = 3
SAMPLE_IMAGE_FRAMERATE = "1/1"
DEFAULT_OUTPUT_IMAGE = Path("artifacts/hailo/sample-detection.png")
HAILO_CALLBACK_MODULE = Path("towersightai/inference/callback.py")


@dataclass(frozen=True)
class HailoImageSmokeResult:
    ok: bool
    command: tuple[str, ...]
    event_path: Path
    output_image: Path | None
    stdout: str
    stderr: str
    reason: str | None = None


def build_image_hailo_pipeline(
    settings: Settings,
    *,
    image_path: Path,
    output_image_path: Path | None = DEFAULT_OUTPUT_IMAGE,
    display: bool = False,
    show_fps: bool = False,
    min_confidence: float = 0.3,
) -> str:
    sink = _sink_segment(output_image_path=output_image_path, display=display, show_fps=show_fps)
    return " ".join(
        (
            f"filesrc location={image_path}",
            "! decodebin",
            f"! imagefreeze num-buffers={SAMPLE_IMAGE_BUFFERS} is-live=true",
            f"! video/x-raw,framerate={SAMPLE_IMAGE_FRAMERATE}",
            "! queue name=queue_scale max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! videoscale n-threads=2",
            "! queue name=queue_src_convert max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! videoconvert n-threads=3",
            f"! video/x-raw,format={NETWORK_FORMAT},width={NETWORK_WIDTH},height={NETWORK_HEIGHT},pixel-aspect-ratio=1/1",
            "! queue name=queue_hailonet max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailonet hef-path={settings.hailo_hef_path} batch-size=1 nms-score-threshold={min_confidence} "
            "nms-iou-threshold=0.45 output-format-type=HAILO_FORMAT_TYPE_FLOAT32",
            "! queue name=queue_hailofilter max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so} qos=false",
            "! queue name=queue_hailopython max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            f"! hailopython module={HAILO_CALLBACK_MODULE} qos=false",
            "! queue name=queue_hailooverlay max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! hailooverlay",
            "! queue name=queue_output max-size-buffers=5 max-size-bytes=0 max-size-time=0",
            "! videoconvert n-threads=3",
            sink,
        )
    )


def image_smoke_command(
    settings: Settings,
    *,
    image_path: Path,
    output_image_path: Path | None = DEFAULT_OUTPUT_IMAGE,
    display: bool = False,
    show_fps: bool = False,
    min_confidence: float = 0.3,
    gst_launch: str = "gst-launch-1.0",
) -> list[str]:
    pipeline = build_image_hailo_pipeline(
        settings,
        image_path=image_path,
        output_image_path=output_image_path,
        display=display,
        show_fps=show_fps,
        min_confidence=min_confidence,
    )
    return [gst_launch, "-q", *pipeline.split()]


def run_image_hailo_smoke(
    settings: Settings,
    *,
    image_path: Path,
    event_path: Path,
    output_image_path: Path | None = DEFAULT_OUTPUT_IMAGE,
    camera_id: str = "sample_image",
    display: bool = False,
    show_fps: bool = False,
    min_confidence: float = 0.3,
    timeout_seconds: int = 30,
    gst_launch: str = "gst-launch-1.0",
    require_opt_in: bool = True,
) -> HailoImageSmokeResult:
    command = tuple(
        image_smoke_command(
            settings,
            image_path=image_path,
            output_image_path=output_image_path,
            display=display,
            show_fps=show_fps,
            min_confidence=min_confidence,
            gst_launch=gst_launch,
        )
    )
    if require_opt_in and os.environ.get("RUN_HARDWARE_TESTS") != "1":
        return HailoImageSmokeResult(False, command, event_path, output_image_path, "", "", "RUN_HARDWARE_TESTS=1 is required")
    if shutil.which(gst_launch) is None:
        return HailoImageSmokeResult(False, command, event_path, output_image_path, "", "", f"{gst_launch} not found")
    if not image_path.exists():
        return HailoImageSmokeResult(False, command, event_path, output_image_path, "", "", f"missing sample image: {image_path}")

    event_path.parent.mkdir(parents=True, exist_ok=True)
    if event_path.exists():
        event_path.unlink()
    if output_image_path is not None:
        output_image_path.parent.mkdir(parents=True, exist_ok=True)

    env = _gst_runtime_env(settings)
    env.update(
        {
            "TOWERSIGHTAI_HAILO_CAMERA_ID": camera_id,
            "TOWERSIGHTAI_HAILO_EVENT_PATH": str(event_path),
            "TOWERSIGHTAI_HAILO_MIN_CONFIDENCE": str(min_confidence),
        }
    )
    try:
        completed = _run_gst_command(command, timeout_seconds=timeout_seconds, env=env)
    except subprocess.TimeoutExpired as exc:
        return HailoImageSmokeResult(
            False,
            command,
            event_path,
            output_image_path,
            exc.stdout or "",
            exc.stderr or "",
            f"gst-launch timed out after {timeout_seconds} seconds",
        )
    ok = completed.returncode == 0 and event_path.exists()
    reason = None if ok else f"gst-launch exit code {completed.returncode}; detection event file missing or pipeline failed"
    return HailoImageSmokeResult(ok, command, event_path, output_image_path, completed.stdout, completed.stderr, reason)


def redacted_command(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def _run_gst_command(command: tuple[str, ...], *, timeout_seconds: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout or exc.output, stderr=stderr or exc.stderr) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()


def _gst_runtime_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    tappas_venv = settings.tappas_workspace / "hailo_tappas_venv"
    tappas_bin = tappas_venv / "bin"
    if tappas_bin.exists() and env.get("VIRTUAL_ENV") != str(tappas_venv):
        env["VIRTUAL_ENV"] = str(tappas_venv)
        env["PATH"] = f"{tappas_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _sink_segment(*, output_image_path: Path | None, display: bool, show_fps: bool) -> str:
    if display:
        return f"! fpsdisplaysink video-sink=autovideosink name=hailo_display sync=false text-overlay={str(show_fps).lower()}"
    if output_image_path is None:
        return "! fakesink sync=false"
    return f"! pngenc snapshot=true ! filesink location={output_image_path}"
