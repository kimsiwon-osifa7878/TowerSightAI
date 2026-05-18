from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from towersightai.config.settings import Settings

HAILO_GSTREAMER_ELEMENTS = (
    "hailonet",
    "hailofilter",
    "hailopython",
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
    items.append(_check_path("HEF path", settings.hailo_hef_path))
    items.append(_check_path("postprocess so", settings.hailo_postprocess_so))
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
