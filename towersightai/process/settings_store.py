"""Operator-tunable runtime settings for the parking-process engine.

These values are field-tuning knobs (detection thresholds, debounce counts,
guide geometry, NAS upload mode) that the operator adjusts from the console —
deliberately outside ``.env`` so a site tweak never requires editing config
files or restarting with new environment. A missing or corrupt file always
falls back to validated defaults: settings load failures must never crash the
safety UI and never relax any safety behavior.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
DEFAULT_SETTINGS_PATH = Path("data/operator-settings.json")

_LOGGER = logging.getLogger("towersightai.process.settings")

NAS_UPLOAD_MODES = ("scheduled", "immediate")


@dataclass(frozen=True)
class VehicleTriggerSettings:
    """Entry trigger on the opposite_side camera."""

    min_confidence: float = 0.6
    consecutive_frames: int = 5
    release_seconds: float = 5.0
    stale_seconds: float = 1.5

    def __post_init__(self) -> None:
        if not 0.0 < self.min_confidence <= 1.0:
            raise ValueError("vehicle_trigger.min_confidence must be in (0, 1]")
        if self.consecutive_frames < 1:
            raise ValueError("vehicle_trigger.consecutive_frames must be >= 1")
        if self.release_seconds <= 0:
            raise ValueError("vehicle_trigger.release_seconds must be > 0")
        if self.stale_seconds <= 0:
            raise ValueError("vehicle_trigger.stale_seconds must be > 0")


@dataclass(frozen=True)
class PersonDebounceSettings:
    """Consecutive-detection debounce for person alerts."""

    idle_frames: int = 2
    parked_frames: int = 2
    stale_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.idle_frames < 1:
            raise ValueError("person_debounce.idle_frames must be >= 1")
        if self.parked_frames < 1:
            raise ValueError("person_debounce.parked_frames must be >= 1")
        if self.stale_seconds <= 0:
            raise ValueError("person_debounce.stale_seconds must be > 0")


@dataclass(frozen=True)
class PlateZoneSettings:
    """Front-camera vehicle-entry line and plate majority vote.

    ``line_y_norm`` is the 차량진입선 (vehicle-entry line) near the top of the
    front camera: a plate recognized *below* it means a vehicle is entering, and
    only those reads are collected for the majority vote.
    """

    line_y_norm: float = 0.40
    min_reads_for_vote: int = 3
    read_interval_seconds: float = 1.0
    max_reads: int = 10
    read_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.line_y_norm <= 1.0:
            raise ValueError("plate_zone.line_y_norm must be in [0, 1]")
        if self.min_reads_for_vote < 1:
            raise ValueError("plate_zone.min_reads_for_vote must be >= 1")
        if self.read_interval_seconds <= 0:
            raise ValueError("plate_zone.read_interval_seconds must be > 0")
        if self.max_reads < self.min_reads_for_vote:
            raise ValueError("plate_zone.max_reads must be >= min_reads_for_vote")
        if self.read_timeout_seconds <= 0:
            raise ValueError("plate_zone.read_timeout_seconds must be > 0")


@dataclass(frozen=True)
class WheelGuideSettings:
    """Trapezoidal wheel-guide overlay on the front camera (normalized).

    Seen from the front camera the wheel paths converge with distance, so the
    guide is a trapezoid: wide at the bottom (near the camera) and narrow at the
    top (``top_y_norm``). Bottom corners run at the frame bottom; ``stop_y_norm``
    is the horizontal stop bar between the two guides.
    """

    left_x_norm: float = 0.30       # bottom-left (wide, near camera)
    right_x_norm: float = 0.70      # bottom-right (wide)
    top_left_x_norm: float = 0.42   # top-left (narrow, far)
    top_right_x_norm: float = 0.58  # top-right (narrow)
    top_y_norm: float = 0.45        # trapezoid top edge height
    stop_y_norm: float = 0.80       # horizontal stop line

    def __post_init__(self) -> None:
        for name in (
            "left_x_norm",
            "right_x_norm",
            "top_left_x_norm",
            "top_right_x_norm",
            "top_y_norm",
            "stop_y_norm",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"wheel_guides.{name} must be in [0, 1]")
        if not self.left_x_norm <= self.top_left_x_norm < self.top_right_x_norm <= self.right_x_norm:
            raise ValueError(
                "wheel_guides must narrow toward the top (left ≤ top_left < top_right ≤ right)"
            )
        if not self.top_y_norm < self.stop_y_norm:
            raise ValueError("wheel_guides.top_y_norm must be above stop_y_norm")


@dataclass(frozen=True)
class AlignmentSettings:
    """Parked decision from front-camera vehicle bbox stability (no position AI yet)."""

    stop_stable_seconds: float = 5.0
    motion_epsilon_norm: float = 0.03
    parked_instruct_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.stop_stable_seconds <= 0:
            raise ValueError("alignment.stop_stable_seconds must be > 0")
        if not 0.0 < self.motion_epsilon_norm <= 0.5:
            raise ValueError("alignment.motion_epsilon_norm must be in (0, 0.5]")
        if self.parked_instruct_seconds <= 0:
            raise ValueError("alignment.parked_instruct_seconds must be > 0")


@dataclass(frozen=True)
class ProcessTimerSettings:
    """Timers standing in for missing PLC signals (assumptions, not authorization)."""

    exit_clear_seconds: float = 10.0
    machine_operation_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.exit_clear_seconds <= 0:
            raise ValueError("timers.exit_clear_seconds must be > 0")
        if self.machine_operation_seconds <= 0:
            raise ValueError("timers.machine_operation_seconds must be > 0")


@dataclass(frozen=True)
class OperatorRuntimeSettings:
    vehicle_trigger: VehicleTriggerSettings = field(default_factory=VehicleTriggerSettings)
    person_debounce: PersonDebounceSettings = field(default_factory=PersonDebounceSettings)
    plate_zone: PlateZoneSettings = field(default_factory=PlateZoneSettings)
    wheel_guides: WheelGuideSettings = field(default_factory=WheelGuideSettings)
    alignment: AlignmentSettings = field(default_factory=AlignmentSettings)
    timers: ProcessTimerSettings = field(default_factory=ProcessTimerSettings)
    nas_upload_mode: str = "scheduled"
    audio_enabled: bool = True

    def __post_init__(self) -> None:
        if self.nas_upload_mode not in NAS_UPLOAD_MODES:
            raise ValueError(f"nas_upload_mode must be one of {NAS_UPLOAD_MODES}")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        return payload


def _section(mapping: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _build_section(cls: type, values: Mapping[str, Any]):  # noqa: ANN202 - dataclass factory
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {key: value for key, value in values.items() if key in known}
    return cls(**kwargs)


def settings_from_payload(payload: Mapping[str, Any]) -> OperatorRuntimeSettings:
    """Build settings from a JSON payload. Unknown keys ignored, missing keys defaulted.

    Raises ValueError/TypeError when present values are invalid — callers that must
    never fail should use load_operator_settings instead.
    """
    nas_upload_mode = payload.get("nas_upload_mode", "scheduled")
    audio_enabled = payload.get("audio_enabled", True)
    return OperatorRuntimeSettings(
        vehicle_trigger=_build_section(VehicleTriggerSettings, _section(payload, "vehicle_trigger")),
        person_debounce=_build_section(PersonDebounceSettings, _section(payload, "person_debounce")),
        plate_zone=_build_section(PlateZoneSettings, _section(payload, "plate_zone")),
        wheel_guides=_build_section(WheelGuideSettings, _section(payload, "wheel_guides")),
        alignment=_build_section(AlignmentSettings, _section(payload, "alignment")),
        timers=_build_section(ProcessTimerSettings, _section(payload, "timers")),
        nas_upload_mode=nas_upload_mode if isinstance(nas_upload_mode, str) else "scheduled",
        audio_enabled=bool(audio_enabled),
    )


def load_operator_settings(path: Path = DEFAULT_SETTINGS_PATH) -> OperatorRuntimeSettings:
    """Load settings; missing/corrupt/invalid file → validated defaults + warning log."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return OperatorRuntimeSettings()
    except OSError as exc:
        _LOGGER.warning("operator settings unreadable (%s): defaults applied — %s", path, exc)
        return OperatorRuntimeSettings()
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("settings root must be a JSON object")
        return settings_from_payload(payload)
    except (ValueError, TypeError) as exc:
        _LOGGER.warning("operator settings invalid (%s): defaults applied — %s", path, exc)
        return OperatorRuntimeSettings()


def save_operator_settings(
    settings: OperatorRuntimeSettings, path: Path = DEFAULT_SETTINGS_PATH
) -> None:
    """Atomically persist settings (tmp write + fsync + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as fp:
        json.dump(settings.to_payload(), fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    temporary.replace(target)


__all__ = [
    "AlignmentSettings",
    "DEFAULT_SETTINGS_PATH",
    "NAS_UPLOAD_MODES",
    "OperatorRuntimeSettings",
    "PersonDebounceSettings",
    "PlateZoneSettings",
    "ProcessTimerSettings",
    "SCHEMA_VERSION",
    "VehicleTriggerSettings",
    "WheelGuideSettings",
    "load_operator_settings",
    "save_operator_settings",
    "settings_from_payload",
]
