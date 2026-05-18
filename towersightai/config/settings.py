from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CameraRole(str, Enum):
    ceiling = "ceiling"
    front = "front"
    rear_side = "rear_side"
    opposite_side = "opposite_side"


@dataclass(frozen=True)
class CameraConfig:
    id: str
    role: CameraRole
    rtsp_url: str
    username: str | None = None
    password: str | None = None


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
    hailo_network_name: str = "yolov5"
    ui_fullscreen: bool = True
    ui_camera_resolution: CameraResolution | tuple[int, int] | str = CameraResolution()

    def __post_init__(self) -> None:
        self.camera_1 = self._as_camera(self.camera_1)
        self.camera_2 = self._as_camera(self.camera_2)
        self.camera_3 = self._as_camera(self.camera_3)
        self.camera_4 = self._as_camera(self.camera_4)
        self.ui_camera_resolution = self._as_resolution(self.ui_camera_resolution)
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
        )

    def _as_resolution(self, resolution: CameraResolution | tuple[int, int] | str) -> CameraResolution:
        return parse_camera_resolution(resolution)

    @property
    def cameras(self) -> list[CameraConfig]:
        return [self.camera_1, self.camera_2, self.camera_3, self.camera_4]

    def _validate_safety_constraints(self) -> None:
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
