from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from towersightai.config.settings import CameraRole, Settings

REQUIRED_SETTINGS_KEYS = (
    "TAPPAS_WORKSPACE",
    "HAILO_HEF_PATH",
    "HAILO_POSTPROCESS_SO",
    "CALIBRATION_PATH",
    "PLC_ENDPOINT",
)

CAMERA_FIELDS = ("ID", "ROLE", "RTSP_URL")


@dataclass(frozen=True)
class CameraInspection:
    index: int
    id: str | None
    role: str | None
    rtsp_url: str | None
    configured: bool
    missing_fields: tuple[str, ...]
    redacted_rtsp_url: str | None


@dataclass(frozen=True)
class ConfigInspectionResult:
    env_path: Path
    env_exists: bool
    settings_loadable: bool
    settings_error: str | None
    missing_settings: tuple[str, ...]
    cameras: tuple[CameraInspection, ...]

    @property
    def configured_cameras(self) -> tuple[CameraInspection, ...]:
        return tuple(camera for camera in self.cameras if camera.configured)


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Parse a simple dotenv file without mutating process environment."""
    values: dict[str, str] = {}
    if not env_path.exists():
        raise FileNotFoundError(f"Environment file does not exist: {env_path}")

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: empty key")
        values[key] = _strip_optional_quotes(value.strip())
    return values


def load_settings_from_env(env_path: Path = Path(".env")) -> Settings:
    values = parse_env_file(env_path)
    return settings_from_mapping(values)


def settings_from_mapping(values: Mapping[str, str]) -> Settings:
    missing = [key for key in REQUIRED_SETTINGS_KEYS if not values.get(key)]
    for index in range(1, 5):
        for field in CAMERA_FIELDS:
            key = f"CAMERA_{index}_{field}"
            if not values.get(key):
                missing.append(key)
    if missing:
        raise ValueError(f"Missing required settings: {', '.join(missing)}")

    return Settings(
        app_env=values.get("APP_ENV", "development"),
        log_level=values.get("LOG_LEVEL", "INFO"),
        tappas_workspace=Path(values["TAPPAS_WORKSPACE"]),
        hailo_hef_path=Path(values["HAILO_HEF_PATH"]),
        hailo_postprocess_so=Path(values["HAILO_POSTPROCESS_SO"]),
        hailo_network_name=values.get("HAILO_NETWORK_NAME", "yolov5"),
        camera_1=_camera_dict(values, 1),
        camera_2=_camera_dict(values, 2),
        camera_3=_camera_dict(values, 3),
        camera_4=_camera_dict(values, 4),
        calibration_path=Path(values["CALIBRATION_PATH"]),
        plc_endpoint=values["PLC_ENDPOINT"],
        ui_fullscreen=_parse_bool(values.get("UI_FULLSCREEN", "true")),
    )


def inspect_env(env_path: Path = Path(".env")) -> ConfigInspectionResult:
    try:
        values = parse_env_file(env_path)
    except FileNotFoundError as exc:
        return ConfigInspectionResult(
            env_path=env_path,
            env_exists=False,
            settings_loadable=False,
            settings_error=str(exc),
            missing_settings=REQUIRED_SETTINGS_KEYS,
            cameras=tuple(_inspect_camera({}, index) for index in range(1, 5)),
        )

    missing_settings = tuple(key for key in REQUIRED_SETTINGS_KEYS if not values.get(key))
    settings_error: str | None = None
    settings_loadable = False
    try:
        settings_from_mapping(values)
        settings_loadable = True
    except Exception as exc:  # noqa: BLE001 - inspection must return a report, not crash the CLI.
        settings_error = str(exc)

    return ConfigInspectionResult(
        env_path=env_path,
        env_exists=True,
        settings_loadable=settings_loadable,
        settings_error=settings_error,
        missing_settings=missing_settings,
        cameras=tuple(_inspect_camera(values, index) for index in range(1, 5)),
    )


def _camera_dict(values: Mapping[str, str], index: int) -> dict[str, str | None]:
    return {
        "id": values[f"CAMERA_{index}_ID"],
        "role": CameraRole(values[f"CAMERA_{index}_ROLE"]).value,
        "rtsp_url": values[f"CAMERA_{index}_RTSP_URL"],
        "username": values.get(f"CAMERA_{index}_USERNAME") or None,
        "password": values.get(f"CAMERA_{index}_PASSWORD") or None,
    }


def _inspect_camera(values: Mapping[str, str], index: int) -> CameraInspection:
    prefix = f"CAMERA_{index}_"
    missing = tuple(f"{prefix}{field}" for field in CAMERA_FIELDS if not values.get(f"{prefix}{field}"))
    rtsp_url = values.get(f"{prefix}RTSP_URL") or None
    return CameraInspection(
        index=index,
        id=values.get(f"{prefix}ID") or None,
        role=values.get(f"{prefix}ROLE") or None,
        rtsp_url=rtsp_url,
        configured=not missing,
        missing_fields=missing,
        redacted_rtsp_url=_redact_rtsp(rtsp_url) if rtsp_url else None,
    )


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _redact_rtsp(rtsp_url: str) -> str:
    return re.sub(r"//([^:/]+):([^@]+)@", r"//***:***@", rtsp_url)
