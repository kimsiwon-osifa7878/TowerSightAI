"""Guided checkerboard capture and camera-intrinsics calibration.

Flow (driven by the operator console):

1. The operator prints the checkerboard (``checkerboard.py``) and stands in front of a camera.
2. The console shows one *pose instruction* at a time (``CAPTURE_POSES``). When the board is
   detected steadily in the selected camera's preview frames the frame is saved as a sample and
   the next instruction appears.
3. With enough samples ``calibrate_intrinsics`` runs ``cv2.calibrateCamera`` and the result is
   written as JSON (camera matrix, distortion, RMS re-projection error, per-view errors, image
   size, board spec, capture rotation).

The result is a *measurement file*. Writing it never marks calibration as valid for the safety
gate; that stays a separate, reviewed step. OpenCV/numpy are imported lazily so the module can be
imported on hosts without them.
"""

from __future__ import annotations

import json
import math
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from towersightai.calibration.checkerboard import CheckerboardSpec


SCHEMA_VERSION = 1
MIN_SAMPLES = 10
DEFAULT_INTRINSICS_ROOT = Path("data/calibration/intrinsics")


@dataclass(frozen=True)
class CapturePose:
    key: str
    instruction: str
    hint: str = ""


# Ordered instructions. Together they cover the image centre, all edges and corners (lens
# distortion lives at the edges), tilts around both axes (focal-length observability), and two
# distances. Fifteen accepted views is a comfortable minimum for a stable fisheye-free model.
CAPTURE_POSES: tuple[CapturePose, ...] = (
    CapturePose("center", "정중앙, 카메라를 정면으로", "보드가 화면 가운데에 크게 보이도록"),
    CapturePose("left", "왼쪽으로 이동", "보드를 화면 왼쪽 가장자리에"),
    CapturePose("right", "오른쪽으로 이동", "보드를 화면 오른쪽 가장자리에"),
    CapturePose("top", "위쪽으로 이동", "보드를 화면 위쪽 가장자리에"),
    CapturePose("bottom", "아래쪽으로 이동", "보드를 화면 아래쪽 가장자리에"),
    CapturePose("top_left", "왼쪽 위 모서리", "보드가 화면 왼쪽 위 구석에"),
    CapturePose("top_right", "오른쪽 위 모서리", "보드가 화면 오른쪽 위 구석에"),
    CapturePose("bottom_left", "왼쪽 아래 모서리", "보드가 화면 왼쪽 아래 구석에"),
    CapturePose("bottom_right", "오른쪽 아래 모서리", "보드가 화면 오른쪽 아래 구석에"),
    CapturePose("tilt_left", "정중앙에서 왼쪽 면을 뒤로 기울임", "보드의 왼쪽 끝이 카메라에서 멀어지게 약 30도"),
    CapturePose("tilt_right", "정중앙에서 오른쪽 면을 뒤로 기울임", "보드의 오른쪽 끝이 카메라에서 멀어지게 약 30도"),
    CapturePose("tilt_up", "정중앙에서 윗면을 뒤로 기울임", "보드의 위쪽이 카메라에서 멀어지게 약 30도"),
    CapturePose("tilt_down", "정중앙에서 아랫면을 뒤로 기울임", "보드의 아래쪽이 카메라에서 멀어지게 약 30도"),
    CapturePose("near", "가까이 (화면의 2/3를 채우도록)", "보드 전체가 잘리지 않게"),
    CapturePose("far", "멀리 (화면의 1/4 정도)", "코너가 또렷하게 보이는 거리까지만"),
)


@dataclass(frozen=True)
class CheckerboardDetection:
    """Inner-corner positions in image pixels, ordered row-major as OpenCV returns them."""

    corners: tuple[tuple[float, float], ...]
    image_width: int
    image_height: int

    @property
    def normalized(self) -> tuple[tuple[float, float], ...]:
        return tuple((x / self.image_width, y / self.image_height) for x, y in self.corners)

    @property
    def coverage(self) -> float:
        """Fraction of the image area spanned by the corner bounding box (0..1)."""
        xs = [x for x, _y in self.corners]
        ys = [y for _x, y in self.corners]
        return ((max(xs) - min(xs)) * (max(ys) - min(ys))) / float(self.image_width * self.image_height)


