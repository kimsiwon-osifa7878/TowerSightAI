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
from typing import Any, Mapping, Sequence

from towersightai.calibration.checkerboard import CheckerboardSpec


SCHEMA_VERSION = 1
MIN_SAMPLES = 10
DEFAULT_INTRINSICS_ROOT = Path("data/calibration/intrinsics")


#: A4 가로 용지 비율(297×210). 목표 상자를 **화면에 보이는 모양 그대로** 이 비율로 그린다.
A4_LANDSCAPE_ASPECT = 297.0 / 210.0

#: 목표 상자 밖으로 이 정도(정규화)까지는 나가도 허용한다.
TARGET_TOLERANCE = 0.02
#: 코너 bbox가 목표 상자를 이 비율 이상 채워야 한다. 내부 코너 범위(200×125 mm)는 A4
#: 용지(297×210 mm)의 약 40 %라서, 용지가 상자를 거의 채우면 0.4 근처가 된다.
TARGET_MIN_FILL = 0.20
#: 기울임 자세로 인정할 최소 왜곡(마주보는 변 길이 비 차이).
TILT_MIN_SKEW = 0.10


@dataclass(frozen=True)
class CapturePose:
    """한 장의 촬영 자세. **화면에 사각 상자로 표시**하고 그 안에 보드가 들어오면 인정한다.

    "왼쪽/오른쪽" 같은 말은 카메라 기준인지 사람 기준인지 헷갈린다는 현장 피드백이 있어
    (2026-09-16), 위치는 전부 화면 위의 상자로만 지시한다. 상자 비율은 A4 가로 용지와
    같으므로 인쇄한 체커보드를 상자에 맞추면 된다.
    """

    key: str
    instruction: str
    #: 목표 상자 중심 (정규화 영상좌표)
    center: tuple[float, float] = (0.5, 0.5)
    #: 목표 상자 가로 길이 (정규화 영상좌표). 세로는 A4 비율로 계산한다.
    width: float = 0.45
    #: "" | "horizontal"(좌우로 기울임) | "vertical"(위아래로 기울임)
    tilt: str = ""
    hint: str = ""

    def target_rect(self, frame_aspect: float) -> tuple[float, float, float, float]:
        """정규화 (x0, y0, x1, y1). ``frame_aspect`` = 영상 높이/너비.

        화면에 보이는 상자의 가로:세로가 A4 가로 비율이 되도록 세로를 정한다.
        """
        if frame_aspect <= 0:
            raise ValueError("frame_aspect must be positive")
        height = self.width / (A4_LANDSCAPE_ASPECT * frame_aspect)
        cx, cy = self.center
        x0, x1 = cx - self.width / 2.0, cx + self.width / 2.0
        y0, y1 = cy - height / 2.0, cy + height / 2.0
        return (max(x0, 0.0), max(y0, 0.0), min(x1, 1.0), min(y1, 1.0))


