"""지면 캘리브레이션 보조 도구.

체커보드 내부 파라미터가 아직 없으므로, 바닥에 실제로 있는 것(레일 내폭 2,106 mm,
턴테이블 Ø6,100 mm)에 맞춰 **손으로** 대응점을 잡는다. 두 모드로 반복한다.

    ruler : 배경 이미지에 정규화 좌표 눈금을 얹어 저장 → 눈으로 대응점 좌표를 읽는다
    check : site_calibration.json의 대응점으로 지면 모델을 투영해 저장 → 맞는지 눈으로 본다

    .venv/bin/python -m vehicle_box_test.calibrate ruler
    .venv/bin/python -m vehicle_box_test.calibrate check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from vehicle_box_test import draw
from vehicle_box_test.geometry import DEFAULT_CALIB_PATH, SiteCalibration

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"
CAMERAS = ("front", "rear_side", "opposite_side")

#: 캘리브레이션 기준 이미지. 바닥(레일·턴테이블)이 가장 잘 보이는 실제 스냅샷을 쓴다.
#: 중앙값 배경은 차량 잔상이 겹쳐 모서리를 읽기 어려운 카메라가 있어 카메라마다 고른다.
REFERENCE_IMAGES = {
    "front": "media/2026-09-16/013219-498514-plate-front.jpg",
    "rear_side": "media/2026-09-16/023932-005086-person-rear_side.jpg",
    "opposite_side": "background/opposite_side-snapshot.jpg",
}


def reference_path(data_root: Path, camera: str) -> Path:
    candidate = data_root / REFERENCE_IMAGES.get(camera, "")
    if candidate.is_file():
        return candidate
    return background_path(data_root, camera)


def background_path(data_root: Path, camera: str) -> Path:
    for source in ("snapshot", "clip"):
        candidate = data_root / "background" / f"{camera}-{source}.jpg"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{camera} 배경 이미지가 없습니다 (background 모듈을 먼저 실행)")


def run_ruler(data_root: Path, out_root: Path) -> None:
    import cv2

    out_root.mkdir(parents=True, exist_ok=True)
    for camera in CAMERAS:
        image = cv2.imread(str(reference_path(data_root, camera)))
        if image is None:
            continue
        ruled = draw.pixel_ruler(image, 0.05)
        draw.put_text(ruled, f"{camera} · 정규화 좌표 눈금 (가로 u, 세로 v)", (20, image.shape[0] - 60), size=26)
        target = out_root / f"ruler-{camera}.jpg"
        cv2.imwrite(str(target), ruled, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"  {target}")


def run_check(data_root: Path, out_root: Path, calib_path: Path) -> None:
    import cv2

    site = SiteCalibration.load(calib_path)
    out_root.mkdir(parents=True, exist_ok=True)
    for camera in CAMERAS:
        calib = site.cameras.get(camera)
        image = cv2.imread(str(reference_path(data_root, camera)))
        if image is None:
            continue
        if calib is None or len(calib.correspondences) < 4:
            draw.failure_note(image, ["지면 대응점이 아직 없습니다"], title=f"{camera} 미교정")
        else:
            draw.draw_ground_model(image, calib, site.ground)
            error = calib.reprojection_error_mm()
            draw.side_panel(
                image,
                [
                    f"대응점 {len(calib.correspondences)}개",
                    f"재투영 오차 {error:.0f} mm",
                    f"레일 내폭 {site.ground.rail_inner_width_mm:.0f} mm",
                    f"턴테이블 Ø{site.ground.turntable_diameter_mm:.0f} mm",
                    calib.note,
                ],
                title=f"{camera} 지면 교정",
            )
        target = out_root / f"ground-{camera}.jpg"
        cv2.imwrite(str(target), image, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"  {target}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="지면 캘리브레이션 보조")
    parser.add_argument("mode", choices=("ruler", "check"))
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--out", default="")
    parser.add_argument("--calib", default=str(DEFAULT_CALIB_PATH))
    args = parser.parse_args(argv)

    data_root = Path(args.data).resolve()
    out_root = Path(args.out).resolve() if args.out else data_root / "calib"
    if args.mode == "ruler":
        run_ruler(data_root, out_root)
    else:
        run_check(data_root, out_root, Path(args.calib))
    return 0


if __name__ == "__main__":
    sys.exit(main())
