from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class BoundingBox:
    """Normalized detection box in x/y/width/height format."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name, value in (("x", self.x), ("y", self.y), ("w", self.w), ("h", self.h)):
            if value < 0:
                raise ValueError(f"BoundingBox {name} must be non-negative.")
        if self.x > 1 or self.y > 1 or self.w > 1 or self.h > 1:
            raise ValueError("BoundingBox values must be normalized to 0..1.")
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError("BoundingBox must fit inside normalized image coordinates.")

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class DetectionEvent:
    """Hardware-independent detection event emitted by Hailo callbacks."""

    camera_id: str
    label: str
    confidence: float
    bbox: BoundingBox
    timestamp: datetime
    source: str = "hailo"

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("DetectionEvent camera_id is required.")
        if not self.label:
            raise ValueError("DetectionEvent label is required.")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("DetectionEvent confidence must be in 0..1.")
        if self.timestamp.tzinfo is None:
            raise ValueError("DetectionEvent timestamp must be timezone-aware.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "label": self.label,
            "confidence": self.confidence,
            "bbox": self.bbox.to_dict(),
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }


def normalize_hailo_detection(
    detection: Any,
    *,
    camera_id: str,
    timestamp: datetime | None = None,
    min_confidence: float = 0.0,
) -> DetectionEvent | None:
    """Convert one Hailo-like detection object into a conservative internal event.

    Returns ``None`` for low-confidence or structurally invalid detections so
    callers can keep the runtime in NG/wait instead of treating uncertainty as OK.
    """

    label = str(_read_value(detection, ("get_label", "label")) or "").strip()
    confidence_raw = _read_value(detection, ("get_confidence", "confidence"))
    if not label or confidence_raw is None:
        return None
    confidence = float(confidence_raw)
    if confidence < min_confidence:
        return None

    bbox_raw = _read_value(detection, ("get_bbox", "bbox"))
    if bbox_raw is None:
        return None
    bbox = _normalize_bbox(bbox_raw)
    if bbox is None:
        return None

    return DetectionEvent(
        camera_id=camera_id,
        label=label,
        confidence=confidence,
        bbox=bbox,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def normalize_hailo_detections(
    detections: Iterable[Any],
    *,
    camera_id: str,
    timestamp: datetime | None = None,
    min_confidence: float = 0.0,
) -> tuple[DetectionEvent, ...]:
    event_time = timestamp or datetime.now(timezone.utc)
    events = []
    for detection in detections:
        event = normalize_hailo_detection(
            detection,
            camera_id=camera_id,
            timestamp=event_time,
            min_confidence=min_confidence,
        )
        if event is not None:
            events.append(event)
    return tuple(events)


def _normalize_bbox(bbox: Any) -> BoundingBox | None:
    values = _bbox_values(bbox)
    if values is None:
        return None
    x, y, w, h = values
    try:
        return BoundingBox(x=float(x), y=float(y), w=float(w), h=float(h))
    except (TypeError, ValueError):
        return None


def _bbox_values(bbox: Any) -> tuple[Any, Any, Any, Any] | None:
    if isinstance(bbox, dict):
        if {"x", "y", "w", "h"}.issubset(bbox):
            return bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        if {"xmin", "ymin", "width", "height"}.issubset(bbox):
            return bbox["xmin"], bbox["ymin"], bbox["width"], bbox["height"]
    if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
        return bbox[0], bbox[1], bbox[2], bbox[3]

    x = _read_value(bbox, ("x", "xmin", "get_xmin"))
    y = _read_value(bbox, ("y", "ymin", "get_ymin"))
    w = _read_value(bbox, ("w", "width", "get_width"))
    h = _read_value(bbox, ("h", "height", "get_height"))
    if None not in (x, y, w, h):
        return x, y, w, h

    xmin = _read_value(bbox, ("xmin", "get_xmin"))
    ymin = _read_value(bbox, ("ymin", "get_ymin"))
    xmax = _read_value(bbox, ("xmax", "get_xmax"))
    ymax = _read_value(bbox, ("ymax", "get_ymax"))
    if None not in (xmin, ymin, xmax, ymax):
        return xmin, ymin, float(xmax) - float(xmin), float(ymax) - float(ymin)
    return None


def _read_value(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        return value() if callable(value) else value
    return None