# 순서대로 진행한다. 화면 중앙·네 변·네 모서리(왜곡은 가장자리에 산다), 가까이/멀리(초점거리
# 관측), 좌우·위아래 기울임(초점거리 관측)까지 13장. `MIN_SAMPLES`(10장) 이상이면 측정 가능.
CAPTURE_POSES: tuple[CapturePose, ...] = (
    CapturePose("center", "화면 가운데 상자에 보드를 맞추세요", (0.50, 0.50), 0.46),
    CapturePose("left", "왼쪽 상자에 보드를 맞추세요", (0.22, 0.50), 0.38),
    CapturePose("right", "오른쪽 상자에 보드를 맞추세요", (0.78, 0.50), 0.38),
    CapturePose("top", "위쪽 상자에 보드를 맞추세요", (0.50, 0.26), 0.38),
    CapturePose("bottom", "아래쪽 상자에 보드를 맞추세요", (0.50, 0.74), 0.38),
    CapturePose("top_left", "왼쪽 위 상자에 보드를 맞추세요", (0.24, 0.26), 0.36),
    CapturePose("top_right", "오른쪽 위 상자에 보드를 맞추세요", (0.76, 0.26), 0.36),
    CapturePose("bottom_left", "왼쪽 아래 상자에 보드를 맞추세요", (0.24, 0.74), 0.36),
    CapturePose("bottom_right", "오른쪽 아래 상자에 보드를 맞추세요", (0.76, 0.74), 0.36),
    CapturePose("near", "가까이 — 큰 상자를 보드로 채우세요", (0.50, 0.50), 0.70),
    CapturePose("far", "멀리 — 작은 상자에 보드를 맞추세요", (0.50, 0.50), 0.28),
    CapturePose(
        "tilt_h",
        "가운데 상자 안에서 보드를 좌우로 기울이세요",
        (0.50, 0.50),
        0.50,
        tilt="horizontal",
        hint="어느 쪽이든 괜찮습니다. 한쪽 끝이 카메라에서 멀어지게 약 30도",
    ),
    CapturePose(
        "tilt_v",
        "가운데 상자 안에서 보드를 위아래로 기울이세요",
        (0.50, 0.50),
        0.50,
        tilt="vertical",
        hint="어느 쪽이든 괜찮습니다. 위나 아래가 카메라에서 멀어지게 약 30도",
    ),
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

    @property
    def normalized_bounds(self) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) of the corner bounding box in normalized image coordinates."""
        points = self.normalized
        xs = [x for x, _y in points]
        ys = [y for _x, y in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def grid_corners(self, spec: CheckerboardSpec) -> tuple[tuple[float, float], ...] | None:
        """(top-left, top-right, bottom-left, bottom-right) of the detected grid, in pixels.

        ``findChessboardCorners`` returns the inner corners row-major for the configured
        ``(cols, rows)`` pattern, so the four extremes are at fixed indices.
        """
        cols, rows = spec.inner_corners
        if len(self.corners) != cols * rows:
            return None
        return (
            self.corners[0],
            self.corners[cols - 1],
            self.corners[cols * (rows - 1)],
            self.corners[-1],
        )

    def tilt_skew(self, spec: CheckerboardSpec) -> tuple[float, float]:
        """(horizontal_skew, vertical_skew) — how much opposite grid edges differ in length.

        A frontal board projects to a near-parallelogram, so both values sit near 0. Rotating
        the board about the vertical axis makes the left and right edges differ (horizontal
        skew); about the horizontal axis, the top and bottom edges (vertical skew). The sign
        of the rotation is deliberately ignored — the operator may tilt either way.
        """
        grid = self.grid_corners(spec)
        if grid is None:
            return (0.0, 0.0)
        top_left, top_right, bottom_left, bottom_right = grid

        def length(a: tuple[float, float], b: tuple[float, float]) -> float:
            return math.dist(a, b)

        left, right = length(top_left, bottom_left), length(top_right, bottom_right)
        top, bottom = length(top_left, top_right), length(bottom_left, bottom_right)
        horizontal = abs(left - right) / max(left, right, 1e-6)
        vertical = abs(top - bottom) / max(top, bottom, 1e-6)
        return (horizontal, vertical)


@dataclass(frozen=True)
class PoseFit:
    """Whether the detected board satisfies the pose's on-screen target box."""

    inside: bool
    fill: float
    skew: float
    ok: bool
    reason: str = ""


