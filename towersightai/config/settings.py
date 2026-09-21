from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class LD2410Config:
    enabled: bool = False
    bind_host: str = "0.0.0.0"
    port: int = 9000
    buffer_seconds: float = 30.0
    max_sample_age_seconds: float = 1.0
    client_idle_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.bind_host:
            raise ValueError("LD2410_TCP_BIND_HOST must not be empty.")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("LD2410_TCP_PORT must be between 1 and 65535.")
        for name, value in (
            ("LD2410_BUFFER_SECONDS", self.buffer_seconds),
            ("LD2410_MAX_SAMPLE_AGE_SECONDS", self.max_sample_age_seconds),
            ("LD2410_CLIENT_IDLE_TIMEOUT_SECONDS", self.client_idle_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive.")


@dataclass(frozen=True)
class VehicleEnvelopeConfig:
    """Site geometry and vehicle limits for the 3D vehicle-box (직육면체) estimation.

    Defaults come from the Hyundai Elevator approval drawing for 구로 신안타워 (RSTT5-36,
    2025-12-08, sheets J001/J002): the 주차구획 pallet rectangle, rail inner width, bay heights,
    and the 설계기준 vehicle limits (mirrors folded). Every value is millimetres. The world
    frame origin is the pallet rectangle centre, x along the pallet length (entry direction),
    y across the width, z up. The two diagonal side cameras are named by the vehicle corner
    they face so the physical cameras can be swapped by configuration only.
    """

    pallet_length_mm: float = 5350.0
    pallet_width_mm: float = 2200.0
    rail_inner_width_mm: float = 2106.0
    bay_height_sedan_mm: float = 1600.0
    bay_height_suv_mm: float = 1900.0
    max_vehicle_length_mm: float = 5205.0
    max_vehicle_width_mm: float = 2000.0
    max_vehicle_width_with_mirrors_mm: float = 2100.0
    max_vehicle_height_sedan_mm: float = 1550.0
    max_vehicle_height_suv_mm: float = 1850.0
    max_wheel_track_mm: float = 2000.0
    front_left_camera_role: str = "rear_side"
    rear_right_camera_role: str = "opposite_side"

    def __post_init__(self) -> None:
        for name, value in (
            ("VEHICLE_BOX_PALLET_LENGTH_MM", self.pallet_length_mm),
            ("VEHICLE_BOX_PALLET_WIDTH_MM", self.pallet_width_mm),
            ("VEHICLE_BOX_RAIL_INNER_WIDTH_MM", self.rail_inner_width_mm),
            ("VEHICLE_BOX_BAY_HEIGHT_SEDAN_MM", self.bay_height_sedan_mm),
            ("VEHICLE_BOX_BAY_HEIGHT_SUV_MM", self.bay_height_suv_mm),
            ("VEHICLE_BOX_MAX_LENGTH_MM", self.max_vehicle_length_mm),
            ("VEHICLE_BOX_MAX_WIDTH_MM", self.max_vehicle_width_mm),
            ("VEHICLE_BOX_MAX_WIDTH_WITH_MIRRORS_MM", self.max_vehicle_width_with_mirrors_mm),
            ("VEHICLE_BOX_MAX_HEIGHT_SEDAN_MM", self.max_vehicle_height_sedan_mm),
            ("VEHICLE_BOX_MAX_HEIGHT_SUV_MM", self.max_vehicle_height_suv_mm),
            ("VEHICLE_BOX_MAX_WHEEL_TRACK_MM", self.max_wheel_track_mm),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive.")
        if self.max_vehicle_length_mm > self.pallet_length_mm:
            raise ValueError("VEHICLE_BOX_MAX_LENGTH_MM must not exceed VEHICLE_BOX_PALLET_LENGTH_MM.")
        if self.max_vehicle_width_mm > self.pallet_width_mm:
            raise ValueError("VEHICLE_BOX_MAX_WIDTH_MM must not exceed VEHICLE_BOX_PALLET_WIDTH_MM.")
        if self.max_vehicle_width_with_mirrors_mm < self.max_vehicle_width_mm:
            raise ValueError(
                "VEHICLE_BOX_MAX_WIDTH_WITH_MIRRORS_MM must not be smaller than VEHICLE_BOX_MAX_WIDTH_MM."
            )
        if self.max_wheel_track_mm > self.rail_inner_width_mm:
            raise ValueError("VEHICLE_BOX_MAX_WHEEL_TRACK_MM must not exceed VEHICLE_BOX_RAIL_INNER_WIDTH_MM.")
        if self.max_vehicle_height_sedan_mm > self.bay_height_sedan_mm:
            raise ValueError("VEHICLE_BOX_MAX_HEIGHT_SEDAN_MM must not exceed VEHICLE_BOX_BAY_HEIGHT_SEDAN_MM.")
        if self.max_vehicle_height_suv_mm > self.bay_height_suv_mm:
            raise ValueError("VEHICLE_BOX_MAX_HEIGHT_SUV_MM must not exceed VEHICLE_BOX_BAY_HEIGHT_SUV_MM.")
        side_roles = {CameraRole.rear_side.value, CameraRole.opposite_side.value}
        for name, role in (
            ("VEHICLE_BOX_FRONT_LEFT_CAMERA", self.front_left_camera_role),
            ("VEHICLE_BOX_REAR_RIGHT_CAMERA", self.rear_right_camera_role),
        ):
            if role not in side_roles:
                raise ValueError(f"{name} must be one of {sorted(side_roles)}, got {role!r}.")
        if self.front_left_camera_role == self.rear_right_camera_role:
            raise ValueError("VEHICLE_BOX_FRONT_LEFT_CAMERA and VEHICLE_BOX_REAR_RIGHT_CAMERA must differ.")

    @property
    def front_left_role(self) -> "CameraRole":
        return CameraRole(self.front_left_camera_role)

    @property
    def rear_right_role(self) -> "CameraRole":
        return CameraRole(self.rear_right_camera_role)


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
    shard_minutes: int = 60
    media_enabled: bool = False
    media_snapshot_jpeg_quality: int = 85
    media_frame_max_age_seconds: float = 1.0
    media_pre_seconds: float = 5.0
    media_vehicle_post_seconds: float = 10.0
    media_segment_seconds: float = 2.0
    media_clip_part_seconds: float = 300.0
    media_gstreamer_python: Path = Path("/usr/bin/python3")
    # Radar (LD2410) raw logging — analysis only, never a safety-gate input.
    ld2410_sample_interval_seconds: float = 1.0  # 0 disables the 1 Hz sample and the radar window
    radar_window_min_seconds: float = 3.0
    radar_window_clear_seconds: float = 5.0
    # How long into a radar window to keep recording camera state (0 disables radar_sample).
    radar_sample_seconds: float = 60.0
    media_radar_evidence: bool = True
    media_radar_min_interval_seconds: float = 60.0
    media_radar_clip_max_seconds: float = 30.0
    # Hailo failure evidence → NAS (remote diagnosis). Diagnostic only, never a safety input.
    hailo_incident_upload_enabled: bool = True
    hailo_incident_min_interval_seconds: float = 1800.0
    nas_host: str = ""
    nas_port: int = 22
    nas_username: str = ""
    nas_password: str = field(default="", repr=False)
    nas_folder: str = ""
    known_hosts_path: Path = Path("~/.ssh/known_hosts")

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_dir", Path(self.local_dir).expanduser())
        object.__setattr__(self, "known_hosts_path", Path(self.known_hosts_path).expanduser())
        object.__setattr__(self, "media_gstreamer_python", Path(self.media_gstreamer_python).expanduser())
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
        if self.shard_minutes < 1 or self.shard_minutes > 60 or 60 % self.shard_minutes:
            raise ValueError("RAW_DATA_SHARD_MINUTES must be a positive divisor of 60.")
        if not 1 <= self.media_snapshot_jpeg_quality <= 100:
            raise ValueError("RAW_MEDIA_SNAPSHOT_JPEG_QUALITY must be between 1 and 100.")
        for name, value in (
            ("RAW_MEDIA_FRAME_MAX_AGE_SECONDS", self.media_frame_max_age_seconds),
            ("RAW_MEDIA_PRE_SECONDS", self.media_pre_seconds),
            ("RAW_MEDIA_VEHICLE_POST_SECONDS", self.media_vehicle_post_seconds),
            ("RAW_MEDIA_SEGMENT_SECONDS", self.media_segment_seconds),
            ("RAW_MEDIA_CLIP_PART_SECONDS", self.media_clip_part_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.media_clip_part_seconds < self.media_segment_seconds:
            raise ValueError("RAW_MEDIA_CLIP_PART_SECONDS must be at least RAW_MEDIA_SEGMENT_SECONDS.")
        if self.radar_sample_seconds < 0:
            raise ValueError("RAW_DATA_RADAR_SAMPLE_SECONDS must be non-negative.")
        if self.ld2410_sample_interval_seconds < 0:
            raise ValueError("RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS must be zero or positive.")
        for name, value in (
            ("RAW_DATA_RADAR_WINDOW_MIN_SECONDS", self.radar_window_min_seconds),
            ("RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS", self.radar_window_clear_seconds),
            ("RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS", self.media_radar_min_interval_seconds),
            ("RAW_MEDIA_RADAR_CLIP_MAX_SECONDS", self.media_radar_clip_max_seconds),
            ("HAILO_INCIDENT_MIN_INTERVAL_SECONDS", self.hailo_incident_min_interval_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.media_radar_clip_max_seconds < self.media_segment_seconds:
            raise ValueError("RAW_MEDIA_RADAR_CLIP_MAX_SECONDS must be at least RAW_MEDIA_SEGMENT_SECONDS.")
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
    record_rtsp_url: str | None = field(default=None, repr=False)

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
    ld2410: LD2410Config | dict | None = None
    vehicle_envelope: VehicleEnvelopeConfig | dict | None = None
    # Hailo 장치가 error 로 이 시간 이상 지속되면 PCIe 재열거를 자동 시도한다(정비 동작).
    hailo_auto_recovery_enabled: bool = True
    hailo_auto_recovery_after_seconds: float = 120.0
    hailo_auto_recovery_max_attempts: int = 3
    hailo_auto_recovery_window_seconds: float = 3600.0

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
        if self.ld2410 is None:
            self.ld2410 = LD2410Config()
        elif isinstance(self.ld2410, dict):
            self.ld2410 = LD2410Config(**self.ld2410)
        if self.vehicle_envelope is None:
            self.vehicle_envelope = VehicleEnvelopeConfig()
        elif isinstance(self.vehicle_envelope, dict):
            self.vehicle_envelope = VehicleEnvelopeConfig(**self.vehicle_envelope)
        for name, value in (
            ("HAILO_AUTO_RECOVERY_AFTER_SECONDS", self.hailo_auto_recovery_after_seconds),
            ("HAILO_AUTO_RECOVERY_WINDOW_SECONDS", self.hailo_auto_recovery_window_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.hailo_auto_recovery_max_attempts < 0:
            raise ValueError("HAILO_AUTO_RECOVERY_MAX_ATTEMPTS must be zero or positive.")
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
            record_rtsp_url=camera.get("record_rtsp_url"),
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
        if self.ld2410.enabled and not self.raw_storage.enabled:
            raise ValueError("LD2410_TCP_ENABLED requires RAW_DATA_ENABLED=true for raw-only integration.")
