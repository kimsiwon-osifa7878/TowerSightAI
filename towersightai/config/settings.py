from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RawStorageConfig:
    enabled: bool = False
    local_dir: Path = Path("artifacts/raw")
    sample_interval_seconds: float = 0.5
    person_stale_seconds: float = 1.0
    person_clear_grace_seconds: float = 5.0
    retention_days: int = 14
    sync_interval_seconds: float = 300.0
    timezone_name: str = "Asia/Seoul"
    nas_host: str = ""
    nas_port: int = 22
    nas_username: str = ""
    nas_password: str = field(default="", repr=False)
    nas_folder: str = ""
    known_hosts_path: Path = Path("~/.ssh/known_hosts")

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_dir", Path(self.local_dir).expanduser())
        object.__setattr__(self, "known_hosts_path", Path(self.known_hosts_path).expanduser())
        if self.sample_interval_seconds <= 0:
            raise ValueError("RAW_DATA_SAMPLE_INTERVAL_SECONDS must be positive.")
        if self.person_stale_seconds < 0:
            raise ValueError("RAW_DATA_PERSON_STALE_SECONDS must be non-negative.")
        if self.person_clear_grace_seconds < 0:
            raise ValueError("RAW_DATA_PERSON_CLEAR_GRACE_SECONDS must be non-negative.")
        if self.retention_days < 1:
            raise ValueError("RAW_DATA_RETENTION_DAYS must be at least 1.")
        if self.sync_interval_seconds <= 0:
            raise ValueError("RAW_DATA_SYNC_INTERVAL_SECONDS must be positive.")
        try:
            ZoneInfo(self.timezone_name)
        except Exception as exc:
            raise ValueError(f"Invalid RAW_DATA_TIMEZONE: {self.timezone_name}") from exc
        if not 1 <= int(self.nas_port) <= 65535:
            raise ValueError("SYNOLOGY_NAS_PORT must be between 1 and 65535.")
        if self.enabled:
            missing = [
                name
                for name, value in (
                    ("SYNOLOGY_NAS_HOST", self.nas_host),
                    ("SYNOLOGY_NAS_ID", self.nas_username),
                    ("SYNOLOGY_NAS_PW", self.nas_password),
                    ("SYNOLOGY_NAS_FOLDER", self.nas_folder),
                )
                if not value
            ]
            if missing:
                raise ValueError("Missing enabled raw-storage settings: " + ", ".join(missing))
            if "://" in self.nas_host or "/" in self.nas_host:
                raise ValueError("SYNOLOGY_NAS_HOST must be a hostname without scheme, port, or path.")
            if ".." in PurePosixPath(self.nas_folder).parts:
                raise ValueError("SYNOLOGY_NAS_FOLDER must not contain '..'.")


class CameraRole(str, Enum):
    ceiling = "ceiling"
    front = "front"
    rear_side = "rear_side"
    opposite_side = "opposite_side"


class BirdviewMode(str, Enum):
    disabled = "disabled"
    ceiling = "ceiling"


@dataclass(frozen=True)
class CameraConfig:
    id: str
    role: CameraRole
    rtsp_url: str
    username: str | None = None
    password: str | None = None
    rotation_degrees: int = 0

    def __post_init__(self) -> None:
        rotation = int(self.rotation_degrees) % 360
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("Camera rotation must be one of 0, 90, 180, or 270 degrees.")
        object.__setattr__(self, "rotation_degrees", rotation)


@dataclass(frozen=True)
class CameraResolution:
    width: int = 1280
    height: int = 720

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Camera resolution width and height must be positive.")

    @property
    def caps(self) -> str:
        return f"width={self.width},height={self.height}"


def parse_camera_resolution(value: CameraResolution | tuple[int, int] | str) -> CameraResolution:
    if isinstance(value, CameraResolution):
        return value
    if isinstance(value, tuple):
        width, height = value
        return CameraResolution(width=width, height=height)
    normalized = value.lower().strip()
    if "x" not in normalized:
        raise ValueError("Camera resolution must use WIDTHxHEIGHT format.")
    width_text, height_text = normalized.split("x", 1)
    try:
        return CameraResolution(width=int(width_text), height=int(height_text))
    except ValueError as exc:
        raise ValueError("Camera resolution must use WIDTHxHEIGHT format.") from exc