def evaluate_pose_fit(
    pose: CapturePose, detection: CheckerboardDetection, *, spec: CheckerboardSpec
) -> PoseFit:
    """Check the detection against ``pose``'s target box (and tilt, when the pose wants one).

    Returns a Korean ``reason`` when it does not qualify — the operator console shows it
    verbatim, so it has to say what to physically do next.
    """
    frame_aspect = detection.image_height / float(detection.image_width)
    x0, y0, x1, y1 = pose.target_rect(frame_aspect)
    bx0, by0, bx1, by1 = detection.normalized_bounds

    inside = (
        bx0 >= x0 - TARGET_TOLERANCE
        and by0 >= y0 - TARGET_TOLERANCE
        and bx1 <= x1 + TARGET_TOLERANCE
        and by1 <= y1 + TARGET_TOLERANCE
    )
    rect_area = max((x1 - x0) * (y1 - y0), 1e-9)
    fill = ((bx1 - bx0) * (by1 - by0)) / rect_area

    horizontal, vertical = detection.tilt_skew(spec)
    skew = horizontal if pose.tilt == "horizontal" else vertical if pose.tilt == "vertical" else 0.0

    if not inside:
        return PoseFit(inside, fill, skew, False, "체커판이 상자 밖으로 나갔습니다")
    if fill < TARGET_MIN_FILL:
        return PoseFit(inside, fill, skew, False, "체커판이 상자에 비해 작습니다. 상자를 채우도록 맞춰 주세요")
    if pose.tilt == "horizontal" and skew < TILT_MIN_SKEW:
        return PoseFit(inside, fill, skew, False, "보드를 좌우로 더 기울여 주세요")
    if pose.tilt == "vertical" and skew < TILT_MIN_SKEW:
        return PoseFit(inside, fill, skew, False, "보드를 위아래로 더 기울여 주세요")
    return PoseFit(inside, fill, skew, True)


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
    #: Host that ran the measurement. Kept on the result so a file shared to another machine
    #: can be shown as borrowed instead of passing for that camera's own measurement.
    source_host: str = ""
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
            "source_host": self.source_host or socket.gethostname(),
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

    def quality_report(self) -> tuple[str, tuple[str, ...]]:
        """(등급, 한국어 점검 항목). 운영자가 '잘 됐는지'를 바로 판단하게 하는 용도.

        등급은 ``good`` / ``acceptable`` / ``poor`` / ``suspicious``. 항목은 통과/실패를 모두
        적는다 — 무엇을 봤고 왜 그 등급인지가 보여야 다시 찍을지 판단할 수 있다.
        """
        lines: list[str] = []
        worst = max(self.per_view_errors) if self.per_view_errors else float("nan")

        mark = "✔" if self.rms_reprojection_error < 0.5 else ("△" if self.rms_reprojection_error < 1.0 else "✘")
        lines.append(f"{mark} 재투영 오차 RMS {self.rms_reprojection_error:.3f} px (0.5 미만이면 양호)")

        mark = "✔" if worst < 1.0 else ("△" if worst < 2.0 else "✘")
        lines.append(f"{mark} 가장 나쁜 장 {worst:.3f} px (2.0 이상이면 그 장을 다시 찍는 게 좋습니다)")

        mark = "✔" if 40.0 <= self.horizontal_fov_degrees <= 150.0 else "✘"
        lines.append(f"{mark} 수평 화각 {self.horizontal_fov_degrees:.1f}° (40~150° 밖이면 측정이 틀어진 것)")

        mark = "✔" if self.sample_count >= MIN_SAMPLES + 2 else ("△" if self.sample_count >= MIN_SAMPLES else "✘")
        lines.append(f"{mark} 샘플 {self.sample_count}장 (권장 {MIN_SAMPLES + 2}장 이상)")

        edge_poses = {"left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right"}
        edges = len(edge_poses & set(self.pose_keys))
        mark = "✔" if edges >= 5 else ("△" if edges >= 3 else "✘")
        lines.append(f"{mark} 가장자리 자세 {edges}/8 (왜곡은 가장자리에서 결정됩니다)")

        local_host = socket.gethostname()
        if self.source_host and self.source_host != local_host:
            lines.append(f"△ 이 장비가 아니라 {self.source_host}에서 측정한 값입니다 (같은 기종이면 사용 가능)")

        tilts = len({"tilt_h", "tilt_v"} & set(self.pose_keys))
        mark = "✔" if tilts == 2 else ("△" if tilts == 1 else "✘")
        lines.append(f"{mark} 기울임 자세 {tilts}/2 (정면 사진만으로는 초점거리가 안 잡힙니다)")

        if not 40.0 <= self.horizontal_fov_degrees <= 150.0:
            grade = "suspicious"
        elif edges < 3 or tilts == 0:
            grade = "poor"
        else:
            grade = self.quality
        return grade, tuple(lines)


