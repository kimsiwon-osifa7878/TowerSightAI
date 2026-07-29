from __future__ import annotations

import os
import sys
from pathlib import Path

from towersightai.config.settings import CameraConfig, Settings


VEHICLE_LABELS = ("car", "truck", "bus", "motorcycle")
PERSON_LABELS = ("person",)


def hailo_apps_detection_command(
    settings: Settings,
    cameras: tuple[CameraConfig, ...],
    *,
    event_path: Path,
    hef_path: Path,
    postprocess_so: Path,
    min_confidence: float,
    allowed_labels: tuple[str, ...] = (),
    camera_rotations: dict[str, int] | None = None,
) -> tuple[str, ...]:
    if not cameras:
        raise ValueError("At least one camera is required for Hailo Apps detection.")

    command = [
        str(settings.hailo_apps_python),
        "-m",
        "towersightai.cli.hailo_apps_detection",
        "--workspace",
        str(settings.hailo_apps_workspace),
        "--resources",
        str(settings.hailo_apps_resources),
        "--hef",
        str(hef_path),
        "--postprocess",
        str(postprocess_so),
        "--event-path",
        str(event_path),
        "--min-confidence",
        str(min_confidence),
        "--arch",
        settings.hailo_arch,
    ]
    for camera in cameras:
        command.extend(("--camera", f"{camera.id}={camera.rtsp_url}"))
        rotation = (camera_rotations or {}).get(camera.id, camera.rotation_degrees)
        command.extend(("--rotation", f"{camera.id}={rotation}"))
    for label in allowed_labels:
        command.extend(("--allowed-label", label))
    return tuple(command)


def hailo_apps_runtime_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    workspace = settings.hailo_apps_workspace.resolve(strict=False)
    resources = settings.hailo_apps_resources.resolve(strict=False)
    venv = settings.hailo_apps_python.parent.parent.resolve(strict=False)
    project_root = Path(__file__).resolve(strict=False).parents[2]

    python_path = [str(project_root), str(workspace)]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    env["PATH"] = os.pathsep.join((str(settings.hailo_apps_python.parent), env.get("PATH", "")))
    env["VIRTUAL_ENV"] = str(venv)
    env["HAILO_ARCH"] = settings.hailo_arch
    if settings.tappas_postproc_path is not None:
        postproc_path = str(settings.tappas_postproc_path.resolve(strict=False))
        env["TAPPAS_POSTPROC_PATH"] = postproc_path
        env["tappas_postproc_path"] = postproc_path

    hailo_env = resources / ".env"
    if hailo_env.is_file():
        env["HAILO_ENV_FILE"] = str(hailo_env)
        _merge_simple_env_file(env, hailo_env)
    return env


def _merge_simple_env_file(env: dict[str, str], path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            env.setdefault(key, value)
            env.setdefault(key.upper(), value)
