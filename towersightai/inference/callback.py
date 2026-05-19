from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from towersightai.inference.events import normalize_hailo_detections

DEFAULT_MIN_CONFIDENCE = 0.3


def run(video_frame: Any):
    """hailopython entrypoint that emits normalized TowerSightAI events.

    Runtime configuration is provided through environment variables because the
    Hailo GStreamer element imports this module by name:

    - ``TOWERSIGHTAI_HAILO_CAMERA_ID``: camera ID for single-image/single-stream smoke tests.
    - ``TOWERSIGHTAI_HAILO_EVENT_PATH``: optional JSONL sink for normalized events.
    - ``TOWERSIGHTAI_HAILO_MIN_CONFIDENCE``: low confidence cutoff; default 0.3.
    """

    gst = importlib.import_module("gi.repository.Gst")
    detections = _extract_detections(video_frame)
    if not detections:
        return gst.FlowReturn.OK

    camera_id = _camera_id(video_frame)
    min_confidence = float(os.environ.get("TOWERSIGHTAI_HAILO_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))
    events = normalize_hailo_detections(
        detections,
        camera_id=camera_id,
        timestamp=datetime.now(timezone.utc),
        min_confidence=min_confidence,
    )
    _write_events(events)
    _print_summary(events)
    return gst.FlowReturn.OK


def close() -> None:
    print("TowerSightAI Hailo callback closed")


def _extract_detections(video_frame: Any) -> list[Any]:
    roi = getattr(video_frame, "roi", None)
    if roi is None:
        return []
    hailo = importlib.import_module("hailo")
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    return list(detections or [])


def _camera_id(video_frame: Any) -> str:
    configured = os.environ.get("TOWERSIGHTAI_HAILO_CAMERA_ID")
    if configured:
        return configured
    for attr in ("camera_id", "stream_id", "source_id"):
        value = getattr(video_frame, attr, None)
        if value is not None:
            return str(value() if callable(value) else value)
    return "sample_image"


def _write_events(events: tuple[Any, ...]) -> None:
    event_path = os.environ.get("TOWERSIGHTAI_HAILO_EVENT_PATH")
    if not event_path or not events:
        return
    path = Path(event_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _print_summary(events: tuple[Any, ...]) -> None:
    if not events:
        return
    labels = ", ".join(f"{event.label}:{event.confidence:.2f}" for event in events)
    print(f"TowerSightAI detections [{events[0].camera_id}]: {labels}")
