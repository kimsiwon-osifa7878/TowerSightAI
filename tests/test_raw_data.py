from __future__ import annotations

import json
import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from towersightai.inference.events import BoundingBox, DetectionEvent
from towersightai.config.settings import RawStorageConfig
from towersightai.storage.raw_data import RawDataManager


def _event(label: str, timestamp: datetime, camera_id: str = "front") -> DetectionEvent:
    return DetectionEvent(
        camera_id=camera_id,
        label=label,
        confidence=0.91,
        bbox=BoundingBox(0.1, 0.2, 0.3, 0.4),
        timestamp=timestamp,
    )


def _records(root: Path, day: str) -> list[dict]:
    records: list[dict] = []
    for path in sorted((root / day).glob("events*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    for path in sorted((root / day).glob("events*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fp:
            records.extend(json.loads(line) for line in fp)
    return records


def test_raw_storage_config_hides_password_and_validates_host(tmp_path: Path):
    config = RawStorageConfig(
        enabled=True,
        local_dir=tmp_path,
        nas_host="nas.example.com",
        nas_port=45222,
        nas_username="uploader",
        nas_password="top-secret",
        nas_folder="/home/site",
    )

    assert "top-secret" not in repr(config)
    with pytest.raises(ValueError, match="without scheme"):
        RawStorageConfig(
            enabled=True,
            local_dir=tmp_path,
            nas_host="https://nas.example.com",
            nas_username="uploader",
            nas_password="secret",
            nas_folder="/home/site",
        )


def test_records_vehicle_plate_ai_and_raw_detection_in_daily_jsonl(tmp_path: Path):
    now = datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc)
    manager = RawDataManager(RawStorageConfig(local_dir=tmp_path), ("front",), clock=lambda: now)

    manager.record_application_started(metadata={"app_env": "test"})
    manager.record_ai_started("vehicle_detection", ("front",))
    manager.record_detection_batch("front", (_event("car", now),), task_id="vehicle_detection")
    manager.record_plate("12가3456", confidence=0.94)

    records = _records(tmp_path, "2026-08-21")
    assert [record["event_type"] for record in records] == [
        "application_started",
        "ai_started",
        "detection_batch",
        "vehicle_entered",
        "plate_recognized",
    ]
    assert records[-1]["payload"]["plate_number"] == "12가3456"
    assert "source_image_path" not in records[-1]["payload"]
    assert records[2]["payload"]["detections"][0]["camera_id"] == "front"
    assert records[3]["vehicle_session_id"]


def test_person_window_samples_every_half_second_until_five_seconds_after_clear(tmp_path: Path):
    start = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(
            local_dir=tmp_path,
            sample_interval_seconds=0.5,
            person_stale_seconds=1.0,
            person_clear_grace_seconds=5.0,
        ),
        ("front", "rear_side"),
        clock=lambda: start,
    )
    manager.record_detection_batch("front", (_event("person", start),), task_id="person_presence", at=start)

    assert manager.tick(now=start + timedelta(seconds=6)) == 13
    records = _records(tmp_path, "2026-08-21")
    samples = [record["payload"] for record in records if record["event_type"] == "person_sample"]
    assert len(samples) == 13
    assert samples[0]["cameras"]["front"]["person_present"] is True
    assert samples[2]["cameras"]["front"]["person_present"] is True
    assert samples[3]["cameras"]["front"]["person_present"] is False
    assert samples[-1]["cameras"]["rear_side"]["person_present"] is False
    assert samples[-1]["sampled_at"] == (start + timedelta(seconds=6)).isoformat()
    assert records[-1]["event_type"] == "person_window_closed"


def test_plate_source_path_is_transient_to_evidence_sink(tmp_path: Path):
    now = datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc)
    manager = RawDataManager(RawStorageConfig(local_dir=tmp_path), ("front",), clock=lambda: now)
    sink_records: list[dict] = []
    manager.set_event_sink(lambda record: sink_records.append(dict(record)))
    manager.record_plate("12가3456", source_image_path="/runtime/private/source.png")

    stored = _records(tmp_path, "2026-08-21")[-1]
    assert "source_image_path" not in stored["payload"]
    assert sink_records[-1]["payload"]["source_image_path"] == "/runtime/private/source.png"
    manager.close()


def test_person_window_records_close_when_clear_deadline_falls_between_sample_ticks(tmp_path: Path):
    start = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(
            local_dir=tmp_path,
            sample_interval_seconds=0.5,
            person_stale_seconds=1.0,
            person_clear_grace_seconds=5.0,
        ),
        ("front",),
        clock=lambda: start,
    )
    manager.record_detection_batch("front", (_event("person", start),), task_id="person_presence", at=start)
    manager.tick(now=start)
    shifted = start + timedelta(seconds=0.108)
    manager.record_detection_batch("front", (_event("person", shifted),), task_id="person_presence", at=shifted)
    for half_second in range(1, 14):
        manager.tick(now=start + timedelta(seconds=half_second * 0.5))

    records = _records(tmp_path, "2026-08-21")
    assert sum(record["event_type"] == "person_window_closed" for record in records) == 1
    assert manager.person_sampler.active is False


class FakeUploader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def upload_day(self, day: str, day_dir: Path, manifest: dict) -> str:
        self.calls.append((day, manifest))
        if self.fail:
            raise OSError("offline")
        return f"/home/site/raw/{day}"


def _write_day(root: Path, day: str) -> None:
    day_dir = root / day
    day_dir.mkdir(parents=True)
    (day_dir / "events.jsonl").write_text('{"schema_version":1}\n', encoding="utf-8")


def test_sync_uploads_completed_days_and_deletes_only_uploaded_data_after_14_days(tmp_path: Path):
    _write_day(tmp_path, "2026-08-21")
    _write_day(tmp_path, "2026-08-20")
    _write_day(tmp_path, "2026-08-07")
    uploader = FakeUploader()
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, retention_days=14, timezone_name="UTC"),
        ("front",),
        uploader=uploader,
        clock=lambda: now,
    )

    result = manager.sync_completed_days(now=now)

    assert result.uploaded_days == ("2026-08-07", "2026-08-20")
    assert result.deleted_days == ("2026-08-07",)
    assert (tmp_path / "2026-08-21/events.jsonl").is_file()
    assert not (tmp_path / "2026-08-07").exists()
    assert (tmp_path / "2026-08-20/.nas-upload.json").is_file()
    assert (tmp_path / "2026-08-20/manifest.json").is_file()


def test_explicit_sync_can_upload_current_day_without_deleting_it(tmp_path: Path):
    _write_day(tmp_path, "2026-08-21")
    uploader = FakeUploader()
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, retention_days=14, timezone_name="UTC"),
        ("front",),
        uploader=uploader,
        clock=lambda: now,
    )

    result = manager.sync_completed_days(now=now, include_current_day=True)

    assert result.uploaded_days == ("2026-08-21",)
    assert result.deleted_days == ()
    assert (tmp_path / "2026-08-21/events.jsonl").is_file()
    assert (tmp_path / "2026-08-21/.nas-upload.json").is_file()


def test_failed_upload_keeps_expired_local_data_for_retry(tmp_path: Path):
    _write_day(tmp_path, "2026-08-01")
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, retention_days=14, timezone_name="UTC"),
        ("front",),
        uploader=FakeUploader(fail=True),
        clock=lambda: now,
    )

    result = manager.sync_completed_days(now=now)

    assert result.errors == ("2026-08-01:OSError",)
    assert not result.deleted_days
    assert (tmp_path / "2026-08-01/events.jsonl").is_file()
