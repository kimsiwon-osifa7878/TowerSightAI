from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from towersightai.inference.events import normalize_hailo_detections


def main() -> int:
    args = _parse_args()
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    resources = Path(args.resources).expanduser().resolve(strict=False)
    hef_path = Path(args.hef).expanduser().resolve(strict=False)
    postprocess_so = Path(args.postprocess).expanduser().resolve(strict=False)
    event_path = Path(args.event_path).expanduser().resolve(strict=False)
    cameras = _parse_cameras(args.camera)
    rotations = _parse_rotations(args.rotation)

    sys.path.insert(0, str(workspace))
    stream_id_so = _find_stream_id_so(resources)
    _configure_tappas_postprocess(stream_id_so)
    os.environ["HAILO_ARCH"] = args.arch

    import hailo  # type: ignore[import-not-found]
    from hailo_apps.python.core.gstreamer import gstreamer_app
    from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class
    from hailo_apps.python.pipeline_apps.multisource import multisource_pipeline

    gstreamer_app.GST_VIDEO_SINK = "fakesink"
    original_get_resource_path = multisource_pipeline.get_resource_path

    def explicit_resource_path(pipeline_name, resource_type, arch=None, model=None):
        if model == "libyolo_hailortpp_postprocess.so":
            return postprocess_so
        return original_get_resource_path(pipeline_name, resource_type, arch, model)

    multisource_pipeline.get_resource_path = explicit_resource_path
    event_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_labels = {label.strip().lower() for label in args.allowed_label if label.strip()}
    stream_to_camera = {f"src_{index}": camera_id for index, (camera_id, _url) in enumerate(cameras)}
    write_lock = threading.Lock()

    def callback(_element, buffer, _user_data):
        if buffer is None:
            return
        roi = hailo.get_roi_from_buffer(buffer)
        stream_id = str(roi.get_stream_id() or "")
        camera_id = stream_to_camera.get(stream_id, stream_id or "unknown")
        events = normalize_hailo_detections(
            roi.get_objects_typed(hailo.HAILO_DETECTION),
            camera_id=camera_id,
            timestamp=datetime.now(timezone.utc),
            min_confidence=args.min_confidence,
        )
        if allowed_labels:
            events = tuple(event for event in events if event.label.lower() in allowed_labels)
        events = tuple(_rotate_event(event, rotations.get(camera_id, 0)) for event in events)
        records = [
            {
                **event.to_dict(),
                "source": "hailo_apps",
            }
            for event in events
        ]
        if records:
            with write_lock, event_path.open("a", encoding="utf-8") as fp:
                for record in records:
                    fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            print(
                f"TowerSightAI Hailo Apps detections [{camera_id}]: "
                + ", ".join(f"{record['label']}:{record['confidence']:.2f}" for record in records),
                flush=True,
            )

    sys.argv = [
        sys.argv[0],
        "--sources",
        ",".join(url for _camera_id, url in cameras),
        "--hef-path",
        str(hef_path),
        "--arch",
        args.arch,
        "--disable-sync",
    ]
    print(
        "TowerSightAI Hailo Apps adapter: "
        f"workspace={workspace} resources={resources} hef={hef_path} "
        f"postprocess={postprocess_so} stream-id-so={stream_id_so} "
        f"cameras={','.join(camera_id for camera_id, _url in cameras)}",
        flush=True,
    )
    user_data = app_callback_class()
    app = multisource_pipeline.GStreamerMultisourceApp(callback, user_data)
    if _all_sources_are_files(cameras):
        app.on_eos = app.shutdown
    app.run()
    return 0


def _all_sources_are_files(cameras: tuple[tuple[str, str], ...]) -> bool:
    return bool(cameras) and all(Path(url).expanduser().is_file() for _camera_id, url in cameras)


def _configure_tappas_postprocess(stream_id_so: Path) -> None:
    postprocess_dir = str(stream_id_so.parent)
    os.environ["TAPPAS_POSTPROC_PATH"] = postprocess_dir
    # Hailo Apps 26.03.1 reads the lower-case key from its defines module.
    os.environ["tappas_postproc_path"] = postprocess_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TowerSightAI adapter for the current Hailo Apps multisource API.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--hef", required=True)
    parser.add_argument("--postprocess", required=True)
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--camera", action="append", required=True, help="CAMERA_ID=RTSP_URL")
    parser.add_argument("--rotation", action="append", default=[], help="CAMERA_ID=0|90|180|270")
    parser.add_argument("--allowed-label", action="append", default=[])
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--arch", choices=("hailo8", "hailo8l"), default="hailo8")
    return parser.parse_args()


def _parse_cameras(values: list[str]) -> tuple[tuple[str, str], ...]:
    cameras = []
    for value in values:
        camera_id, separator, url = value.partition("=")
        if not separator or not camera_id.strip() or not url.strip():
            raise ValueError(f"Invalid --camera value: {value!r}")
        cameras.append((camera_id.strip(), url.strip()))
    return tuple(cameras)


def _parse_rotations(values: list[str]) -> dict[str, int]:
    rotations = {}
    for value in values:
        camera_id, separator, rotation_text = value.partition("=")
        if not separator:
            raise ValueError(f"Invalid --rotation value: {value!r}")
        rotation = int(rotation_text)
        if rotation not in {0, 90, 180, 270}:
            raise ValueError(f"Invalid rotation for {camera_id}: {rotation}")
        rotations[camera_id] = rotation
    return rotations


def _rotate_event(event, rotation: int):
    if rotation == 0:
        return event
    from towersightai.inference.events import BoundingBox, DetectionEvent

    box = event.bbox
    if rotation == 90:
        rotated = BoundingBox(x=box.y, y=_unit(1.0 - box.x - box.w), w=box.h, h=box.w)
    elif rotation == 180:
        rotated = BoundingBox(
            x=_unit(1.0 - box.x - box.w),
            y=_unit(1.0 - box.y - box.h),
            w=box.w,
            h=box.h,
        )
    else:
        rotated = BoundingBox(x=_unit(1.0 - box.y - box.h), y=box.x, w=box.h, h=box.w)
    return DetectionEvent(
        camera_id=event.camera_id,
        label=event.label,
        confidence=event.confidence,
        bbox=rotated,
        timestamp=event.timestamp,
        source=event.source,
    )


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _find_stream_id_so(resources: Path) -> Path:
    configured_dir = os.environ.get("TAPPAS_POSTPROC_PATH")
    candidates = []
    if configured_dir:
        candidates.append(Path(configured_dir).expanduser() / "libstream_id_tool.so")
    candidates.append(resources / "so" / "libstream_id_tool.so")
    candidates.append(resources / "libstream_id_tool.so")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    matches = tuple(resources.rglob("libstream_id_tool.so")) if resources.is_dir() else ()
    if matches:
        return matches[0].resolve()
    searched = ", ".join(str(path.resolve(strict=False)) for path in candidates)
    raise FileNotFoundError(f"libstream_id_tool.so not found; searched: {searched}")


if __name__ == "__main__":
    raise SystemExit(main())