def detect_checkerboard(gray: Any, spec: CheckerboardSpec) -> CheckerboardDetection | None:
    """Find the inner corners of ``spec`` in a grayscale ``numpy`` image. Returns None if absent."""
    import cv2
    import numpy as np

    if gray is None or gray.ndim != 2 or gray.size == 0:
        return None
    pattern = spec.inner_corners
    found, corners = cv2.findChessboardCornersSB(gray, pattern, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not found or corners is None:
        found, corners = cv2.findChessboardCorners(
            gray,
            pattern,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK,
        )
        if not found or corners is None:
            return None
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    if points.shape[0] != pattern[0] * pattern[1]:
        return None
    height, width = gray.shape[:2]
    return CheckerboardDetection(
        corners=tuple((float(x), float(y)) for x, y in points),
        image_width=int(width),
        image_height=int(height),
    )


@dataclass(frozen=True)
class IntrinsicsSample:
    pose_key: str
    image_path: Path
    detection: CheckerboardDetection
    captured_at: str


@dataclass(frozen=True)
class IntrinsicsResult:
    camera_id: str
    image_width: int
    image_height: int
    rotation_degrees: int
    spec: CheckerboardSpec
    sample_count: int
    rms_reprojection_error: float
    camera_matrix: tuple[tuple[float, float, float], ...]
    distortion: tuple[float, ...]
    per_view_errors: tuple[float, ...]
    pose_keys: tuple[str, ...]
    measured_at: str
    calibration_seconds: float
    sample_paths: tuple[str, ...] = field(default_factory=tuple)
    # A measurement file is evidence for review, never authorization.
    reviewed: bool = False

    @property
    def focal_px(self) -> tuple[float, float]:
        return (self.camera_matrix[0][0], self.camera_matrix[1][1])

    @property
    def principal_point(self) -> tuple[float, float]:
        return (self.camera_matrix[0][2], self.camera_matrix[1][2])

    @property
    def horizontal_fov_degrees(self) -> float:
        return math.degrees(2 * math.atan(self.image_width / (2 * self.camera_matrix[0][0])))

    @property
    def quality(self) -> str:
        """Coarse operator-facing grade of the RMS re-projection error in pixels.

        A low RMS with an implausible field of view (e.g. every sample was a flat, frontal
        board) is graded ``suspicious`` so the operator re-captures with real tilts/depths.
        """
        if not 40.0 <= self.horizontal_fov_degrees <= 150.0:
            return "suspicious"
        if self.rms_reprojection_error < 0.5:
            return "good"
        if self.rms_reprojection_error < 1.0:
            return "acceptable"
        return "poor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "camera_intrinsics",
            "camera_id": self.camera_id,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "rotation_degrees": self.rotation_degrees,
            "checkerboard": {
                "columns": self.spec.columns,
                "rows": self.spec.rows,
                "square_mm": self.spec.square_mm,
                "inner_corners": list(self.spec.inner_corners),
            },
            "sample_count": self.sample_count,
            "rms_reprojection_error_px": self.rms_reprojection_error,
            "quality": self.quality,
            "camera_matrix": [list(row) for row in self.camera_matrix],
            "distortion_coefficients": list(self.distortion),
            "per_view_errors_px": list(self.per_view_errors),
            "pose_keys": list(self.pose_keys),
            "horizontal_fov_degrees": round(self.horizontal_fov_degrees, 2),
            "measured_at": self.measured_at,
            "calibration_seconds": round(self.calibration_seconds, 3),
            "source_host": socket.gethostname(),
            "sample_paths": list(self.sample_paths),
            "reviewed": self.reviewed,
            "safe_to_operate": False,
        }

    def summary(self) -> str:
        fx, fy = self.focal_px
        return (
            f"{self.sample_count}장, RMS {self.rms_reprojection_error:.3f}px ({self.quality}), "
            f"f=({fx:.1f}, {fy:.1f})px, HFOV {self.horizontal_fov_degrees:.1f}°"
        )


