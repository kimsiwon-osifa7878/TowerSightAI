from __future__ import annotations

from datetime import datetime, timezone

from towersightai.inference.events import BoundingBox, normalize_hailo_detection, normalize_hailo_detections


class FakeBBox:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self._x = x
        self._y = y
        self._w = w
        self._h = h

    def xmin(self) -> float:
        return self._x

    def ymin(self) -> float:
        return self._y

    def width(self) -> float:
        return self._w

    def height(self) -> float:
        return self._h


class FakeDetection:
    def __init__(self, label: str, confidence: float, bbox) -> None:  # noqa: ANN001 - bbox mimics Hailo object shapes.
        self._label = label
        self._confidence = confidence
        self._bbox = bbox

    def get_label(self) -> str:
        return self._label

    def get_confidence(self) -> float:
        return self._confidence

    def get_bbox(self):  # noqa: ANN201 - fake follows Hailo API style.
        return self._bbox


def test_normalize_hailo_detection_accepts_hailo_style_bbox_methods():
    timestamp = datetime(2026, 5, 18, tzinfo=timezone.utc)
    detection = FakeDetection("person", 0.91, FakeBBox(0.1, 0.2, 0.3, 0.4))

    event = normalize_hailo_detection(detection, camera_id="front", timestamp=timestamp, min_confidence=0.3)

    assert event is not None
    assert event.camera_id == "front"
    assert event.label == "person"
    assert event.confidence == 0.91
    assert event.bbox == BoundingBox(0.1, 0.2, 0.3, 0.4)
    assert event.to_dict()["source"] == "hailo"


def test_normalize_hailo_detections_drops_low_confidence_and_invalid_boxes():
    timestamp = datetime(2026, 5, 18, tzinfo=timezone.utc)
    detections = (
        FakeDetection("car", 0.88, {"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4}),
        FakeDetection("person", 0.12, {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}),
        FakeDetection("truck", 0.92, {"x": 0.9, "y": 0.9, "w": 0.3, "h": 0.3}),
    )

    events = normalize_hailo_detections(detections, camera_id="sample_image", timestamp=timestamp, min_confidence=0.3)

    assert len(events) == 1
    assert events[0].label == "car"
