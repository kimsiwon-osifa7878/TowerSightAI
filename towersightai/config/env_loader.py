from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from towersightai.config.settings import CameraRole, Settings

REQUIRED_SETTINGS_KEYS = (
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

    hailo_apps_workspace = values.get("HAILO_APPS_WORKSPACE", "~/hailo-apps")
    hailo_apps_resources = values.get("HAILO_APPS_RESOURCES", f"{hailo_apps_workspace}/resources")
    hailo_arch = values.get("HAILO_ARCH", "hailo8")
    hailo_model_dir = values.get("HAILO_MODEL_DIR", f"{hailo_apps_resources}/models/{hailo_arch}")
    return Settings(
        app_env=values.get("APP_ENV", "development"),
        log_level=values.get("LOG_LEVEL", "INFO"),
        tappas_workspace=_expand_config_path(values.get("TAPPAS_WORKSPACE", hailo_apps_workspace), values),
        hailo_apps_workspace=_expand_config_path(hailo_apps_workspace, values),
        hailo_apps_resources=_expand_config_path(hailo_apps_resources, values),
        hailo_apps_python=_expand_config_path(
            values.get("HAILO_APPS_PYTHON", f"{hailo_apps_workspace}/venv_hailo_apps/bin/python"),
            values,
        ),
        tappas_postproc_path=(
            _expand_config_path(values["TAPPAS_POSTPROC_PATH"], values)
            if values.get("TAPPAS_POSTPROC_PATH")
            else None
        ),
        hailo_arch=hailo_arch,
        hailo_model_dir=_expand_config_path(hailo_model_dir, values),
        hailo_hef_path=_expand_config_path(values["HAILO_HEF_PATH"], values),
        hailo_postprocess_so=_expand_config_path(values["HAILO_POSTPROCESS_SO"], values),
        hailo_vehicle_detection_hef_path=_expand_config_path(
            values.get("HAILO_VEHICLE_DETECTION_HEF_PATH", values["HAILO_HEF_PATH"]),
            values,
        ),
        hailo_vehicle_detection_config_path=_expand_config_path(
            values.get("HAILO_VEHICLE_DETECTION_CONFIG_PATH", ""),
            values,
        ),
        hailo_vehicle_detection_postprocess_so=_expand_config_path(
            values.get(
                "HAILO_VEHICLE_DETECTION_POSTPROCESS_SO",
                values["HAILO_POSTPROCESS_SO"],
            ),
            values,
        ),
        hailo_person_presence_hef_path=_expand_config_path(
            values.get("HAILO_PERSON_PRESENCE_HEF_PATH", values["HAILO_HEF_PATH"]),
            values,
        ),
        hailo_person_presence_config_path=_expand_config_path(
            values.get("HAILO_PERSON_PRESENCE_CONFIG_PATH", ""),
            values,
        ),
        hailo_person_presence_postprocess_so=_expand_config_path(
            values.get("HAILO_PERSON_PRESENCE_POSTPROCESS_SO", values["HAILO_POSTPROCESS_SO"]),
            values,
        ),
        hailo_person_presence_crop_so=_expand_config_path(
            values.get("HAILO_PERSON_PRESENCE_CROP_SO", ""),
            values,
        ),
        fast_alpr_detector_model=values.get("FAST_ALPR_DETECTOR_MODEL", "yolo-v9-t-384-license-plate-end2end"),
        fast_alpr_ocr_model=values.get("FAST_ALPR_OCR_MODEL", "cct-xs-v2-global-model"),
        hailo_network_name=values.get("HAILO_NETWORK_NAME", "filter_letterbox"),
        camera_1=_camera_dict(values, 1),
        camera_2=_camera_dict(values, 2),
        camera_3=_camera_dict(values, 3),
        camera_4=_camera_dict(values, 4),
        calibration_path=_expand_config_path(values["CALIBRATION_PATH"], values),
        plc_endpoint=values["PLC_ENDPOINT"],
        ui_fullscreen=_parse_bool(values.get("UI_FULLSCREEN", "true")),
        ui_camera_resolution=values.get("UI_CAMERA_RESOLUTION", "1280x720"),
        birdview_mode=values.get("BIRDVIEW_MODE", "ceiling"),
        ld2410={
            "enabled": _parse_bool(values.get("LD2410_TCP_ENABLED", "false")),
            "bind_host": values.get("LD2410_TCP_BIND_HOST", "0.0.0.0"),
            "port": _parse_int(values.get("LD2410_TCP_PORT", "9000"), "LD2410_TCP_PORT"),
            "buffer_seconds": _parse_float(
                values.get("LD2410_BUFFER_SECONDS", "30"),
                "LD2410_BUFFER_SECONDS",
            ),
            "max_sample_age_seconds": _parse_float(
                values.get("LD2410_MAX_SAMPLE_AGE_SECONDS", "1"),
                "LD2410_MAX_SAMPLE_AGE_SECONDS",
            ),
            "client_idle_timeout_seconds": _parse_float(
                values.get("LD2410_CLIENT_IDLE_TIMEOUT_SECONDS", "5"),
                "LD2410_CLIENT_IDLE_TIMEOUT_SECONDS",
            ),
        },
        raw_storage={
            "enabled": _parse_bool(values.get("RAW_DATA_ENABLED", "false")),
            "local_dir": _expand_config_path(values.get("RAW_DATA_LOCAL_DIR", "artifacts/raw"), values),
            "sample_interval_seconds": _parse_float(
                values.get("RAW_DATA_SAMPLE_INTERVAL_SECONDS", "0.5"),
                "RAW_DATA_SAMPLE_INTERVAL_SECONDS",
            ),
            "person_stale_seconds": _parse_float(
                values.get("RAW_DATA_PERSON_STALE_SECONDS", "1.0"),
                "RAW_DATA_PERSON_STALE_SECONDS",
            ),
            "person_clear_grace_seconds": _parse_float(
                values.get("RAW_DATA_PERSON_CLEAR_GRACE_SECONDS", "5.0"),
                "RAW_DATA_PERSON_CLEAR_GRACE_SECONDS",
            ),
            "retention_days": _parse_int(values.get("RAW_DATA_RETENTION_DAYS", "14"), "RAW_DATA_RETENTION_DAYS"),
            "sync_interval_seconds": _parse_float(
                values.get("RAW_DATA_SYNC_INTERVAL_SECONDS", "300"),
                "RAW_DATA_SYNC_INTERVAL_SECONDS",
            ),
            "timezone_name": values.get("RAW_DATA_TIMEZONE", "Asia/Seoul"),
            "shard_minutes": _parse_int(values.get("RAW_DATA_SHARD_MINUTES", "60"), "RAW_DATA_SHARD_MINUTES"),
            "media_enabled": _parse_bool(values.get("RAW_MEDIA_ENABLED", "false")),
            "media_snapshot_jpeg_quality": _parse_int(
                values.get("RAW_MEDIA_SNAPSHOT_JPEG_QUALITY", "85"),
                "RAW_MEDIA_SNAPSHOT_JPEG_QUALITY",
            ),
            "media_frame_max_age_seconds": _parse_float(
                values.get("RAW_MEDIA_FRAME_MAX_AGE_SECONDS", "1"),
                "RAW_MEDIA_FRAME_MAX_AGE_SECONDS",
            ),
            "media_pre_seconds": _parse_float(values.get("RAW_MEDIA_PRE_SECONDS", "5"), "RAW_MEDIA_PRE_SECONDS"),
            "media_vehicle_post_seconds": _parse_float(
                values.get("RAW_MEDIA_VEHICLE_POST_SECONDS", "10"),
                "RAW_MEDIA_VEHICLE_POST_SECONDS",
            ),
            "media_segment_seconds": _parse_float(
                values.get("RAW_MEDIA_SEGMENT_SECONDS", "2"),
                "RAW_MEDIA_SEGMENT_SECONDS",
            ),
            "media_clip_part_seconds": _parse_float(
                values.get("RAW_MEDIA_CLIP_PART_SECONDS", "300"),
                "RAW_MEDIA_CLIP_PART_SECONDS",
            ),
            "media_gstreamer_python": _expand_config_path(
                values.get("RAW_MEDIA_GSTREAMER_PYTHON", "/usr/bin/python3"), values
            ),
            "nas_host": values.get("SYNOLOGY_NAS_HOST", ""),
            "nas_port": _parse_int(values.get("SYNOLOGY_NAS_PORT", "22"), "SYNOLOGY_NAS_PORT"),
            "nas_username": values.get("SYNOLOGY_NAS_ID", ""),
            "nas_password": values.get("SYNOLOGY_NAS_PW", ""),
            "nas_folder": values.get("SYNOLOGY_NAS_FOLDER", ""),
            "known_hosts_path": _expand_config_path(
                values.get("SYNOLOGY_NAS_KNOWN_HOSTS", "~/.ssh/known_hosts"), values
            ),
        },
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
        "rotation_degrees": values.get(f"CAMERA_{index}_ROTATION_DEGREES", "0"),
        "record_rtsp_url": values.get(f"CAMERA_{index}_RECORD_RTSP_URL") or None,
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


def _parse_float(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value for {name}: {value}") from exc


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {name}: {value}") from exc


def _expand_config_path(value: str, values: Mapping[str, str]) -> Path:
    expanded = _expand_mapping_variables(value, values)
    return Path(expanded).expanduser()


def _expand_mapping_variables(value: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        braced_name, plain_name = match.groups()
        name = braced_name or plain_name
        return values.get(name, match.group(0))

    expanded = value
    for _ in range(10):
        next_value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, expanded)
        if next_value == expanded:
            return expanded
        expanded = next_value
    return expanded


def _redact_rtsp(rtsp_url: str) -> str:
    return re.sub(r"//([^:/]+):([^@]+)@", r"//***:***@", rtsp_url)
