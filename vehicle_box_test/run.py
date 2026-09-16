"""검증 파이프라인 실행: 표본 이미지 → 추정 → 주석 이미지 → results.json.

    .venv/bin/python -m vehicle_box_test.run
    .venv/bin/python -m vehicle_box_test.run --max-frames-per-clip 4
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from vehicle_box_test import draw, estimate as est
from vehicle_box_test.geometry import DEFAULT_CALIB_PATH, SiteCalibration, pose_from_homography

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = LAB_ROOT / "data"

CAMERA_LABEL = {
    "front": "전면 (카메라 2)",
    "rear_side": "좌측 사선 (카메라 3)",
    "opposite_side": "우측 사선 (카메라 4)",
    "ceiling": "천장 버드뷰 (카메라 1)",
}
EVENT_LABEL = {
    "vehicle": "차량 진입",
    "plate": "번호판 인식",
    "person": "사람 감지",
    "person_end": "사람 감지 종료",
    "radar": "레이더 감지",
    "radar_end": "레이더 종료",
}


def load_backgrounds(data_root: Path) -> dict[tuple[str, str], object]:
    import cv2

    backgrounds: dict[tuple[str, str], object] = {}
    for path in sorted((data_root / "background").glob("*.jpg")):
        stem = path.stem
        if stem.endswith("-empty"):
            continue
        camera, _, source = stem.rpartition("-")
        image = cv2.imread(str(path))
        if image is not None:
            backgrounds[(camera, source)] = image
    return backgrounds


def annotate(image, result: est.VehicleEstimate, blob, polygon, calib, ground, pose, meta: dict):
    """추정 결과를 이미지에 새긴다. 실패면 한국어 사유를, 성공이면 치수를 적는다."""
    import cv2
    import numpy as np

    canvas = image.copy()
    height, width = canvas.shape[:2]

    # 주차기 영역(관심 영역)과 지면 모델
    draw.polyline(canvas, polygon, (110, 110, 110), 1, closed=True)
    left, right = ground.rail_lines()
    draw.polyline(canvas, calib.world_to_image(left), draw.COLOR_RAIL, 1)
    draw.polyline(canvas, calib.world_to_image(right), draw.COLOR_RAIL, 1)
    draw.polyline(canvas, calib.world_to_image(ground.pallet_rect()), draw.COLOR_PALLET, 1, closed=True)

    # 실루엣 외곽선
    if blob is not None and np.count_nonzero(blob):
        contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(canvas, contours, -1, (200, 200, 60), 2, cv2.LINE_AA)

    footprint = result.footprint()
    if footprint is not None:
        base = calib.world_to_image(footprint)
        if result.height_mm and result.pose_used and pose is not None:
            top = pose.project([(x, y, result.height_mm) for x, y in footprint])
            draw.polyline(canvas, base, draw.COLOR_BOX, 3, closed=True)
            draw.polyline(canvas, top, draw.COLOR_BOX_TOP, 2, closed=True)
            for a, b in zip(base, top):
                draw.line(canvas, a, b, draw.COLOR_BOX_TOP, 2)
        else:
            draw.polyline(canvas, base, draw.COLOR_BOX, 3, closed=True)

        # 앞끝 / 뒤끝 / 좌우 바퀴선
        y0, y1 = result.y_right, result.y_left
        front_line = calib.world_to_image([(result.x_front, y0), (result.x_front, y1)])
        rear_line = calib.world_to_image([(result.x_rear, y0), (result.x_rear, y1)])
        draw.polyline(canvas, front_line, draw.COLOR_FRONT, 3)
        draw.polyline(canvas, rear_line, draw.COLOR_REAR, 3)
        for y in (y0, y1):
            draw.polyline(
                canvas, calib.world_to_image([(result.x_rear, y), (result.x_front, y)]), draw.COLOR_WHEEL, 2
            )

        draw.label_at(canvas, "진입쪽 끝 (+x)", front_line[0], size=max(width // 85, 14), color=(160, 190, 255))
        draw.label_at(canvas, "안쪽 끝 (-x)", rear_line[0], size=max(width // 85, 14), color=(255, 200, 150))

        # 치수 화살표 (길이·폭)
        mid_y = y0 + (y1 - y0) * 0.15
        draw.arrow_with_length(
            canvas,
            *calib.world_to_image([(result.x_rear, mid_y), (result.x_front, mid_y)]),
            f"길이 {result.length_mm:.0f} mm",
            draw.COLOR_BOX,
            size=max(width // 75, 15),
        )
        mid_x = result.x_rear + (result.x_front - result.x_rear) * 0.72
        draw.arrow_with_length(
            canvas,
            *calib.world_to_image([(mid_x, y0), (mid_x, y1)]),
            f"폭 {result.width_mm:.0f} mm",
            draw.COLOR_WHEEL,
            size=max(width // 75, 15),
        )

    header = f"{CAMERA_LABEL.get(result.camera_id, result.camera_id)} · {meta.get('day','')} · {EVENT_LABEL.get(meta.get('event_kind',''), meta.get('event_kind',''))}"
    draw.put_text(canvas, header, (int(width * 0.015), int(height * 0.955)), size=max(width // 75, 15))

    if result.ok:
        lines = [
            f"길이 {result.length_mm:.0f} mm",
            f"폭 {result.width_mm:.0f} mm",
            f"높이 {result.height_mm:.0f} mm" if result.height_mm else "높이 미산출 (자세 복원 불충분)",
            f"진입쪽 끝 x {result.x_front:+.0f} mm · 안쪽 끝 x {result.x_rear:+.0f} mm",
            f"중심 치우침 y {(result.y_left + result.y_right) / 2:+.0f} mm",
            f"접지점 {result.contact_samples}개 · 실루엣 {result.silhouette_ratio*100:.1f}%",
        ]
        if result.camera_id in ("rear_side", "opposite_side"):
            lines.append("※ 사선 카메라 한 대만으로는 폭이 부정확합니다 (먼 쪽 바퀴가 안 보임).")
        lines += [f"※ {note}" for note in result.notes]
        draw.side_panel(canvas, lines, title="추정값 (지면 실치수 기준)", size=max(width // 95, 13))
    else:
        draw.failure_note(canvas, result.reasons + result.notes)
    return canvas


def run(args: argparse.Namespace) -> int:
    import cv2

    data_root = Path(args.data).resolve()
    out_root = Path(args.out).resolve() if args.out else LAB_ROOT / "out"
    (out_root / "annotated").mkdir(parents=True, exist_ok=True)

    site = SiteCalibration.load(Path(args.calib))
    backgrounds = load_backgrounds(data_root)
    sample_index = json.loads((data_root / "sample-index.json").read_text(encoding="utf-8"))
    frame_index = json.loads((data_root / "frame-index.json").read_text(encoding="utf-8"))

    targets: list[dict] = []
    for item in sample_index["items"]:
        if item.get("kind") == "snapshot" and item.get("local_path"):
            targets.append(
                {
                    "path": item["local_path"],
                    "camera_id": item["camera_id"],
                    "day": item["day"],
                    "event_kind": item.get("event_kind", ""),
                    "source": "snapshot",
                    "captured_at": item.get("captured_at", ""),
                }
            )
    # 클립은 앞부분(5초 프리롤)에 차가 아직 없다. 클립 전체에 고르게 퍼뜨려 뽑는다.
    by_clip: dict[str, list[dict]] = {}
    for frame in frame_index:
        by_clip.setdefault(frame["clip"], []).append(frame)
    picked: list[dict] = []
    for clip, frames in by_clip.items():
        frames.sort(key=lambda f: f["index"])
        count = min(args.max_frames_per_clip, len(frames))
        if count <= 0:
            continue
        step = len(frames) / count
        picked += [frames[min(int(i * step), len(frames) - 1)] for i in range(count)]
    for frame in picked:
        targets.append(
            {
                "path": frame["path"],
                "camera_id": frame["camera_id"],
                "day": frame["day"],
                "event_kind": frame.get("event_kind", ""),
                "source": "clip",
                "captured_at": f"{frame['clip']} +{frame['seconds']}s",
            }
        )

    poses: dict[str, object] = {}
    pose_errors: dict[str, float] = {}
    results: list[dict] = []
    print(f"대상 이미지 {len(targets)}장")

    for index, target in enumerate(targets, start=1):
        image = cv2.imread(str(data_root / target["path"]))
        if image is None:
            continue
        camera = target["camera_id"]
        calib = site.cameras.get(camera)
        aspect = image.shape[0] / image.shape[1]
        stem = Path(target["path"]).stem
        out_name = f"{camera}-{target['day']}-{target['source']}-{stem}.jpg"
        out_path = out_root / "annotated" / out_name

        blocked: list[str] | None = None
        if calib is None or len(calib.correspondences) < 4:
            blocked = [
                f"{CAMERA_LABEL.get(camera, camera)} 카메라의 지면 교정이 아직 없습니다.",
                "레일·턴테이블 기준점을 아직 이 화각에서 읽지 못해 mm 단위 판정을 못 합니다.",
                "체커보드 내부 파라미터 측정 또는 운영자 점 찍기 UI가 필요합니다.",
            ]
        elif target["source"] != "snapshot":
            blocked = [
                "증거 클립(stream2)은 스냅샷(stream1)과 **화각이 달라** 같은 지면 교정을 쓸 수 없습니다.",
                "Tapo 카메라가 스트림마다 다른 크롭을 내보내는 것으로 확인됐습니다.",
                "클립으로 측정하려면 stream2 화각의 지면 교정을 따로 잡아야 합니다.",
            ]
        if blocked is not None:
            canvas = image.copy()
            draw.failure_note(canvas, blocked, title="측정 보류")
            cv2.imwrite(str(out_path), canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
            record = est.VehicleEstimate(camera_id=camera, source=target["source"])
            record.reasons = blocked
            record.annotated_path = str(out_path.relative_to(out_root))
            results.append({**target, **record.to_dict()})
            continue

        if camera not in poses:
            pose = pose_from_homography(calib, aspect)
            poses[camera] = pose
            pose_errors[camera] = est.pose_agreement(calib, pose, site.ground)
        pose = poses[camera]
        background = backgrounds.get((camera, target["source"]))
        if background is None:
            background = backgrounds.get((camera, "snapshot"))
        if background is None:
            continue

        # 자세가 지면과 크게 어긋나면 아예 쓰지 않는다 (ROI·높이 모두).
        usable_pose = pose if pose_errors[camera] <= est.POSE_AGREEMENT_LIMIT else None
        result, blob, polygon = est.estimate_vehicle(
            image,
            background,
            calib,
            site.ground,
            usable_pose,
            camera_id=camera,
            source=target["source"],
        )
        result.pose_error = round(pose_errors[camera], 4)
        if usable_pose is None:
            result.notes.append(
                f"자세 복원이 지면과 {pose_errors[camera]*100:.0f}% 어긋나 3D 높이는 계산하지 않고"
                " 바닥 사각형만 그렸습니다 (어안 왜곡 · 체커보드 미측정)."
            )

        canvas = annotate(image, result, blob, polygon, calib, site.ground, usable_pose, target)
        cv2.imwrite(str(out_path), canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
        result.annotated_path = str(out_path.relative_to(out_root))
        results.append({**target, **result.to_dict()})
        if index % 20 == 0:
            print(f"  {index}/{len(targets)} 처리")

    summary = {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "ground": {
            "pallet_length_mm": site.ground.pallet_length_mm,
            "pallet_width_mm": site.ground.pallet_width_mm,
            "rail_inner_width_mm": site.ground.rail_inner_width_mm,
            "turntable_diameter_mm": site.ground.turntable_diameter_mm,
        },
        "cameras": {
            camera: {
                "calibrated": camera in site.cameras,
                "note": site.cameras[camera].note if camera in site.cameras else "",
                "pose_error": round(pose_errors.get(camera, float("inf")), 4)
                if camera in pose_errors
                else None,
            }
            for camera in sorted({t["camera_id"] for t in targets})
        },
        "results": results,
    }
    (out_root / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    ok = sum(1 for r in results if r.get("ok"))
    print(f"완료: {ok}/{len(results)} 성공 · 주석 이미지 {out_root/'annotated'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="차량 직육면체 검증 파이프라인")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--out", default="")
    parser.add_argument("--calib", default=str(DEFAULT_CALIB_PATH))
    parser.add_argument("--max-frames-per-clip", type=int, default=4)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
