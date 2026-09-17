"""팔레트 실치수에 카메라 자세를 직접 맞춘다 (보정된 화면 기준).

**왜 새로 만들었나** (2026-09-16 사용자 지적)
이전 지면 교정은 *왜곡 보정 전* 화면에서 눈으로 읽은 팔레트 모서리를 좌표만 옮긴 것이었다.
먼 쪽 모서리는 잔차가 149 px이나 됐고, 그 값을 씨앗으로 자세를 풀었으니 화각·위치가 맞을 리
없었다. 순서를 바로잡는다.

1. 영상을 왜곡 보정한다 → 팔레트의 직선이 **진짜 직선**이 된다.
2. 팔레트 데크를 두른 **노란 테두리**를 자동 검출해 직선으로 맞춘다. 길이 방향 선은
   y = ±1,100 mm, 끝 방향 선은 x = ±2,675 mm (승인도 주차구획 5,350 × 2,200).
3. 측정된 내부 파라미터 K를 그대로 쓰고, 이 **선 대응**만으로 6자유도 자세를 푼다.
   손으로 읽은 점은 한 개도 쓰지 않는다.
4. 결과를 팔레트 좌표계에서 읽어 준다 — 카메라가 팔레트 기준으로 **어디에(x,y,z) 어느
   각도로(팬·틸트·롤)** 있는지. 그 다음에야 차량 직육면체를 추정한다.

팔레트는 턴테이블 위에서 회전한다. 여기서 맞추는 것은 **입고 홈 포지션**의 팔레트다
(배경 프레임을 홈 포지션으로 군집해 두었다).

    .venv/bin/python -m vehicle_box_test.pallet_fit
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from vehicle_box_test import draw, rails as rail_detect
from vehicle_box_test.geometry import (
    DEFAULT_CALIB_PATH,
    CameraPose,
    GroundCalibration,
    GroundModel,
    SiteCalibration,
    _nelder_mead,
)
from vehicle_box_test.undistort import load_undistorts

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"
CAMERAS = ("front", "rear_side", "opposite_side")

#: 길이 방향과 끝 방향을 가르는 각도 (도). 팔레트는 직사각형이라 두 방향이 크게 갈린다.
DIRECTION_SPLIT_DEGREES = 35.0
MIN_STRIP_AREA = 2500
#: 자세 해가 물리적으로 말이 되는 범위 (mm).
CAMERA_HEIGHT_RANGE = (600.0, 6000.0)


@dataclass
class DetectedLine:
    """검출된 노란 테두리 한 줄."""

    points: list[tuple[float, float]]  # 정규화 영상좌표
    angle: float  # 도, 0~180
    length: float  # 정규화 길이
    world_axis: str = ""  # "y" | "x"
    world_value: float = 0.0

    @property
    def midpoint(self) -> tuple[float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))


def detect_pallet_lines(image, *, max_strips: int = 8) -> list[DetectedLine]:
    """노란 테두리 띠들을 찾아 방향별로 분류한다."""
    import numpy as np

    strips = rail_detect.detect_strips(image, max_strips=max_strips, samples=50)
    lines: list[DetectedLine] = []
    for strip in strips:
        if strip.area < MIN_STRIP_AREA or len(strip.centerline) < 6:
            continue
        points = np.array(strip.centerline, dtype=np.float64)
        centred = points - points.mean(axis=0)
        _u, _s, vt = np.linalg.svd(centred)
        direction = vt[0]
        angle = math.degrees(math.atan2(direction[1], direction[0])) % 180.0
        extent = float(np.ptp(centred @ direction))
        lines.append(DetectedLine(points=[tuple(p) for p in points], angle=angle, length=extent))
    return lines


def assign_world_lines(lines: Sequence[DetectedLine], ground: GroundModel) -> list[DetectedLine]:
    """검출된 줄에 월드 직선(y=±1100 / x=±2675)을 붙인다.

    가장 긴 줄의 방향을 '길이 방향'으로 보고, 그와 크게 어긋난 줄을 '끝 방향'으로 본다.
    각 방향에서 화면상 바깥쪽 두 줄만 팔레트 테두리로 채택한다 — 안쪽 트랙 테두리는
    월드 y를 모르므로 쓰지 않는다.
    """
    if not lines:
        return []
    longitudinal_angle = max(lines, key=lambda line: line.length).angle

    def difference(angle: float) -> float:
        delta = abs(angle - longitudinal_angle) % 180.0
        return min(delta, 180.0 - delta)

    longitudinal = [line for line in lines if difference(line.angle) <= DIRECTION_SPLIT_DEGREES]
    transverse = [line for line in lines if difference(line.angle) > DIRECTION_SPLIT_DEGREES]

    assigned: list[DetectedLine] = []
    if len(longitudinal) >= 2:
        # 길이 방향 줄들을 '수직 거리' 순으로 세워 바깥 두 줄을 고른다.
        radians = math.radians(longitudinal_angle)
        normal = (-math.sin(radians), math.cos(radians))
        ordered = sorted(longitudinal, key=lambda line: line.midpoint[0] * normal[0] + line.midpoint[1] * normal[1])
        for line, value in ((ordered[0], -ground.pallet_width_mm / 2), (ordered[-1], ground.pallet_width_mm / 2)):
            line.world_axis, line.world_value = "y", value
            assigned.append(line)
    if transverse:
        radians = math.radians(longitudinal_angle)
        along = (math.cos(radians), math.sin(radians))
        ordered = sorted(transverse, key=lambda line: line.midpoint[0] * along[0] + line.midpoint[1] * along[1])
        picks = [(ordered[0], -ground.pallet_length_mm / 2)]
        if len(ordered) >= 2:
            picks.append((ordered[-1], ground.pallet_length_mm / 2))
        for line, value in picks:
            line.world_axis, line.world_value = "x", value
            assigned.append(line)
    return assigned


def fit_pose_to_lines(
    seed: CameraPose, assigned: Sequence[DetectedLine], *, iterations: int = 2500
) -> tuple[CameraPose, float]:
    """선 대응만으로 6자유도 자세를 맞춘다. 반환 잔차는 mm RMS."""
    import cv2
    import numpy as np

    samples = [(u, v, line.world_axis, line.world_value) for line in assigned for u, v in line.points]
    if len(samples) < 12:
        return seed, float("inf")
    image_points = np.array([[u, v] for u, v, _axis, _value in samples], dtype=np.float64)
    axis_index = np.array([0 if axis == "x" else 1 for _u, _v, axis, _value in samples])
    targets = np.array([value for _u, _v, _axis, value in samples], dtype=np.float64)

    def build(params) -> CameraPose:
        rotation, _ = cv2.Rodrigues(np.asarray(params[:3], dtype=np.float64))
        return CameraPose(
            fx=seed.fx,
            fy=seed.fy,
            cx=seed.cx,
            cy=seed.cy,
            aspect=seed.aspect,
            rotation=rotation,
            translation=np.asarray(params[3:], dtype=np.float64),
            borrowed_intrinsics=seed.borrowed_intrinsics,
        )

    def cost(params) -> float:
        candidate = build(params)
        if candidate.translation[2] <= 0:
            return 1e12
        centre = np.asarray(candidate.center, dtype=np.float64)
        if not (CAMERA_HEIGHT_RANGE[0] <= centre[2] <= CAMERA_HEIGHT_RANGE[1]):
            return 1e12
        try:
            inverse = np.linalg.inv(candidate.ground_homography())
        except np.linalg.LinAlgError:
            return 1e12
        pts = np.column_stack([image_points, np.ones(len(image_points))]).T
        world = inverse @ pts
        with np.errstate(divide="ignore", invalid="ignore"):
            world = world[:2] / world[2]
        world = world.T
        if not np.all(np.isfinite(world)):
            return 1e12
        residual = world[np.arange(len(world)), axis_index] - targets
        return float(np.mean(residual**2))

    rvec, _ = cv2.Rodrigues(np.asarray(seed.rotation, dtype=np.float64))
    start = np.concatenate([np.asarray(rvec).reshape(3), np.asarray(seed.translation).reshape(3)])
    best, value = _nelder_mead(
        cost, start, step=np.array([0.08, 0.08, 0.08, 300.0, 300.0, 300.0]), iterations=iterations
    )
    pose = build(best)
    residual = math.sqrt(max(value, 0.0))
    pose.reprojection_error = residual
    return pose, residual


def pose_angles(pose: CameraPose) -> tuple[float, float, float]:
    """팔레트 좌표계에서 본 카메라 방향 (팬, 틸트, 롤) — 도 단위.

    팬 = +x(진입 방향)에서 시계 반대로 잰 광축의 수평 방위, 틸트 = 수평면 아래로 내려본 각,
    롤 = 영상 가로축이 지면 수평에서 기운 각.
    """
    import numpy as np

    rotation = np.asarray(pose.rotation, dtype=np.float64)
    optical_axis = rotation.T @ np.array([0.0, 0.0, 1.0])  # 월드에서 본 카메라가 보는 방향
    pan = math.degrees(math.atan2(optical_axis[1], optical_axis[0]))
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, -optical_axis[2]))))
    right = rotation.T @ np.array([1.0, 0.0, 0.0])
    roll = math.degrees(math.asin(max(-1.0, min(1.0, right[2]))))
    return (pan, tilt, roll)


def seed_pose(calib: GroundCalibration, undistort, width: int, height: int) -> CameraPose:
    """씨앗 자세. 이미 풀어 둔 자세가 있으면 쓰고, 없으면 팔레트 위를 내려다보는 기본값."""
    import cv2
    import numpy as np

    if calib.pose is not None:
        return calib.pose
    matrix = undistort.matrix_for(width, height)
    rotation, _ = cv2.Rodrigues(np.array([2.2, 0.0, 0.0], dtype=np.float64))  # 대략 내려다봄
    return CameraPose(
        fx=float(matrix[0, 0]) / width,
        fy=float(matrix[1, 1]) / width,
        cx=float(matrix[0, 2]) / width,
        cy=float(matrix[1, 2]) / width,
        aspect=height / float(width),
        rotation=rotation,
        translation=np.array([0.0, 1500.0, 5000.0]),
        borrowed_intrinsics=getattr(undistort, "borrowed_from", ""),
    )


def render_fit(image, pose: CameraPose, ground: GroundModel, assigned: Sequence[DetectedLine], label: str):
    """맞춘 결과를 눈으로 확인할 이미지: 검출한 테두리 + 투영한 팔레트 + 500 mm 격자."""
    canvas = image.copy()
    height, width = canvas.shape[:2]

    class _Projector:
        def world_to_image(self, points):
            return pose.project([(x, y, 0.0) for x, y in points])

    projector = _Projector()
    for line in assigned:
        color = (0, 215, 255) if line.world_axis == "y" else (120, 255, 180)
        draw.polyline(canvas, line.points, color, 2)

    # 500 mm 격자 — 지면 축척이 맞는지 한눈에 보인다.
    half_x, half_y = ground.pallet_length_mm / 2, ground.pallet_width_mm / 2
    step = 500.0
    value = -half_x
    while value <= half_x + 1e-6:
        draw.polyline(canvas, projector.world_to_image([(value, -half_y), (value, half_y)]), (200, 120, 255), 1)
        value += step
    value = -half_y
    while value <= half_y + 1e-6:
        draw.polyline(canvas, projector.world_to_image([(-half_x, value), (half_x, value)]), (200, 120, 255), 1)
        value += step
    draw.polyline(canvas, projector.world_to_image(ground.pallet_rect()), (60, 80, 255), 3, closed=True)

    for point, text in (
        ((half_x, 0.0), "+x 진입"),
        ((-half_x, 0.0), "-x 안쪽"),
        ((0.0, half_y), "+y"),
        ((0.0, -half_y), "-y"),
    ):
        draw.label_at(canvas, text, projector.world_to_image([point])[0], size=max(width // 70, 16))

    centre = pose.center
    pan, tilt, roll = pose_angles(pose)
    draw.side_panel(
        canvas,
        [
            f"카메라 위치 (팔레트 중심 기준) x {centre[0]:+.0f} · y {centre[1]:+.0f} · z {centre[2]:+.0f} mm",
            f"방향 팬 {pan:+.1f}° · 틸트 {tilt:+.1f}°(아래) · 롤 {roll:+.1f}°",
            f"팔레트 테두리 잔차 {pose.reprojection_error:.0f} mm",
            f"사용한 테두리 {len(assigned)}줄 "
            f"(길이방향 {sum(1 for l in assigned if l.world_axis == 'y')} · 끝 {sum(1 for l in assigned if l.world_axis == 'x')})",
            f"내부 파라미터 {'빌려 씀: ' + pose.borrowed_intrinsics if pose.borrowed_intrinsics else '자체 측정'}",
            "빨간 사각형 = 팔레트 5,350×2,200 · 분홍 격자 500 mm",
        ],
        title=f"{label} 팔레트 기준 카메라 자세",
        size=max(width // 95, 13),
    )
    return canvas


def run(args: argparse.Namespace) -> int:
    import cv2

    data_root = Path(args.data).resolve()
    calib_path = Path(args.calib)
    site = SiteCalibration.load(calib_path)
    site.space = "undistorted"
    undistorts = load_undistorts(CAMERAS)
    out_root = data_root / "calib"
    out_root.mkdir(parents=True, exist_ok=True)

    for camera in CAMERAS:
        background = data_root / "background" / f"{camera}-snapshot.jpg"
        if not background.is_file():
            print(f"  {camera}: 배경 이미지가 없습니다")
            continue
        image = cv2.imread(str(background))  # 배경은 이미 보정본
        entry = undistorts.get(camera)
        if image is None or entry is None:
            print(f"  {camera}: 이미지 또는 내부 파라미터 없음")
            continue
        height, width = image.shape[:2]

        lines = detect_pallet_lines(image)
        assigned = assign_world_lines(lines, site.ground)
        axes = {line.world_axis for line in assigned}
        if len(assigned) < 3 or axes != {"x", "y"}:
            print(
                f"  {camera}: 팔레트 테두리를 충분히 못 찾았습니다 "
                f"(검출 {len(lines)}줄 · 채택 {len(assigned)}줄 · 축 {sorted(axes)})"
            )
            continue

        calib = site.cameras.get(camera) or GroundCalibration(camera_id=camera)
        pose, residual = fit_pose_to_lines(seed_pose(calib, entry, width, height), assigned)
        centre = pose.center
        pan, tilt, roll = pose_angles(pose)
        pose.note = (
            f"팔레트 테두리 {len(assigned)}줄에 직접 맞춤 (손으로 읽은 점 없음) · "
            f"{entry.label()} · 왜곡 보정 후 좌표계"
        )
        calib.pose = pose
        calib.note = "팔레트 실치수(5,350×2,200)에 자세를 직접 맞춤 · 왜곡 보정 후"
        calib.correspondences = []
        calib._matrix = None
        site.cameras[camera] = calib

        print(
            f"  {camera:14s} 잔차 {residual:6.0f} mm · 테두리 {len(assigned)}줄 · "
            f"위치 x={centre[0]:7.0f} y={centre[1]:7.0f} z={centre[2]:6.0f} · "
            f"팬 {pan:+6.1f}° 틸트 {tilt:+5.1f}° 롤 {roll:+5.1f}° · {entry.label()}"
        )
        target = out_root / f"pallet-{camera}.jpg"
        cv2.imwrite(str(target), render_fit(image, pose, site.ground, assigned, camera), [cv2.IMWRITE_JPEG_QUALITY, 92])

    site.save(calib_path)
    print(f"저장: {calib_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="팔레트 실치수에 카메라 자세 맞추기")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--calib", default=str(DEFAULT_CALIB_PATH))
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