def calibrate_intrinsics(
    samples: Sequence[IntrinsicsSample],
    spec: CheckerboardSpec,
    *,
    camera_id: str,
    rotation_degrees: int = 0,
    now: datetime | None = None,
) -> IntrinsicsResult:
    """Run ``cv2.calibrateCamera`` over the samples. Raises ValueError on insufficient input."""
    import cv2
    import numpy as np

    if len(samples) < MIN_SAMPLES:
        raise ValueError(f"체커보드 샘플이 부족합니다: {len(samples)}/{MIN_SAMPLES}")
    sizes = {(sample.detection.image_width, sample.detection.image_height) for sample in samples}
    if len(sizes) != 1:
        raise ValueError(f"샘플 해상도가 서로 다릅니다: {sorted(sizes)}")
    (width, height) = next(iter(sizes))

    cols, rows = spec.inner_corners
    object_template = np.zeros((cols * rows, 3), dtype=np.float32)
    object_template[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * float(spec.square_mm)
    object_points = [object_template for _ in samples]
    image_points = [
        np.asarray(sample.detection.corners, dtype=np.float32).reshape(-1, 1, 2) for sample in samples
    ]

    started = time.monotonic()
    rms, camera_matrix, dist, rvecs, tvecs = cv2.calibrateCamera(
        object_points, image_points, (width, height), None, None
    )
    per_view: list[float] = []
    for obj, img, rvec, tvec in zip(object_points, image_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, camera_matrix, dist)
        error = cv2.norm(img, projected, cv2.NORM_L2) / math.sqrt(len(projected))
        per_view.append(float(error))
    elapsed = time.monotonic() - started

    matrix = tuple(tuple(float(value) for value in row) for row in np.asarray(camera_matrix).tolist())
    return IntrinsicsResult(
        camera_id=camera_id,
        image_width=width,
        image_height=height,
        rotation_degrees=rotation_degrees,
        spec=spec,
        sample_count=len(samples),
        rms_reprojection_error=float(rms),
        camera_matrix=matrix,  # type: ignore[arg-type]
        distortion=tuple(float(value) for value in np.asarray(dist).ravel().tolist()),
        per_view_errors=tuple(per_view),
        pose_keys=tuple(sample.pose_key for sample in samples),
        measured_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        calibration_seconds=elapsed,
        sample_paths=tuple(str(sample.image_path) for sample in samples),
    )


class IntrinsicsSessionStore:
    """Filesystem layout for one capture session.

    ``<root>/sessions/<camera_id>-<UTC stamp>/pose-NN-<key>.png`` holds the accepted frames and
    ``intrinsics.json`` the result; ``<root>/<camera_id>.json`` is overwritten with the latest
    result so later tooling has one well-known path per camera.
    """

    def __init__(self, root: Path, camera_id: str, *, now: datetime | None = None) -> None:
        self.root = Path(root)
        self.camera_id = camera_id
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        self.session_dir = self.root / "sessions" / f"{camera_id}-{stamp}"

    @property
    def latest_path(self) -> Path:
        return self.root / f"{self.camera_id}.json"

    @property
    def result_path(self) -> Path:
        return self.session_dir / "intrinsics.json"

    def sample_path(self, index: int, pose_key: str) -> Path:
        return self.session_dir / f"pose-{index:02d}-{pose_key}.png"

    def save_sample(self, index: int, pose_key: str, bgr: Any, detection: CheckerboardDetection) -> IntrinsicsSample:
        import cv2

        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.sample_path(index, pose_key)
        if not cv2.imwrite(str(path), bgr):
            raise OSError(f"could not write calibration frame: {path}")
        return IntrinsicsSample(
            pose_key=pose_key,
            image_path=path,
            detection=detection,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def save_result(self, result: IntrinsicsResult) -> tuple[Path, Path]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.result_path.write_text(payload, encoding="utf-8")
        temporary = self.latest_path.with_suffix(".json.part")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.latest_path)
        return self.result_path, self.latest_path


def load_intrinsics(path: Path) -> dict[str, Any]:
    """Read a saved intrinsics JSON. Raises ValueError when it is not an intrinsics file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("kind") != "camera_intrinsics" or "camera_matrix" not in data:
        raise ValueError(f"not a camera intrinsics file: {path}")
    return data
