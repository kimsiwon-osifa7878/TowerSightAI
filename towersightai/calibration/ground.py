"""Ground (extrinsic) calibration: where each camera sits relative to the pallet.

The intrinsics step (``intrinsics.py``) answers "what does this lens do". This step answers
"where is this camera, and which way is it pointing, relative to the parking bay" — the missing
half needed before any camera measurement can be expressed in millimetres.

Why an operator picks the points
--------------------------------
Automatic detection was tried first and does not close (2026-09-16): the pallet deck is edged in
yellow only along its two **long** sides, so the ``x`` reference (the deck ends) has no automatic
feature — one end is the orange stopper frame, a 3-D structure whose silhouette is not a ground
line, and the other is open. Guessing it is what made the earlier lab calibration wrong. Four
clicks remove the guess.

Flow
----
1. The operator clicks the four corners of the pallet deck, in any order.
2. The operator clicks the base of the **orange stopper frame**, which marks the inner end
   (``-x``). That single landmark fixes the entry direction without saying "left" or "right" —
   the same lesson as the checkerboard target boxes.
3. ``solve_ground_pose`` undistorts the clicks with the measured intrinsics, orders the corners
   around the deck, tries both remaining assignments, and keeps the one that reprojects best.

The result is a **measurement file**: ``reviewed`` stays false and ``safe_to_operate`` is always
false, exactly like the intrinsics file. Nothing here marks calibration valid for the safety gate.

World frame (same as INTENT.md §4): origin at the pallet rectangle centre, ``x`` along the pallet
length with ``+x`` toward the entry, ``y`` across the width, ``z`` up, millimetres.
"""

from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
DEFAULT_GROUND_ROOT = Path("data/calibration/ground")

#: Clicks needed before the pose can be solved: four deck corners plus the stopper landmark.
REQUIRED_CORNERS = 4

#: A plausible mounting envelope for the site cameras, in millimetres. A solution outside this
#: is reported as implausible instead of being silently trusted.
CAMERA_HEIGHT_RANGE_MM = (500.0, 7000.0)
CAMERA_DISTANCE_LIMIT_MM = 15000.0


