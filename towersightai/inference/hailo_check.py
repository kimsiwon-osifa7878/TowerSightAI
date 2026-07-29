from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from towersightai.config.settings import Settings

HAILO_GSTREAMER_ELEMENTS = (
    "hailonet",
    "hailofilter",
    "hailoroundrobin",
    "hailostreamrouter",
)


@dataclass(frozen=True)
class HailoCheckItem:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class HailoCheckResult:
    items: tuple[HailoCheckItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)


def check_hailo_installation(settings: Settings, *, timeout_seconds: int = 10) -> HailoCheckResult:
    items: list[HailoCheckItem] = []
    items.append(_check_executable("hailortcli"))
    items.append(_check_hailort_identify(timeout_seconds=timeout_seconds))
    for element in HAILO_GSTREAMER_ELEMENTS:
        items.append(_check_gstreamer_element(element, timeout_seconds=timeout_seconds))
    items.append(_check_path("Hailo Apps workspace", settings.hailo_apps_workspace))
    items.append(_check_path("Hailo Apps resources", settings.hailo_apps_resources))
    items.append(_check_path("Hailo Apps Python", settings.hailo_apps_python))
    items.append(_check_stream_id_helper(settings))
    items.append(_check_path("HEF path", settings.hailo_hef_path))
    items.append(_check_path("postprocess so", settings.hailo_postprocess_so))
    items.append(_check_path("vehicle HEF", settings.hailo_vehicle_detection_hef_path))
    items.append(_check_path("vehicle postprocess", settings.hailo_vehicle_detection_postprocess_so))
    items.append(_check_path("person HEF", settings.hailo_person_presence_hef_path))
    items.append(_check_path("person postprocess", settings.hailo_person_presence_postprocess_so))
    return HailoCheckResult(tuple(items))


def _check_executable(name: str) -> HailoCheckItem:
    path = shutil.which(name)
    if path is None:
        return HailoCheckItem(name, False, f"{name} not found in PATH")
    return HailoCheckItem(name, True, path)


def _check_hailort_identify(*, timeout_seconds: int) -> HailoCheckItem:
    if shutil.which("hailortcli") is None:
        return HailoCheckItem("hailort identify", False, "hailortcli not found in PATH")
    return _run_check(
        "hailort identify",
        ["hailortcli", "fw-control", "identify"],
        timeout_seconds=timeout_seconds,
    )


def _check_gstreamer_element(element: str, *, timeout_seconds: int) -> HailoCheckItem:
    if shutil.which("gst-inspect-1.0") is None:
        return HailoCheckItem(element, False, "gst-inspect-1.0 not found in PATH")
    return _run_check(element, ["gst-inspect-1.0", element], timeout_seconds=timeout_seconds)


def _check_path(name: str, path: Path) -> HailoCheckItem:
    if path.exists():
        return HailoCheckItem(name, True, str(path))
    return HailoCheckItem(name, False, f"missing: {path}")


def _check_stream_id_helper(settings: Settings) -> HailoCheckItem:
    candidates = []
    configured_dir = str(settings.tappas_postproc_path) if settings.tappas_postproc_path is not None else None
    if not configured_dir:
        configured_dir = os.environ.get("TAPPAS_POSTPROC_PATH")
    if not configured_dir:
        configured_dir = _dotenv_value(settings.hailo_apps_resources / ".env", "TAPPAS_POSTPROC_PATH")
    if configured_dir:
        candidates.append(Path(configured_dir).expanduser() / "libstream_id_tool.so")
    candidates.extend(
        (
            settings.hailo_apps_resources / "so" / "libstream_id_tool.so",
            settings.hailo_apps_resources / "libstream_id_tool.so",
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return HailoCheckItem("stream-id helper", True, str(candidate))
    if settings.hailo_apps_resources.is_dir():
        match = next(settings.hailo_apps_resources.rglob("libstream_id_tool.so"), None)
        if match is not None:
            return HailoCheckItem("stream-id helper", True, str(match))
    return HailoCheckItem(
        "stream-id helper",
        False,
        "libstream_id_tool.so missing; source ~/hailo-apps/setup_env.sh or run hailo-post-install",
    )


def _dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        current_key, value = line.split("=", 1)
        if current_key.strip().upper() == key:
            return value.strip().strip("'\"")
    return None


def _run_check(name: str, command: Sequence[str], *, timeout_seconds: int) -> HailoCheckItem:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return HailoCheckItem(name, False, f"timed out after {timeout_seconds}s")
    except OSError as exc:
        return HailoCheckItem(name, False, str(exc))

    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    detail = output[0] if output else f"exit code {completed.returncode}"
    return HailoCheckItem(name, completed.returncode == 0, detail)
