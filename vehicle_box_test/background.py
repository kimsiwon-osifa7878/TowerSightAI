"""카메라별 '빈 주차기' 배경 이미지를 만든다 (중앙값 누적).

카메라가 고정이므로, 여러 세션·여러 시각의 프레임을 픽셀별 중앙값으로 합치면 차량·사람은
사라지고 바닥·레일·턴테이블만 남는다. 이 배경은 두 곳에 쓴다.

1. 지면 캘리브레이션 — 차에 가리지 않은 레일·원판을 보고 대응점을 잡는다.
2. 차량 실루엣 추출 — 배경차분의 기준.

    .venv/bin/python -m vehicle_box_test.background
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Sequence

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"
#: 중앙값이 배경으로 수렴하려면 최소 이 정도 장수는 있어야 한다.
MIN_FRAMES = 8


def build_median(paths: Sequence[Path], *, max_frames: int) -> "object | None":
    import cv2
    import numpy as np

    picked = list(paths)
    if len(picked) > max_frames:  # 고르게 솎아낸다 (한 구간에 몰리면 차가 남는다)
        step = len(picked) / max_frames
        picked = [picked[int(i * step)] for i in range(max_frames)]

    stack = []
    shape = None
    for path in picked:
        image = cv2.imread(str(path))
        if image is None:
            continue
        if shape is None:
            shape = image.shape
        elif image.shape != shape:
            image = cv2.resize(image, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        stack.append(image)
    if len(stack) < MIN_FRAMES:
        return None
    return np.median(np.stack(stack), axis=0).astype("uint8")


def _least_changed(paths: Sequence[Path], median) -> tuple[Path | None, float]:
    """중앙값 배경과 가장 비슷한 프레임(= 피사체가 가장 적은 프레임)을 고른다."""
    import cv2
    import numpy as np

    best: tuple[Path | None, float] = (None, float("inf"))
    for path in paths:
        image = cv2.imread(str(path))
        if image is None or image.shape != median.shape:
            continue
        score = float(np.mean(cv2.absdiff(image, median)))
        if score < best[1]:
            best = (path, score)
    return best


def run(args: argparse.Namespace) -> int:
    import cv2

    data_root = Path(args.data).resolve()
    frame_index = json.loads((data_root / "frame-index.json").read_text(encoding="utf-8"))
    sample_index = json.loads((data_root / "sample-index.json").read_text(encoding="utf-8"))

    # 저해상도(클립 프레임)와 고해상도(스냅샷)는 따로 배경을 만든다 — 해상도가 다르다.
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for frame in frame_index:
        groups[(frame["camera_id"], "clip")].append(data_root / frame["path"])
    for item in sample_index["items"]:
        if item.get("kind") == "snapshot" and item.get("local_path"):
            groups[(item["camera_id"], "snapshot")].append(data_root / item["local_path"])

    out_root = data_root / "background"
    out_root.mkdir(parents=True, exist_ok=True)
    summary = {}
    for (camera, source), paths in sorted(groups.items()):
        median = build_median(sorted(paths), max_frames=args.max_frames)
        if median is None:
            print(f"  {camera:14s} {source:9s} 프레임 부족({len(paths)}장) — 건너뜀")
            continue
        target = out_root / f"{camera}-{source}.jpg"
        cv2.imwrite(str(target), median, [cv2.IMWRITE_JPEG_QUALITY, 95])
        summary[f"{camera}-{source}"] = {"frames": len(paths), "path": str(target.relative_to(data_root))}
        print(f"  {camera:14s} {source:9s} {len(paths):4d}장 → {target.relative_to(data_root)}")

        # 중앙값에서 가장 덜 벗어난 실제 프레임 = '가장 비어 있는' 한 장. 합성이 아니라
        # 진짜 사진이라 선명해서 캘리브레이션 기준 이미지로 쓴다.
        empty_path, score = _least_changed(sorted(paths), median)
        if empty_path is not None:
            empty_target = out_root / f"{camera}-{source}-empty.jpg"
            cv2.imwrite(str(empty_target), cv2.imread(str(empty_path)), [cv2.IMWRITE_JPEG_QUALITY, 95])
            summary[f"{camera}-{source}-empty"] = {
                "source": str(empty_path.relative_to(data_root)),
                "mean_abs_diff": round(score, 2),
                "path": str(empty_target.relative_to(data_root)),
            }
            print(f"  {'':14s} {'':9s} 빈 프레임 {empty_path.name} (차이 {score:.1f}) → {empty_target.name}")

    (out_root / "index.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="카메라별 빈 주차기 배경(중앙값) 생성")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--max-frames", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