@dataclass(frozen=True)
class GroundPoseResult:
    camera_id: str
    image_width: int
    image_height: int
    rotation_degrees: int
    pallet_length_mm: float
    pallet_width_mm: float
    #: Rodrigues rotation and translation of the world frame in camera coordinates.
    rvec: tuple[float, float, float]
    tvec: tuple[float, float, float]
    #: Clicked points, normalized image coordinates, in the order the operator entered them.
    corner_points: tuple[tuple[float, float], ...]
    stopper_point: tuple[float, float]
    #: World coordinate assigned to each clicked corner, same order.
    corner_world: tuple[tuple[float, float], ...]
    residual_mm: float
    intrinsics_camera_id: str
    intrinsics_borrowed: bool
    measured_at: str
    #: Host that picked the points. A ground pose belongs to **that camera at that site**, not to
    #: whichever machine holds the file — the offline lab analyses site images on the bench and
    #: needs the site's pose. Recorded so the operator can see which installation it describes.
    source_host: str = ""
    #: Host that measured the lens file this pose was solved with. Kept apart from
    #: ``intrinsics_camera_id`` so the file stays *findable*: the camera id resolves to
    #: ``intrinsics/<id>.json``, the host is only a label. Folding the two into one string
    #: produced names like ``opposite_side@bench`` that no loader could open (2026-09-18).
    intrinsics_source_host: str = ""
    reviewed: bool = False

    @property
    def foreign(self) -> bool:
        return bool(self.source_host) and self.source_host != socket.gethostname()

    @property
    def camera_position_mm(self) -> tuple[float, float, float]:
        """Camera centre in the pallet frame."""
        import cv2
        import numpy as np

        rotation, _ = cv2.Rodrigues(np.array(self.rvec, dtype=np.float64))
        centre = -rotation.T @ np.array(self.tvec, dtype=np.float64)
        return (float(centre[0]), float(centre[1]), float(centre[2]))

    @property
    def orientation_degrees(self) -> tuple[float, float, float]:
        """(pan, tilt, roll) of the optical axis in the pallet frame.

        pan = bearing from +x (entry direction), tilt = how far below horizontal it looks,
        roll = how far the image horizontal is tipped from the ground horizontal.
        """
        import cv2
        import numpy as np

        rotation, _ = cv2.Rodrigues(np.array(self.rvec, dtype=np.float64))
        axis = rotation.T @ np.array([0.0, 0.0, 1.0])
        pan = math.degrees(math.atan2(axis[1], axis[0]))
        tilt = math.degrees(math.asin(max(-1.0, min(1.0, -axis[2]))))
        right = rotation.T @ np.array([1.0, 0.0, 0.0])
        roll = math.degrees(math.asin(max(-1.0, min(1.0, right[2]))))
        return (pan, tilt, roll)

    @property
    def plausible(self) -> bool:
        x, y, z = self.camera_position_mm
        return (
            CAMERA_HEIGHT_RANGE_MM[0] <= z <= CAMERA_HEIGHT_RANGE_MM[1]
            and math.hypot(x, y) <= CAMERA_DISTANCE_LIMIT_MM
        )

    @property
    def quality(self) -> str:
        if not self.plausible:
            return "suspicious"
        if self.residual_mm < 40.0:
            return "good"
        if self.residual_mm < 120.0:
            return "acceptable"
        return "poor"

    def quality_report(self) -> tuple[str, tuple[str, ...]]:
        """(등급, 한국어 점검 항목) — 운영자가 '제대로 찍혔는지' 판단할 근거."""
        x, y, z = self.camera_position_mm
        pan, tilt, roll = self.orientation_degrees
        lines: list[str] = []

        mark = "✔" if self.residual_mm < 40.0 else ("△" if self.residual_mm < 120.0 else "✘")
        lines.append(f"{mark} 모서리 되맞춤 오차 {self.residual_mm:.0f} mm (40 mm 미만이면 양호)")

        mark = "✔" if CAMERA_HEIGHT_RANGE_MM[0] <= z <= CAMERA_HEIGHT_RANGE_MM[1] else "✘"
        lines.append(f"{mark} 카메라 높이 {z:.0f} mm (설치 가능 범위 500~7,000 mm)")

        mark = "✔" if math.hypot(x, y) <= CAMERA_DISTANCE_LIMIT_MM else "✘"
        lines.append(f"{mark} 팔레트 중심에서 수평 거리 {math.hypot(x, y):.0f} mm")
        lines.append(f"· 위치 x {x:+.0f} · y {y:+.0f} mm (원점 = 팔레트 중심, +x = 진입 방향)")
        lines.append(f"· 방향 팬 {pan:+.1f}° · 틸트 {tilt:+.1f}°(아래로) · 롤 {roll:+.1f}°")

        mark = "✔" if not self.intrinsics_borrowed else "△"
        origin = self.intrinsics_camera_id
        if self.intrinsics_source_host:
            origin = f"{origin} ({self.intrinsics_source_host})"
        source = f"{origin} 측정값을 빌려 씀" if self.intrinsics_borrowed else "자체 측정값"
        lines.append(f"{mark} 렌즈 내부 파라미터 {source}")

        if self.foreign:
            lines.append(
                f"· {self.source_host}에서 찍은 기준점입니다. 그 장비 카메라로 찍은 영상에 쓰는 값이며, "
                "이 장비의 실카메라 화면에는 맞지 않습니다"
            )
        return self.quality, tuple(lines)

    def summary(self) -> str:
        x, y, z = self.camera_position_mm
        pan, tilt, _roll = self.orientation_degrees
        return (
            f"오차 {self.residual_mm:.0f}mm ({self.quality}), "
            f"위치 ({x:.0f}, {y:.0f}, {z:.0f})mm, 팬 {pan:.1f}° 틸트 {tilt:.1f}°"
        )

    def to_dict(self) -> dict[str, Any]:
        x, y, z = self.camera_position_mm
        pan, tilt, roll = self.orientation_degrees
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "camera_ground_pose",
            "camera_id": self.camera_id,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "rotation_degrees": self.rotation_degrees,
            "pallet": {"length_mm": self.pallet_length_mm, "width_mm": self.pallet_width_mm},
            "rvec": list(self.rvec),
            "tvec": list(self.tvec),
            "camera_position_mm": [x, y, z],
            "orientation_degrees": {"pan": pan, "tilt": tilt, "roll": roll},
            "corner_points_normalized": [list(point) for point in self.corner_points],
            "corner_world_mm": [list(point) for point in self.corner_world],
            "stopper_point_normalized": list(self.stopper_point),
            "residual_mm": self.residual_mm,
            "quality": self.quality,
            "intrinsics_camera_id": self.intrinsics_camera_id,
            "intrinsics_borrowed": self.intrinsics_borrowed,
            "intrinsics_source_host": self.intrinsics_source_host,
            "measured_at": self.measured_at,
            "source_host": self.source_host or socket.gethostname(),
            "reviewed": self.reviewed,
            "safe_to_operate": False,
        }


