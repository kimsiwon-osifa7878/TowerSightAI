"""내려받은 MKV 클립에서 일정 간격으로 프레임을 뽑는다 (검증용).

증거 클립은 H.264 패스스루 MKV라 프레임 단위 seek가 부정확할 수 있어, 순차 디코딩하며
간격마다 저장한다. 결과는 ``data/frames/<day>/<clip>/NNNN.jpg``.

    .venv/bin/python -m vehicle_box_test.frames --interval 1.5 --max-per-clip 20
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"


@dataclass
class ExtractedFrame:
    day: str
    camera_id: str
    event_kind: str
    clip: str
    index: int
    seconds: float
    path: str


def extract_clip(
    clip: Path, dest: Path, *, interval: float, max_frames: int
) -> list[tuple[int, float, Path]]:
    import cv2

    capture = cv2.VideoCapture(str(clip))
    if not capture.isOpened():
        return []
    fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
    if fps <= 1.0 or fps > 120.0:
        fps = 15.0  # Tapo stream2 기본값 근처. 메타데이터가 이상하면 가정치를 쓴다.
    step = max(int(round(fps * interval)), 1)
    dest.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[int, float, Path]] = []
    position = 0
    while len(saved) < max_frames:
        ok, frame = capture.read()
        if not ok:
            break
        if position % step == 0:
            target = dest / f"{len(saved):04d}.jpg"
            cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            saved.append((position, position / fps, target))
        position += 1
    capture.release()
    return saved


def run(args: argparse.Namespace) -> int:
    data_root = Path(args.data).resolve()
    index_path = data_root / "sample-index.json"
    if not index_path.is_file():
        raise SystemExit(f"샘플 색인이 없습니다: {index_path} (먼저 nas_sampler를 실행하세요)")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    frames_root = data_root / "frames"
    results: list[ExtractedFrame] = []
    clips = [item for item in index["items"] if item.get("kind") == "video" and item.get("local_path")]
    print(f"클립 {len(clips)}개에서 {args.interval}초 간격으로 최대 {args.max_per_clip}장씩 추출")

    for item in clips:
        clip = data_root / item["local_path"]
        if not clip.is_file():
            print(f"  건너뜀(파일 없음) {item['local_path']}")
            continue
        name = clip.stem
        dest = frames_root / item["day"] / name
        saved = extract_clip(clip, dest, interval=args.interval, max_frames=args.max_per_clip)
        for order, (_, seconds, path) in enumerate(saved):
            results.append(
                ExtractedFrame(
                    day=item["day"],
                    camera_id=item["camera_id"],
                    event_kind=item.get("event_kind", ""),
                    clip=name,
                    index=order,
                    seconds=round(seconds, 2),
                    path=str(path.relative_to(data_root)),
                )
            )
        print(f"  {item['day']} {item['camera_id']:14s} {name} → {len(saved)}장")

    out = data_root / "frame-index.json"
    out.write_text(
        json.dumps([frame.__dict__ for frame in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"총 {len(results)}장 · 색인 {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="증거 클립에서 검증용 프레임을 추출한다.")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--interval", type=float, default=1.5, help="추출 간격(초)")
    parser.add_argument("--max-per-clip", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
