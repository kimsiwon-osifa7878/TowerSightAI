from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


# ---- radar window evidence ----------------------------------------------------------------


def _radar_coordinator(tmp_path: Path, monkeypatch, now: datetime, **config):
    monkeypatch.setattr(EvidenceCoordinator, "_start_recorder", lambda self, camera: None)
    artifacts: list[dict] = []
    failures: list[dict] = []
    clock = {"now": now}
    coordinator = EvidenceCoordinator(
        RawStorageConfig(local_dir=tmp_path, timezone_name="UTC", media_enabled=True, **config),
        [_camera()],
        artifact_callback=lambda **item: artifacts.append(item),
        failure_callback=lambda **item: failures.append(item),
        clock=lambda: clock["now"],
    )
    coordinator.update_camera_status("front", "정상 수신")
    coordinator.update_frame("front", FakeImage(), received_at=now)
    coordinator._recorder_ready.add("front")
    return coordinator, artifacts, failures, clock


def _radar_event(event_type: str, event_id: str, at: datetime, **payload) -> dict:
    return {"event_id": event_id, "event_type": event_type, "recorded_at": at.isoformat(), "payload": payload}


def test_radar_window_start_captures_snapshot_and_capped_clip(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    coordinator, artifacts, failures, _clock = _radar_coordinator(tmp_path, monkeypatch, now)

    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now, radar_window_id="w1"))
    session = coordinator._sessions[coordinator._radar_session_id]
    assert session.kind == "radar"
    assert session.close_at == now + timedelta(seconds=30)
    coordinator._executor.shutdown(wait=True)
    snapshot = next(item for item in artifacts if item["kind"] == "snapshot")
    assert "-radar-front.jpg" in snapshot["relative_path"]
    assert snapshot["related_event_id"] == "rw-1"
    assert snapshot["metadata"]["event_kind"] == "radar"
    assert not failures


def test_radar_window_close_adds_end_snapshot_and_keeps_the_clip_cap(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    coordinator, artifacts, failures, clock = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now))
    session_id = coordinator._radar_session_id

    # A long window: closed 2 minutes later → clip stays capped at +30 s, end snapshot still taken.
    later = now + timedelta(seconds=120)
    clock["now"] = later
    coordinator.update_frame("front", FakeImage(), received_at=later)
    coordinator.handle_raw_event(_radar_event("radar_window_closed", "rw-2", later))
    assert coordinator._radar_session_id is None
    assert coordinator._sessions[session_id].close_at == now + timedelta(seconds=30)
    coordinator._executor.shutdown(wait=True)
    kinds = sorted(item["metadata"]["event_kind"] for item in artifacts if item["kind"] == "snapshot")
    assert kinds == ["radar", "radar_end"]  # executor order is not deterministic
    end = next(item for item in artifacts if item["metadata"]["event_kind"] == "radar_end")
    assert end["related_event_id"] == "rw-2" and "-radar_end-front.jpg" in end["relative_path"]

    # A short window closes the clip at the close event, not the cap.
    coordinator2, _a, _f, clock2 = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator2.handle_raw_event(_radar_event("radar_window_started", "rw-3", now))
    short_close = now + timedelta(seconds=8)
    coordinator2.handle_raw_event(_radar_event("radar_window_closed", "rw-4", short_close))
    assert next(iter(coordinator2._sessions.values())).close_at == short_close


def test_radar_window_during_person_window_records_reason_instead_of_media(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    coordinator, artifacts, failures, _clock = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator.handle_raw_event(_radar_event("person_window_started", "pw-1", now))
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now))
    coordinator._executor.shutdown(wait=True)
    assert coordinator._radar_session_id is None
    assert [f["reason"] for f in failures] == ["person_window_active"]
    assert failures[0]["related_event_id"] == "rw-1" and failures[0]["kind"] == "snapshot"
    assert all(item["metadata"]["event_kind"] == "person" for item in artifacts if item["kind"] == "snapshot")


def test_radar_evidence_is_throttled_to_the_minimum_interval(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    coordinator, artifacts, failures, clock = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now))
    clock["now"] = now + timedelta(seconds=5)
    coordinator.update_frame("front", FakeImage(), received_at=clock["now"])
    coordinator.handle_raw_event(_radar_event("radar_window_closed", "rw-2", clock["now"]))

    second = now + timedelta(seconds=40)
    clock["now"] = second
    coordinator.update_frame("front", FakeImage(), received_at=second)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-3", second))
    assert [f["reason"] for f in failures] == ["radar_evidence_throttled"]
    assert coordinator._radar_session_id is None

    third = now + timedelta(seconds=61)
    clock["now"] = third
    coordinator.update_frame("front", FakeImage(), received_at=third)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-5", third))
    assert coordinator._radar_session_id is not None
    coordinator._executor.shutdown(wait=True)
    radar_snapshots = [i for i in artifacts if i["metadata"]["event_kind"] == "radar"]
    assert [i["related_event_id"] for i in radar_snapshots] == ["rw-1", "rw-5"]


def test_radar_evidence_disabled_or_simulated_does_nothing(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    coordinator, artifacts, failures, _clock = _radar_coordinator(tmp_path, monkeypatch, now, media_radar_evidence=False)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now))
    assert coordinator._radar_session_id is None and not artifacts and not failures

    coordinator, artifacts, failures, _clock = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", now, simulated=True))
    coordinator._executor.shutdown(wait=True)
    assert coordinator._radar_session_id is None and not artifacts and not failures


def test_radar_snapshots_use_the_live_clock_not_the_backdated_window_time(tmp_path: Path, monkeypatch):
    """Radar window times are backdated (start = first present sample). Stamping the snapshot with
    that time made the frame-freshness check reject every camera — field data 2026-09-16 had 70
    latest_frame_missing_or_stale failures and zero radar snapshots."""
    now = datetime(2026, 9, 16, 1, 30, 46, tzinfo=timezone.utc)
    coordinator, artifacts, failures, clock = _radar_coordinator(tmp_path, monkeypatch, now)
    coordinator.update_frame("front", FakeImage(), received_at=now)
    backdated = now - timedelta(seconds=3)  # the window opened 3 s ago
    coordinator.handle_raw_event(_radar_event("radar_window_started", "rw-1", backdated, radar_window_id="w1"))
    coordinator.close()

    assert not [item for item in failures if item["reason"] == "latest_frame_missing_or_stale"]
    snapshots = [item for item in artifacts if item["kind"] == "snapshot" and item["metadata"]["event_kind"] == "radar"]
    assert len(snapshots) == 1 and snapshots[0]["captured_at"] == now
    # The clip still starts from the backdated moment so the pre-roll covers the real onset.
    assert coordinator._sessions == {} or all(s.started_at == backdated for s in coordinator._sessions.values())
