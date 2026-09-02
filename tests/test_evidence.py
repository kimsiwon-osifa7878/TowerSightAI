from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from towersightai.config.settings import CameraConfig, CameraRole, RawStorageConfig
from towersightai.storage.evidence import EvidenceCoordinator, _Fragment


class FakeImage:
    def copy(self):
        return self

    def save(self, path: str, _format: str, _quality: int) -> bool:
        Path(path).write_bytes(b"jpeg-evidence")
        return True


def _camera() -> CameraConfig:
    return CameraConfig(id="front", role=CameraRole.front, rtsp_url="rtsp://example.invalid/live")


def test_real_vehicle_event_writes_snapshot_and_hashed_video_without_base64(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    artifacts: list[dict] = []
    failures: list[dict] = []
    now = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True),
        [_camera()],
        artifact_callback=lambda **item: artifacts.append(item),
        failure_callback=lambda **item: failures.append(item),
        clock=lambda: now,
    )
    coordinator.update_camera_status("front", "정상 수신")
    coordinator.update_frame("front", FakeImage(), received_at=now)
    segment = tmp_path / ".buffer/front/segment-0001.mkv"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"matroska-h264")
    coordinator._recorder_ready.add("front")
    coordinator._fragments["front"].append(_Fragment(segment, now, 2.0))

    coordinator.handle_raw_event(
        {
            "event_id": "event-1",
            "event_type": "vehicle_entered",
            "recorded_at": now.isoformat(),
            "payload": {"camera_id": "front", "simulated": False},
        }
    )
    session = next(iter(coordinator._sessions.values()))
    coordinator._schedule_finalize(session, "front")
    coordinator.close()

    assert not failures
    assert {item["kind"] for item in artifacts} == {"snapshot", "video"}
    assert all(len(item["sha256"]) == 64 for item in artifacts)
    assert all("base64" not in item for item in artifacts)


def test_simulated_event_creates_no_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    artifacts: list[dict] = []
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True),
        [_camera()],
        artifact_callback=lambda **item: artifacts.append(item),
        failure_callback=lambda **item: None,
    )
    coordinator.handle_raw_event(
        {
            "event_id": "sim-1",
            "event_type": "vehicle_entered",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"camera_id": "front", "simulated": True},
        }
    )
    coordinator.close()
    assert artifacts == []


def test_plate_event_preserves_source_and_bbox_crop_without_video(tmp_path: Path, monkeypatch):
    from PyQt6.QtGui import QColor, QImage

    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    source = tmp_path / "lpr-source.png"
    image = QImage(120, 60, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    assert image.save(str(source), "PNG")
    artifacts: list[dict] = []
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path / "raw", timezone_name="UTC", media_enabled=True),
        [_camera()],
        artifact_callback=lambda **item: artifacts.append(item),
        failure_callback=lambda **item: None,
    )
    now = datetime.now(timezone.utc)
    coordinator.handle_raw_event(
        {
            "event_id": "plate-1",
            "event_type": "plate_recognized",
            "recorded_at": now.isoformat(),
            "payload": {
                "camera_id": "front",
                "source_image_path": str(source),
                "plate_bbox": {"x1": 10, "y1": 10, "x2": 90, "y2": 45},
                "simulated": False,
            },
        }
    )
    coordinator.close()
    assert {item["kind"] for item in artifacts} == {"plate_image", "plate_crop"}


def test_managed_vehicle_session_stays_open_until_session_end(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    now = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True),
        [_camera()],
        artifact_callback=lambda **item: None,
        failure_callback=lambda **item: None,
        clock=lambda: now,
    )
    coordinator.update_camera_status("front", "정상 수신")
    coordinator.update_frame("front", FakeImage(), received_at=now)
    coordinator._recorder_ready.add("front")

    coordinator.handle_raw_event(
        {
            "event_id": "managed-1",
            "event_type": "vehicle_entered",
            "recorded_at": now.isoformat(),
            "payload": {"camera_id": "front", "simulated": False, "managed": True},
        }
    )
    session = coordinator._sessions[coordinator._vehicle_session_id]
    assert session.close_at is None  # stays open well past the legacy 10 s post-roll

    from datetime import timedelta

    end_at = now + timedelta(seconds=95)
    coordinator.handle_raw_event(
        {
            "event_id": "managed-2",
            "event_type": "vehicle_session_ended",
            "recorded_at": end_at.isoformat(),
            "payload": {"reason": "parking_started"},
        }
    )
    assert session.close_at == end_at
    assert coordinator._vehicle_session_id is None
    coordinator.close()


def test_legacy_vehicle_session_keeps_fixed_post_roll(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    now = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    config = RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True)
    coordinator = EvidenceCoordinator(
        config,
        [_camera()],
        artifact_callback=lambda **item: None,
        failure_callback=lambda **item: None,
        clock=lambda: now,
    )
    coordinator.update_camera_status("front", "정상 수신")
    coordinator.update_frame("front", FakeImage(), received_at=now)
    coordinator._recorder_ready.add("front")
    coordinator.handle_raw_event(
        {
            "event_id": "legacy-1",
            "event_type": "vehicle_entered",
            "recorded_at": now.isoformat(),
            "payload": {"camera_id": "front", "simulated": False},
        }
    )
    from datetime import timedelta

    session = next(iter(coordinator._sessions.values()))
    assert session.close_at == now + timedelta(seconds=config.media_vehicle_post_seconds)
    assert coordinator._vehicle_session_id is None
    coordinator.close()


def test_person_window_close_captures_end_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    artifacts: list[dict] = []
    now = datetime(2026, 9, 2, 11, 0, 0, tzinfo=timezone.utc)
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True),
        [_camera()],
        artifact_callback=lambda **item: artifacts.append(item),
        failure_callback=lambda **item: None,
        clock=lambda: now,
    )
    coordinator.update_camera_status("front", "정상 수신")
    coordinator.update_frame("front", FakeImage(), received_at=now)
    coordinator._recorder_ready.add("front")
    coordinator.handle_raw_event(
        {
            "event_id": "pw-1",
            "event_type": "person_window_started",
            "recorded_at": now.isoformat(),
            "payload": {},
        }
    )
    coordinator.handle_raw_event(
        {
            "event_id": "pw-2",
            "event_type": "person_window_closed",
            "recorded_at": now.isoformat(),
            "payload": {},
        }
    )
    coordinator.close()
    snapshot_paths = [item["relative_path"] for item in artifacts if item["kind"] == "snapshot"]
    assert any("-person-" in path for path in snapshot_paths)
    assert any("-person_end-" in path for path in snapshot_paths)