def order_corners(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Order four clicked points counter-clockwise around their centroid.

    The operator may click in any order; the deck is convex, so the angular order around the
    centroid is the order around the rectangle.
    """
    if len(points) != REQUIRED_CORNERS:
        raise ValueError(f"네 모서리가 필요합니다: {len(points)}개")
    cx = sum(point[0] for point in points) / 4.0
    cy = sum(point[1] for point in points) / 4.0
    return sorted(points, key=lambda point: math.atan2(point[1] - cy, point[0] - cx))


def _undistort_normalized(
    points: Sequence[tuple[float, float]], matrix, distortion, width: int, height: int
):
    import cv2
    import numpy as np

    pixels = np.array([[p[0] * width, p[1] * height] for p in points], dtype=np.float64)
    mapped = cv2.undistortPoints(pixels.reshape(-1, 1, 2), matrix, distortion, P=matrix)
    return mapped.reshape(-1, 2)


def scale_camera_matrix(matrix, source_size: tuple[int, int], target_size: tuple[int, int]):
    """Move a camera matrix measured at ``source_size`` to ``target_size``."""
    import numpy as np

    scaled = np.asarray(matrix, dtype=np.float64).copy()
    if source_size != target_size:
        scale_x = target_size[0] / float(source_size[0])
        scale_y = target_size[1] / float(source_size[1])
        scaled[0, 0] *= scale_x
        scaled[0, 2] *= scale_x
        scaled[1, 1] *= scale_y
        scaled[1, 2] *= scale_y
    return scaled


def solve_ground_pose(
    corner_points: Sequence[tuple[float, float]],
    stopper_point: tuple[float, float],
    *,
    camera_id: str,
    camera_matrix,
    distortion,
    image_size: tuple[int, int],
    pallet_length_mm: float,
    pallet_width_mm: float,
    rotation_degrees: int = 0,
    intrinsics_camera_id: str = "",
    intrinsics_borrowed: bool = False,
    intrinsics_source_host: str = "",
    now: datetime | None = None,
) -> GroundPoseResult:
    """Solve the camera pose from four clicked deck corners plus the stopper landmark.

    ``corner_points`` and ``stopper_point`` are normalized image coordinates on the **raw**
    (distorted) preview; they are undistorted here so the pinhole solve is valid.
    """
    import cv2
    import numpy as np

    ordered = order_corners(corner_points)
    width, height = image_size
    matrix = np.asarray(camera_matrix, dtype=np.float64)
    dist = np.asarray(distortion, dtype=np.float64)

    pixels = _undistort_normalized(ordered, matrix, dist, width, height)
    stopper = _undistort_normalized([stopper_point], matrix, dist, width, height)[0]

    half_x, half_y = pallet_length_mm / 2.0, pallet_width_mm / 2.0
    # The deck rectangle, counter-clockwise, starting at (-x, -y).
    rectangle = [(-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)]

    best: tuple[float, Any, Any, list[tuple[float, float]]] | None = None
    # The clicked order is counter-clockwise in the image; the world rectangle may be traversed
    # either way depending on which side the camera is on, and the starting corner is unknown.
    for flip in (False, True):
        world_cycle = rectangle if not flip else list(reversed(rectangle))
        for shift in range(4):
            assignment = [world_cycle[(shift + index) % 4] for index in range(4)]
            object_points = np.array([[x, y, 0.0] for x, y in assignment], dtype=np.float64)
            ok, rvec, tvec = cv2.solvePnP(
                object_points, pixels.astype(np.float64), matrix, np.zeros(5), flags=cv2.SOLVEPNP_IPPE
            )
            if not ok:
                continue
            projected, _ = cv2.projectPoints(object_points, rvec, tvec, matrix, np.zeros(5))
            pixel_error = float(np.mean(np.linalg.norm(projected.reshape(-1, 2) - pixels, axis=1)))
            rotation, _ = cv2.Rodrigues(rvec)
            if (rotation.T @ (-tvec.reshape(3)))[2] <= 0:
                continue  # camera below the deck — not a real mounting
            # The stopper landmark must land on the -x half of the pallet.
            homography = matrix @ np.column_stack([rotation[:, 0], rotation[:, 1], tvec.reshape(3)])
            try:
                world_stopper = np.linalg.inv(homography) @ np.array([stopper[0], stopper[1], 1.0])
            except np.linalg.LinAlgError:
                continue
            if world_stopper[2] == 0:
                continue
            world_stopper = world_stopper[:2] / world_stopper[2]
            if world_stopper[0] >= 0:
                continue  # stopper ended up on the entry side: wrong assignment
            if best is None or pixel_error < best[0]:
                best = (pixel_error, rvec, tvec, assignment)

    if best is None:
        raise ValueError(
            "네 모서리로 카메라 자세를 풀지 못했습니다. 모서리 순서나 스토퍼 위치를 다시 확인해 주세요."
        )

    pixel_error, rvec, tvec, assignment = best
    # Convert the pixel residual into millimetres on the ground, using the deck's own scale.
    rotation, _ = cv2.Rodrigues(rvec)
    homography = matrix @ np.column_stack([rotation[:, 0], rotation[:, 1], tvec.reshape(3)])
    inverse = np.linalg.inv(homography)
    world = inverse @ np.column_stack([pixels, np.ones(len(pixels))]).T
    world = (world[:2] / world[2]).T
    residual_mm = float(np.mean(np.linalg.norm(world - np.array(assignment), axis=1)))

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return GroundPoseResult(
        camera_id=camera_id,
        image_width=width,
        image_height=height,
        rotation_degrees=rotation_degrees,
        pallet_length_mm=pallet_length_mm,
        pallet_width_mm=pallet_width_mm,
        rvec=tuple(float(v) for v in np.asarray(rvec).reshape(3)),
        tvec=tuple(float(v) for v in np.asarray(tvec).reshape(3)),
        corner_points=tuple(ordered),
        stopper_point=tuple(stopper_point),
        corner_world=tuple(assignment),
        residual_mm=residual_mm,
        intrinsics_camera_id=intrinsics_camera_id or camera_id,
        intrinsics_borrowed=intrinsics_borrowed,
        intrinsics_source_host=intrinsics_source_host,
        measured_at=stamp,
    )


def project_ground_points(
    result: GroundPoseResult,
    points_3d: Sequence[tuple[float, float, float]],
    *,
    camera_matrix,
    distortion,
    image_size: tuple[int, int] | None = None,
) -> list[tuple[float, float]]:
    """Project world points back onto the **raw** preview (lens distortion included).

    Used for the on-screen overlay, so the operator sees the fit on the picture they clicked.
    """
    import cv2
    import numpy as np

    width, height = image_size or (result.image_width, result.image_height)
    matrix = scale_camera_matrix(camera_matrix, (result.image_width, result.image_height), (width, height))
    projected, _ = cv2.projectPoints(
        np.array(points_3d, dtype=np.float64).reshape(-1, 3),
        np.array(result.rvec, dtype=np.float64),
        np.array(result.tvec, dtype=np.float64),
        matrix,
        np.asarray(distortion, dtype=np.float64),
    )
    return [(float(x) / width, float(y) / height) for x, y in projected.reshape(-1, 2)]


def pallet_outline(result: GroundPoseResult) -> list[tuple[float, float, float]]:
    half_x, half_y = result.pallet_length_mm / 2.0, result.pallet_width_mm / 2.0
    return [(-half_x, -half_y, 0.0), (half_x, -half_y, 0.0), (half_x, half_y, 0.0), (-half_x, half_y, 0.0)]


def pallet_grid(result: GroundPoseResult, step_mm: float = 500.0) -> list[list[tuple[float, float, float]]]:
    """Metric grid over the deck — the operator can see at a glance whether the scale is right."""
    half_x, half_y = result.pallet_length_mm / 2.0, result.pallet_width_mm / 2.0
    lines: list[list[tuple[float, float, float]]] = []
    value = -half_x
    while value <= half_x + 1e-6:
        lines.append([(value, -half_y, 0.0), (value, half_y, 0.0)])
        value += step_mm
    value = -half_y
    while value <= half_y + 1e-6:
        lines.append([(-half_x, value, 0.0), (half_x, value, 0.0)])
        value += step_mm
    return lines


@dataclass
class GroundCalibrationStore:
    """``<root>/<camera_id>.json`` holds the latest ground pose for that camera."""

    root: Path = field(default_factory=lambda: DEFAULT_GROUND_ROOT)

    def path_for(self, camera_id: str) -> Path:
        return Path(self.root) / f"{camera_id}.json"

    def save(self, result: GroundPoseResult) -> Path:
        target = self.path_for(result.camera_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = target.with_suffix(".json.part")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
        return target

    def load(self, camera_id: str) -> GroundPoseResult | None:
        path = self.path_for(camera_id)
        if not path.is_file():
            return None
        try:
            return result_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None


def result_from_dict(data: Mapping[str, Any]) -> GroundPoseResult:
    if data.get("kind") != "camera_ground_pose":
        raise ValueError("not a camera ground pose file")
    pallet = data.get("pallet") or {}
    # Files written before 2026-09-18 glued the measuring host into the camera id
    # ("opposite_side@bench", or just the bare host name). Split it back apart on load so the
    # lens file stays findable.
    lens_camera = str(data.get("intrinsics_camera_id", ""))
    lens_host = str(data.get("intrinsics_source_host", ""))
    if "@" in lens_camera:
        lens_camera, _, glued = lens_camera.partition("@")
        lens_host = lens_host or glued

    return GroundPoseResult(
        camera_id=str(data["camera_id"]),
        image_width=int(data["image_width"]),
        image_height=int(data["image_height"]),
        rotation_degrees=int(data.get("rotation_degrees", 0)),
        pallet_length_mm=float(pallet.get("length_mm", 5350.0)),
        pallet_width_mm=float(pallet.get("width_mm", 2200.0)),
        rvec=tuple(float(v) for v in data["rvec"]),
        tvec=tuple(float(v) for v in data["tvec"]),
        corner_points=tuple(tuple(float(v) for v in row) for row in data.get("corner_points_normalized", ())),
        stopper_point=tuple(float(v) for v in data.get("stopper_point_normalized", (0.0, 0.0))),
        corner_world=tuple(tuple(float(v) for v in row) for row in data.get("corner_world_mm", ())),
        residual_mm=float(data.get("residual_mm", 0.0)),
        intrinsics_camera_id=lens_camera,
        intrinsics_borrowed=bool(data.get("intrinsics_borrowed", False)),
        intrinsics_source_host=lens_host,
        measured_at=str(data.get("measured_at", "")),
        source_host=str(data.get("source_host", "")),
        reviewed=bool(data.get("reviewed", False)),
    )