@dataclass
class Settings:
    tappas_workspace: Path
    hailo_hef_path: Path
    hailo_postprocess_so: Path
    camera_1: CameraConfig | dict
    camera_2: CameraConfig | dict
    camera_3: CameraConfig | dict
    camera_4: CameraConfig | dict
    calibration_path: Path
    plc_endpoint: str
    app_env: str = "development"
    log_level: str = "INFO"
    hailo_apps_workspace: Path = Path("~/hailo-apps")
    hailo_apps_resources: Path = Path("~/hailo-apps/resources")
    hailo_apps_python: Path = Path("~/hailo-apps/venv_hailo_apps/bin/python")
    tappas_postproc_path: Path | None = None
    hailo_arch: str = "hailo8"
    hailo_model_dir: Path = Path("~/hailo-apps/resources/models/hailo8")
    hailo_vehicle_detection_hef_path: Path = Path("~/hailo-apps/resources/models/hailo8/yolov8m.hef")
    hailo_vehicle_detection_config_path: Path = Path("")
    hailo_vehicle_detection_postprocess_so: Path = Path(
        "~/hailo-apps/resources/so/libyolo_hailortpp_postprocess.so"
    )
    hailo_person_presence_hef_path: Path = Path("~/hailo-apps/resources/models/hailo8/yolov8m.hef")
    hailo_person_presence_config_path: Path = Path("")
    hailo_person_presence_postprocess_so: Path = Path(
        "~/hailo-apps/resources/so/libyolo_hailortpp_postprocess.so"
    )
    hailo_person_presence_crop_so: Path = Path("")
    fast_alpr_detector_model: str = "yolo-v9-t-384-license-plate-end2end"
    fast_alpr_ocr_model: str = "cct-xs-v2-global-model"
    hailo_network_name: str = "filter_letterbox"
    ui_fullscreen: bool = True
    ui_camera_resolution: CameraResolution | tuple[int, int] | str = CameraResolution()
    birdview_mode: BirdviewMode | str = BirdviewMode.ceiling
    raw_storage: RawStorageConfig | dict | None = None

    def __post_init__(self) -> None:
        self.hailo_apps_workspace = self.hailo_apps_workspace.expanduser()
        self.hailo_apps_resources = self.hailo_apps_resources.expanduser()
        self.hailo_apps_python = self.hailo_apps_python.expanduser()
        if self.tappas_postproc_path is not None:
            self.tappas_postproc_path = self.tappas_postproc_path.expanduser()
        self.camera_1 = self._as_camera(self.camera_1)
        self.camera_2 = self._as_camera(self.camera_2)
        self.camera_3 = self._as_camera(self.camera_3)
        self.camera_4 = self._as_camera(self.camera_4)
        self.ui_camera_resolution = self._as_resolution(self.ui_camera_resolution)
        self.birdview_mode = BirdviewMode(self.birdview_mode)
        if self.raw_storage is None:
            self.raw_storage = RawStorageConfig()
        elif isinstance(self.raw_storage, dict):
            self.raw_storage = RawStorageConfig(**self.raw_storage)
        self._validate_safety_constraints()

    def _as_camera(self, camera: CameraConfig | dict) -> CameraConfig:
        if isinstance(camera, CameraConfig):
            return camera
        return CameraConfig(
            id=camera["id"],
            role=CameraRole(camera["role"]),
            rtsp_url=camera["rtsp_url"],
            username=camera.get("username"),
            password=camera.get("password"),
            rotation_degrees=int(camera.get("rotation_degrees", 0)),
        )

    def _as_resolution(self, resolution: CameraResolution | tuple[int, int] | str) -> CameraResolution:
        return parse_camera_resolution(resolution)

    @property
    def cameras(self) -> list[CameraConfig]:
        return [self.camera_1, self.camera_2, self.camera_3, self.camera_4]

    @property
    def active_cameras(self) -> list[CameraConfig]:
        if self.birdview_mode is BirdviewMode.disabled:
            return [camera for camera in self.cameras if camera.role is not CameraRole.ceiling]
        return self.cameras

    @property
    def birdview_enabled(self) -> bool:
        return self.birdview_mode is not BirdviewMode.disabled

    def _validate_safety_constraints(self) -> None:
        if self.hailo_arch not in {"hailo8", "hailo8l"}:
            raise ValueError("HAILO_ARCH must be hailo8 or hailo8l.")
        ids = [cam.id for cam in self.cameras]
        if len(set(ids)) != 4:
            raise ValueError("Camera IDs must be unique.")
        roles = {cam.role for cam in self.cameras}
        required_roles = set(CameraRole)
        if roles != required_roles:
            missing = required_roles - roles
            raise ValueError(f"Missing required camera roles: {sorted(missing)}")
        if self.app_env == "production" and not self.calibration_path.exists():
            raise ValueError("Calibration file must exist in production mode.")