#: 확인용 격자 간격(정규화). 직선이 휘었는지 눈으로 보려는 것이므로 촘촘할 필요는 없다.
VERIFICATION_GRID_STEPS = 8


def build_verification_image(bgr: Any, result: "IntrinsicsResult") -> Any:
    """원본과 왜곡 보정 결과를 좌우로 붙이고, 둘 다에 **곧은 격자**를 겹쳐 돌려준다.

    격자는 완벽한 직선이다. 보정 전에는 화면의 실제 직선(레일·기둥·바닥 이음매)이 격자에서
    휘어 보이고, 보정이 잘 됐으면 나란해진다 — 숫자를 몰라도 눈으로 판정할 수 있다.
    캡션(한국어)은 UI가 붙인다. 제품 코드에 폰트 의존성을 만들지 않기 위해서다.
    """
    import cv2
    import numpy as np

    if bgr is None or getattr(bgr, "size", 0) == 0:
        raise ValueError("검증할 프레임이 없습니다")
    height, width = bgr.shape[:2]
    matrix = np.asarray(result.camera_matrix, dtype=np.float64)
    if (width, height) != (result.image_width, result.image_height):
        # 측정 당시와 해상도가 다르면 내부 파라미터를 같은 비율로 옮긴다.
        scale_x = width / float(result.image_width)
        scale_y = height / float(result.image_height)
        matrix = matrix.copy()
        matrix[0, 0] *= scale_x
        matrix[0, 2] *= scale_x
        matrix[1, 1] *= scale_y
        matrix[1, 2] *= scale_y
    dist = np.asarray(result.distortion, dtype=np.float64)
    corrected = cv2.undistort(bgr, matrix, dist)

    def with_grid(image: Any) -> Any:
        canvas = image.copy()
        overlay = canvas.copy()
        for index in range(1, VERIFICATION_GRID_STEPS):
            x = int(round(width * index / VERIFICATION_GRID_STEPS))
            y = int(round(height * index / VERIFICATION_GRID_STEPS))
            cv2.line(overlay, (x, 0), (x, height), (0, 215, 255), 1, cv2.LINE_AA)
            cv2.line(overlay, (0, y), (width, y), (0, 215, 255), 1, cv2.LINE_AA)
        return cv2.addWeighted(overlay, 0.55, canvas, 0.45, 0)

    divider = np.full((height, 6, 3), 40, dtype=bgr.dtype)
    return np.hstack([with_grid(bgr), divider, with_grid(corrected)])


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


def result_from_dict(data: Mapping[str, Any]) -> IntrinsicsResult:
    """Rebuild an :class:`IntrinsicsResult` from a saved file so a past measurement can be
    re-checked (quality report, undistortion preview) without re-shooting."""
    board = data.get("checkerboard") or {}
    return IntrinsicsResult(
        camera_id=str(data.get("camera_id", "")),
        image_width=int(data["image_width"]),
        image_height=int(data["image_height"]),
        rotation_degrees=int(data.get("rotation_degrees", 0)),
        spec=CheckerboardSpec(
            columns=int(board.get("columns", CheckerboardSpec().columns)),
            rows=int(board.get("rows", CheckerboardSpec().rows)),
            square_mm=float(board.get("square_mm", CheckerboardSpec().square_mm)),
        ),
        sample_count=int(data.get("sample_count", 0)),
        rms_reprojection_error=float(data.get("rms_reprojection_error_px", 0.0)),
        camera_matrix=tuple(tuple(float(v) for v in row) for row in data["camera_matrix"]),
        distortion=tuple(float(v) for v in data.get("distortion_coefficients", ())),
        per_view_errors=tuple(float(v) for v in data.get("per_view_errors_px", ())),
        pose_keys=tuple(str(v) for v in data.get("pose_keys", ())),
        measured_at=str(data.get("measured_at", "")),
        calibration_seconds=float(data.get("calibration_seconds", 0.0)),
        sample_paths=tuple(str(v) for v in data.get("sample_paths", ())),
        source_host=str(data.get("source_host", "")),
        reviewed=bool(data.get("reviewed", False)),
    )
