from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from towersightai.inference.events import normalize_hailo_detections

DEFAULT_MIN_CONFIDENCE = 0.3

try:
    import hailo as _hailo

    # TAPPAS requires VideoFrame to be imported before Gst in hailopython modules.
    from gsthailo import VideoFrame as _VideoFrame
    from gi.repository import Gst as _Gst
except ImportError:  # pragma: no cover - hardware runtime dependency
    _hailo = None
    _VideoFrame = Any
    _Gst = None


def run(video_frame: _VideoFrame):
    """hailopython entrypoint that emits normalized TowerSightAI events.

    Runtime configuration is provided through environment variables because the
    Hailo GStreamer element imports this module by name:

    - ``TOWERSIGHTAI_HAILO_CAMERA_ID``: camera ID for single-image/single-stream smoke tests.
    - ``TOWERSIGHTAI_HAILO_EVENT_PATH``: optional JSONL sink for normalized events.
    - ``TOWERSIGHTAI_HAILO_MIN_CONFIDENCE``: low confidence cutoff; default 0.3.
    """

    return run_with_config(
        video_frame,
        camera_id=_camera_id(video_frame),
        event_path=os.environ.get("TOWERSIGHTAI_HAILO_EVENT_PATH"),
        min_confidence=float(os.environ.get("TOWERSIGHTAI_HAILO_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)),
    )


def run_with_config(
    video_frame: _VideoFrame,
    *,
    camera_id: str,
    event_path: str | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    allowed_labels: tuple[str, ...] | None = None,
):
    detections = _extract_detections(video_frame)
    if not detections:
        return _flow_ok()

    events = normalize_hailo_detections(
        detections,
        camera_id=camera_id,
        timestamp=datetime.now(timezone.utc),
        min_confidence=min_confidence,
    )
    if allowed_labels is not None:
        normalized_labels = {label.lower() for label in allowed_labels}
        events = tuple(event for event in events if event.label.lower() in normalized_labels)
    _write_events(events, event_path=event_path)
    _print_summary(events)
    return _flow_ok()


def run_lpr_ocr_with_config(
    video_frame: _VideoFrame,
    *,
    event_path: str | None = None,
    min_confidence: float = 0.0,
    source_image: str | None = None,
    frame_index: int | None = None,
):
    events = _extract_lpr_ocr_events(video_frame, min_confidence=min_confidence)
    _write_lpr_ocr_events(events, event_path=event_path)
    _write_lpr_ocr_attempt(
        events,
        event_path=event_path,
        source_image=source_image,
        frame_index=frame_index,
    )
    if events:
        plates = ", ".join(f"{event['plate_number']}:{event['confidence']:.2f}" for event in events)
        print(f"TowerSightAI LPR image attempt: image={source_image or 'unknown'} frame={frame_index} result={plates}")
    else:
        print(f"TowerSightAI LPR image attempt: image={source_image or 'unknown'} frame={frame_index} result=no_result")
    return _flow_ok()


def close() -> None:
    print("TowerSightAI Hailo callback closed")


def _extract_detections(video_frame: Any) -> list[Any]:
    roi = getattr(video_frame, "roi", None)
    if roi is None:
        return []
    hailo = _hailo if _hailo is not None else importlib.import_module("hailo")
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    return list(detections or [])


def _extract_lpr_ocr_events(video_frame: Any, *, min_confidence: float = 0.0) -> tuple[dict[str, Any], ...]:
    roi = getattr(video_frame, "roi", None)
    if roi is None:
        return ()
    events: list[dict[str, Any]] = []
    timestamp = datetime.now(timezone.utc).isoformat()
    seen: set[tuple[str, float]] = set()
    for classification in _iter_lpr_ocr_classifications(roi):
        label = str(_read_hailo_value(classification, ("get_label", "label")) or "").strip()
        if not label:
            continue
        confidence_raw = _read_hailo_value(classification, ("get_confidence", "confidence"))
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        key = (label, round(confidence, 4))
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "type": "plate_ocr",
                "plate_number": label,
                "confidence": confidence,
                "timestamp": timestamp,
                "source": "hailo_lpr_image",
            }
        )
    return tuple(events)


