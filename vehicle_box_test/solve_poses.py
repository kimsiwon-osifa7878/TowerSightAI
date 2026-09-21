"""측정된 내부 파라미터 + 자동 검출한 레일로 카메라 자세를 풀어 저장한다.

1차 검증에서 3D 직육면체를 못 그린 이유는 호모그래피와 카메라 자세가 서로 어긋나서였다.
운영자 콘솔에서 체커보드로 내부 파라미터를 측정한 뒤로는 순서가 달라진다.

1. 영상을 **왜곡 보정**한다 → 레일이 직선이 되고 핀홀 모델이 화면 전체에서 성립한다.
2. 측정된 K를 **그대로 써서** solvePnP로 자세를 푼다 (초점거리를 더 이상 추정하지 않는다).
3. 자동 검출한 노란 레일(= y를 아는 직선) 전체로 자세를 다듬는다. 손으로 읽어야 했던
   팔레트 먼 쪽 모서리가 필요 없어지고, 그 모서리의 읽기 오차도 사라진다.
4. 지면 호모그래피를 **자세에서 만든다** → 투영과 정의상 일치 → 직육면체를 그릴 수 있다.

    .venv/bin/python -m vehicle_box_test.solve_poses
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from vehicle_box_test import rails as rail_detect
from vehicle_box_test.calibrate import reference_image
from vehicle_box_test.geometry import (
    DEFAULT_CALIB_PATH,
    GroundCalibration,
    SiteCalibration,
    pose_from_ground_file,
    pose_from_intrinsics,
    refine_pose_with_rails,
)
from vehicle_box_test.undistort import load_undistorts

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"

#: 카메라마다 어떤 노란 띠가 어느 world y인지. 띠는 화면에서 세로 위치(중심 v)로 고른다.
#: 데크 폭 2,200 = 바깥 가장자리 ±1,100, 트랙 안쪽 노란 띠 ±440 (1차 검증에서 실측).
RAIL_PLAN = {
    # 측면 카메라는 가까운 레일의 바깥 노란 띠 하나만 확실히 보인다. 트랙 안쪽 띠의 y는
    # 승인도에 없고 1차 검증의 추정치(±440)는 왜곡 때문에 틀렸으므로 쓰지 않는다 —
    # 폭 축척은 x=-2675의 기준점 두 개(= 데크 폭 2,200 mm)가 준다.
    "opposite_side": (("largest", -1100.0),),
    "rear_side": (("largest", 1100.0),),
    # 전면 카메라는 네 띠가 모두 보인다 (좌→우).
    "front": (("left_outer", 1100.0), ("left_inner", 440.0), ("right_inner", -440.0), ("right_outer", -1100.0)),
}


def rail_samples(camera: str, image) -> list[tuple[float, float, float]]:
    """(u, v, world_y) 표본. 계획표대로 검출된 띠에 y를 붙인다."""
    strips = rail_detect.detect_strips(image, max_strips=4)
    if not strips:
        return []
    plan = RAIL_PLAN.get(camera, ())
    by_area = sorted(strips, key=lambda s: -s.area)
    by_x = sorted(strips, key=lambda s: s.centroid[0])
    samples: list[tuple[float, float, float]] = []
    for slot, world_y in plan:
        strip = None
        if slot == "largest":
            strip = by_area[0]
        elif slot == "above_largest":
            # 가장 큰 띠와 가로 위치가 비슷하면서 화면에서 더 위에 있는 띠 = 같은 트랙의 반대쪽 가장자리
            reference = by_area[0]
            candidates = [
                s
                for s in strips
                if s is not reference and abs(s.centroid[0] - reference.centroid[0]) < 0.08
            ]
            if candidates:
                strip = min(candidates, key=lambda s: s.centroid[1])
        elif slot.startswith(("left", "right")) and len(by_x) >= 4:
            strip = {"left_outer": by_x[0], "left_inner": by_x[1], "right_inner": by_x[2], "right_outer": by_x[3]}[slot]
        if strip is not None:
            samples += [(u, v, world_y) for u, v in strip.centerline]
    return samples


def run(args: argparse.Namespace) -> int:
    import cv2

    data_root = Path(args.data).resolve()
    calib_path = Path(args.calib)
    site = SiteCalibration.load(calib_path)
    if site.space != "undistorted":
        raise SystemExit("먼저 `calibrate migrate`로 대응점을 보정 좌표계로 옮기세요.")
    # 운영자가 `지면 기준점`에서 찍어 둔 카메라는 랩 교정 파일에 없더라도 포함한다 —
    # 현장기가 찍은 값을 개발기에서 현장 영상 분석에 그대로 쓰기 위해서다.
    from towersightai.calibration.share import LIBRARY_DIR

    operator_cameras = {
        path.stem
        for path in Path("data/calibration/ground").glob("*.json")
        if path.parent.name != LIBRARY_DIR
    }
    cameras = tuple(dict.fromkeys(tuple(site.cameras) + tuple(sorted(operator_cameras))))
    undistorts = load_undistorts(cameras + ("rear_side",))

    for camera in cameras:
        calib = site.cameras.get(camera) or GroundCalibration(camera_id=camera)
        site.cameras[camera] = calib
        entry = undistorts.get(camera)
        if entry is None:
            print(f"  {camera}: 내부 파라미터가 없어 건너뜁니다")
            continue
        image = reference_image(data_root, camera, undistorts)
        if image is None:
            print(f"  {camera}: 기준 이미지를 찾지 못했습니다")
            continue

        # 운영자가 `지면 기준점` 페이지에서 팔레트 모서리를 직접 찍어 둔 것이 있으면 그것을 쓴다.
        # 랩의 어떤 자동 추정보다 우선한다 — x 기준이 추측이 아니기 때문이다.
        operator_pose = pose_from_ground_file(camera)
        if operator_pose is not None:
            calib.pose = operator_pose
            calib._matrix = None
            centre = operator_pose.center
            print(
                f"  {camera:14s} 운영자 기준점 사용 · 잔차 {operator_pose.reprojection_error:5.0f} mm · "
                f"위치 x={centre[0]:7.0f} y={centre[1]:7.0f} z={centre[2]:6.0f} mm"
            )
            continue

        seed = pose_from_intrinsics(calib, entry)
        if seed is None:
            print(f"  {camera}: solvePnP 실패")
            continue
        samples = rail_samples(camera, image)
        anchors = [(c[0], c[1], c[2], c[3]) for c in calib.correspondences]
        if args.anchor_side == "near":
            # 먼 쪽 모서리는 읽기 오차가 커서 레일로 대체한다 (측면 카메라).
            near = [a for a in anchors if a[0] < 0]
            anchors = near or anchors
        pose = refine_pose_with_rails(seed, samples, anchors) if samples else seed
        pose.note = (
            f"레일 표본 {len(samples)}개 + 기준점 {len(anchors)}개 · "
            f"{entry.label()} · 왜곡 보정 후 좌표계"
        )
        calib.pose = pose
        calib._matrix = None
        center = pose.center
        print(
            f"  {camera:14s} 잔차 {pose.reprojection_error:6.0f} mm · 레일 표본 {len(samples):3d} · "
            f"카메라 위치 x={center[0]:7.0f} y={center[1]:7.0f} z={center[2]:6.0f} mm · {entry.label()}"
        )

    site.save(calib_path)
    print(f"저장: {calib_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="측정된 내부 파라미터로 카메라 자세 풀기")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--calib", default=str(DEFAULT_CALIB_PATH))
    parser.add_argument("--anchor-side", choices=("near", "all"), default="near")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
