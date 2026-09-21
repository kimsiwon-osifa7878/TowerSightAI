"""이벤트 샤드에서 차량 검출 상자를 뽑아 시각별 색인으로 만든다.

배경 차분만으로는 **흰 차가 밝은 데크 위에 있을 때** 실루엣이 거의 잡히지 않는다
(2026-09-18 검증: 차량 진입 스냅샷 49장 중 상당수가 이 이유로 실패). 그런데 현장기는
이미 같은 프레임에 Hailo YOLO 차량 검출을 돌려 그 결과를 원천 데이터에 남겨 두었다.

그 상자를 쓰면

* 실루엣을 차량 영역으로 **가둘 수 있고** (바닥 매트 무늬·문 밖 행인 오검출 제거),
* 상자 아랫변을 지면에 투영해 **앞끝·뒤끝**을 직접 읽을 수 있다 (사선 카메라에서
  아랫변은 가까운 쪽 타이어의 접지선이다).

제품이 실제로 가지고 있는 신호만 쓰는 셈이라, 여기서 되는 것은 제품에서도 된다.

    .venv/bin/python -m vehicle_box_test.detections
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"

VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle"})
#: 스냅샷 시각과 검출 시각의 허용 간격(초). 검출은 초당 여러 번이라 1초면 충분하다.
MATCH_SECONDS = 1.0
MIN_CONFIDENCE = 0.35


@dataclass(frozen=True)
class DetectionBox:
    """정규화 좌표계(0~1)의 차량 상자."""

    camera_id: str
    at: float  # epoch seconds
    label: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bottom_edge(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return (self.x0, self.y1), (self.x1, self.y1)

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "at": self.at,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "box": [round(v, 5) for v in (self.x0, self.y0, self.x1, self.y1)],
        }


def _parse_time(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _read_lines(path: Path) -> Iterator[str]:
    """샤드 한 개를 읽는다. gzip 여부는 매직 바이트로 판별하고, 잘린 파일은 읽은 데까지 쓴다."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        stream = gzip.GzipFile(fileobj=io.BytesIO(raw))
        chunks: list[bytes] = []
        try:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, EOFError):
            pass  # 업로드 중 잘린 샤드 — 읽은 데까지가 유효하다
        data = b"".join(chunks)
    else:
        data = raw
    yield from data.decode("utf-8", "replace").splitlines()


def collect(events_root: Path, days: Sequence[str]) -> list[DetectionBox]:
    boxes: list[DetectionBox] = []
    wanted = tuple(days)
    for path in sorted(events_root.rglob("*")):
        if not path.is_file() or not path.name.endswith((".jsonl", ".gz")):
            continue
        if wanted and not any(day in str(path) for day in wanted):
            continue
        for line in _read_lines(path):
            if '"detection_batch"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event_type") != "detection_batch":
                continue
            for detection in record.get("payload", {}).get("detections", ()):
                label = str(detection.get("label", ""))
                if label not in VEHICLE_LABELS:
                    continue
                confidence = float(detection.get("confidence", 0.0))
                if confidence < MIN_CONFIDENCE:
                    continue
                at = _parse_time(str(detection.get("timestamp", "")))
                bbox = detection.get("bbox") or {}
                if at is None or not bbox:
                    continue
                x, y = float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))
                w, h = float(bbox.get("w", 0.0)), float(bbox.get("h", 0.0))
                if w <= 0.0 or h <= 0.0:
                    continue
                boxes.append(
                    DetectionBox(
                        camera_id=str(detection.get("camera_id", "")),
                        at=at,
                        label=label,
                        confidence=confidence,
                        x0=x,
                        y0=y,
                        x1=x + w,
                        y1=y + h,
                    )
                )
    boxes.sort(key=lambda b: (b.camera_id, b.at))
    return boxes


class DetectionIndex:
    """(카메라, 시각) → 그 순간의 차량 상자."""

    def __init__(self, boxes: Sequence[DetectionBox]) -> None:
        self._by_camera: dict[str, list[DetectionBox]] = {}
        for box in boxes:
            self._by_camera.setdefault(box.camera_id, []).append(box)
        for items in self._by_camera.values():
            items.sort(key=lambda b: b.at)
        self._times = {camera: [b.at for b in items] for camera, items in self._by_camera.items()}

    @classmethod
    def load(cls, path: Path) -> "DetectionIndex":
        if not path.is_file():
            return cls(())
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            [
                DetectionBox(
                    camera_id=item["camera_id"],
                    at=float(item["at"]),
                    label=item.get("label", "car"),
                    confidence=float(item.get("confidence", 0.0)),
                    **dict(zip(("x0", "y0", "x1", "y1"), (float(v) for v in item["box"]))),
                )
                for item in payload.get("boxes", ())
            ]
        )

    def at(self, camera_id: str, when: str | float, *, window: float = MATCH_SECONDS) -> list[DetectionBox]:
        """그 시각 ±window 안의 차량 상자들 (신뢰도 내림차순)."""
        moment = _parse_time(when) if isinstance(when, str) else float(when)
        items = self._by_camera.get(camera_id) or []
        if moment is None or not items:
            return []
        times = self._times[camera_id]
        start = bisect_left(times, moment - window)
        found = []
        for index in range(start, len(items)):
            if times[index] > moment + window:
                break
            found.append(items[index])
        found.sort(key=lambda b: -b.confidence)
        return found

    def best(self, camera_id: str, when: str | float, *, window: float = MATCH_SECONDS) -> DetectionBox | None:
        """그 시각의 **가장 큰** 차량 상자. 가장 가까운 차 = 주차기 안의 차다."""
        found = self.at(camera_id, when, window=window)
        if not found:
            return None
        return max(found, key=lambda b: (b.x1 - b.x0) * (b.y1 - b.y0))


def run(args: argparse.Namespace) -> int:
    data_root = Path(args.data).resolve()
    sample_index = json.loads((data_root / "sample-index.json").read_text(encoding="utf-8"))
    days = sorted({item["day"] for item in sample_index["items"]})
    events_root = data_root / "events"
    print(f"[1/2] 샤드에서 차량 검출 수집 · 대상 날짜 {', '.join(days)}")
    boxes = collect(events_root, days)
    by_camera: dict[str, int] = {}
    for box in boxes:
        by_camera[box.camera_id] = by_camera.get(box.camera_id, 0) + 1
    for camera, count in sorted(by_camera.items()):
        print(f"  {camera:14s} {count:7d}개")

    target = data_root / "detection-index.json"
    target.write_text(
        json.dumps(
            {"days": days, "count": len(boxes), "boxes": [b.to_dict() for b in boxes]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"[2/2] 색인 저장: {target} ({len(boxes)}개)")

    index = DetectionIndex(boxes)
    matched = 0
    snapshots = [i for i in sample_index["items"] if i.get("kind") == "snapshot" and i.get("local_path")]
    for item in snapshots:
        if index.best(item["camera_id"], item.get("captured_at", "")) is not None:
            matched += 1
    print(f"      스냅샷 {matched}/{len(snapshots)}장에 차량 상자가 붙었습니다")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="차량 검출 상자 색인 생성")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