def _iter_lpr_ocr_classifications(roi: Any) -> list[Any]:
    classifications: list[Any] = []
    for vehicle in _objects_typed(roi, "HAILO_DETECTION"):
        for plate in _objects_typed(vehicle, "HAILO_DETECTION"):
            for classification in _objects_typed(plate, "HAILO_CLASSIFICATION"):
                if _is_lpr_ocr_classification(classification):
                    classifications.append(classification)
    if classifications:
        return classifications
    return _iter_lpr_classifications(roi)


def _iter_lpr_classifications(obj: Any) -> list[Any]:
    classifications: list[Any] = []
    direct = _objects_typed(obj, "HAILO_CLASSIFICATION")
    for classification in direct:
        if _is_lpr_ocr_classification(classification, allow_missing_type=True):
            classifications.append(classification)
    for detection in _objects_typed(obj, "HAILO_DETECTION"):
        classifications.extend(_iter_lpr_classifications(detection))
    return classifications


def _is_lpr_ocr_classification(classification: Any, *, allow_missing_type: bool = False) -> bool:
    classification_type = str(
        _read_hailo_value(classification, ("get_classification_type", "classification_type", "type")) or ""
    ).lower()
    if not classification_type:
        return allow_missing_type
    return classification_type == "ocr"


def _objects_typed(obj: Any, hailo_type_name: str) -> list[Any]:
    get_objects_typed = getattr(obj, "get_objects_typed", None)
    if callable(get_objects_typed):
        try:
            hailo = _hailo if _hailo is not None else importlib.import_module("hailo")
            value = getattr(hailo, hailo_type_name)
            return list(get_objects_typed(value) or [])
        except (ImportError, AttributeError, TypeError):
            pass
    attr_name = "classifications" if hailo_type_name == "HAILO_CLASSIFICATION" else "detections"
    value = getattr(obj, attr_name, None)
    if value is None:
        return []
    value = value() if callable(value) else value
    return list(value or [])


def _read_hailo_value(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        return value() if callable(value) else value
    return None


def _flow_ok() -> Any:
    if _Gst is not None:
        return _Gst.FlowReturn.OK
    return importlib.import_module("gi.repository.Gst").FlowReturn.OK


def _camera_id(video_frame: Any) -> str:
    configured = os.environ.get("TOWERSIGHTAI_HAILO_CAMERA_ID")
    if configured:
        return configured
    for attr in ("camera_id", "stream_id", "source_id"):
        value = getattr(video_frame, attr, None)
        if value is not None:
            return str(value() if callable(value) else value)
    return "sample_image"


def _write_events(events: tuple[Any, ...], *, event_path: str | None = None) -> None:
    event_path = event_path or os.environ.get("TOWERSIGHTAI_HAILO_EVENT_PATH")
    if not event_path or not events:
        return
    path = Path(event_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _write_lpr_ocr_events(events: tuple[dict[str, Any], ...], *, event_path: str | None = None) -> None:
    event_path = event_path or os.environ.get("TOWERSIGHTAI_LPR_EVENT_PATH")
    if not event_path or not events:
        return
    path = Path(event_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _write_lpr_ocr_attempt(
    events: tuple[dict[str, Any], ...],
    *,
    event_path: str | None = None,
    source_image: str | None,
    frame_index: int | None,
) -> None:
    event_path = event_path or os.environ.get("TOWERSIGHTAI_LPR_EVENT_PATH")
    if not event_path:
        return
    path = Path(event_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attempt = {
        "type": "plate_ocr_attempt",
        "source": "hailo_lpr_image",
        "source_image": source_image,
        "frame_index": frame_index,
        "status": "recognized" if events else "no_result",
        "plates": tuple(
            {
                "plate_number": event["plate_number"],
                "confidence": event["confidence"],
            }
            for event in events
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(attempt, ensure_ascii=False, sort_keys=True) + "\n")


def _print_summary(events: tuple[Any, ...]) -> None:
    if not events:
        return
    labels = ", ".join(f"{event.label}:{event.confidence:.2f}" for event in events)
    print(f"TowerSightAI detections [{events[0].camera_id}]: {labels}")
