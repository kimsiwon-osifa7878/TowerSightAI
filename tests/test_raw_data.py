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


def test_person_samples_attach_ld2410_snapshot_at_each_half_second(tmp_path: Path):
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    sampled_times: list[datetime] = []

    def snapshot_provider(sampled_at: datetime) -> dict:
        sampled_times.append(sampled_at)
        return {
            "status": "fresh",
            "source": "ld2410_tcp",
            "received_at": sampled_at.isoformat(),
            "age_ms": 0,
            "target_status": 2,
            "raw_hex": "F4 F3 F2 F1",
        }

    manager = RawDataManager(
        RawStorageConfig(
            local_dir=tmp_path,
            sample_interval_seconds=0.5,
            person_stale_seconds=1.0,
            person_clear_grace_seconds=5.0,
            ld2410_sample_interval_seconds=0,  # isolate the person-sample path from the 1 Hz radar sample
        ),
        ("front",),
        clock=lambda: start,
        ld2410_snapshot_provider=snapshot_provider,
    )
    manager.record_detection_batch("front", (_event("person", start),), task_id="person_presence", at=start)

    assert manager.tick(now=start + timedelta(seconds=1)) == 3

    samples = [
        record["payload"]
        for record in _records(tmp_path, "2026-08-21")
        if record["event_type"] == "person_sample"
    ]
    assert sampled_times == [start, start + timedelta(seconds=0.5), start + timedelta(seconds=1)]
    assert [sample["ld2410"]["status"] for sample in samples] == ["fresh", "fresh", "fresh"]
    assert samples[-1]["ld2410"]["target_status"] == 2
    assert samples[-1]["ld2410"]["raw_hex"] == "F4 F3 F2 F1"


def test_ld2410_provider_outside_person_window_is_only_used_for_the_1hz_sample(tmp_path: Path):
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    calls: list[datetime] = []
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, ld2410_sample_interval_seconds=0),
        ("front",),
        clock=lambda: start,
        ld2410_snapshot_provider=lambda sampled_at: calls.append(sampled_at) or {},
    )
    assert manager.tick(now=start + timedelta(seconds=10)) == 0
    assert calls == []  # interval 0: no radar sampling, so no provider call without a person window

    calls.clear()
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, ld2410_sample_interval_seconds=1.0),
        ("front",),
        clock=lambda: start,
        ld2410_snapshot_provider=lambda sampled_at: calls.append(sampled_at) or {"status": "unavailable"},
    )
    assert manager.tick(now=start + timedelta(seconds=3)) == 0  # person samples: none
    assert calls == [start + timedelta(seconds=i) for i in range(4)]  # radar samples: t=0..3
    assert [r["event_type"] for r in _records(tmp_path, "2026-08-21")] == []  # unavailable → not recorded


def test_ld2410_provider_failure_is_explicit_and_status_is_raw_only(tmp_path: Path):
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)

    def failing_provider(_sampled_at: datetime) -> dict:
        raise RuntimeError("sensor unavailable")

    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path),
        ("front",),
        clock=lambda: start,
        ld2410_snapshot_provider=failing_provider,
    )
    manager.record_ld2410_status("listening", {"port": 9000})
    manager.record_detection_batch("front", (_event("person", start),), task_id="person_presence", at=start)
    assert manager.tick(now=start) == 1

    records = _records(tmp_path, "2026-08-21")
    server_status = next(record for record in records if record["event_type"] == "ld2410_server_status")
    sample = next(record for record in records if record["event_type"] == "person_sample")
    assert server_status["payload"]["safety_effect"] == "raw_only"
    assert sample["payload"]["ld2410"] == {
        "status": "unavailable",
        "source": "ld2410_tcp",
        "received_at": None,
        "age_ms": None,
        "reason": "provider_error",
    }


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


def test_request_current_day_sync_is_debounced(tmp_path: Path):
    import time as _time

    _write_day(tmp_path, "2026-08-21")
    uploader = FakeUploader()
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    manager = RawDataManager(
        RawStorageConfig(local_dir=tmp_path, retention_days=14, timezone_name="UTC"),
        ("front",),
        uploader=uploader,
        clock=lambda: now,
    )

    assert manager.request_current_day_sync() is True
    for _ in range(100):
        with manager._sync_lock:
            if not manager._sync_running:
                break
        _time.sleep(0.05)
    assert (tmp_path / "2026-08-21/.nas-upload.json").is_file()
    # second call inside the debounce window is refused
    assert manager.request_current_day_sync() is False
    assert manager.request_current_day_sync(min_interval_seconds=0.0) is True


# ---- ld2410_sample (1 Hz radar sample, analysis only) ------------------------------------------


def _radar(status: str = "fresh", *, target: int = 2, received_at: datetime | None = None, **extra) -> dict:
    payload = {
        "status": status,
        "source": "ld2410_tcp",
        "received_at": received_at.isoformat() if received_at else None,
        "age_ms": 100 if status != "unavailable" else None,
        "client_ip": "192.168.0.50",
        "target_status": target,
        "target_status_text": {0: "None", 1: "Moving", 2: "Motionless", 3: "Both"}[target],
        "moving_energy": 10 if target in (1, 3) else 0,
        "motionless_energy": 63 if target in (2, 3) else 0,
        "detection_distance_cm": 226,
        "raw_hex": "F4 F3 F2 F1",
    }
    payload.update(extra)
    return payload


def _manager(tmp_path: Path, start: datetime, provider, **config) -> RawDataManager:
    return RawDataManager(
        RawStorageConfig(local_dir=tmp_path, **config),
        ("front",),
        clock=lambda: start,
        ld2410_snapshot_provider=provider,
    )


def _events(tmp_path: Path, day: str, event_type: str) -> list[dict]:
    return [r for r in _records(tmp_path, day) if r["event_type"] == event_type]


def test_ld2410_sample_is_recorded_every_second_outside_a_person_window(tmp_path: Path):
    start = datetime(2026, 9, 10, 6, 40, 40, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at - timedelta(milliseconds=126)))

    assert manager.tick(now=start + timedelta(seconds=4)) == 0  # return value = person samples only

    samples = _events(tmp_path, "2026-09-10", "ld2410_sample")
    assert [s["payload"]["sampled_at"] for s in samples] == [
        (start + timedelta(seconds=i)).isoformat() for i in range(5)
    ]
    assert [s["recorded_at"] for s in samples] == [s["payload"]["sampled_at"] for s in samples]
    payload = samples[0]["payload"]
    assert payload["status"] == "fresh" and payload["source"] == "ld2410_tcp"
    assert payload["target_status"] == 2 and payload["target_status_text"] == "Motionless"
    assert payload["safety_effect"] == "raw_only"
    assert "raw_hex" not in payload  # size: the hex dump only lives in person_sample.ld2410
    assert not manager.person_sampler.active


def test_ld2410_sample_dedupes_stale_frames_and_skips_unavailable(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    frame_at = start - timedelta(seconds=2)

    def provider(at: datetime) -> dict:
        offset = int((at - start).total_seconds())
        if offset < 3:
            return _radar("stale", received_at=frame_at)  # same frame three times
        if offset < 5:
            return _radar("unavailable", target=0)
        return _radar("stale", received_at=start + timedelta(seconds=4))  # a new (but stale) frame

    manager = _manager(tmp_path, start, provider)
    manager.tick(now=start + timedelta(seconds=7))

    samples = _events(tmp_path, "2026-09-10", "ld2410_sample")
    assert [s["payload"]["status"] for s in samples] == ["stale", "stale"]
    assert [s["payload"]["sampled_at"] for s in samples] == [
        start.isoformat(),
        (start + timedelta(seconds=5)).isoformat(),
    ]


def test_ld2410_sample_interval_zero_disables_sampling_and_windows(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    calls: list[datetime] = []
    manager = _manager(
        tmp_path, start, lambda at: calls.append(at) or _radar(), ld2410_sample_interval_seconds=0
    )
    manager.tick(now=start + timedelta(seconds=30))
    assert calls == []
    assert _events(tmp_path, "2026-09-10", "ld2410_sample") == []
    assert _events(tmp_path, "2026-09-10", "radar_window_started") == []
    assert manager.close_radar_window(reason="service_stopped") is False


def test_ld2410_sample_provider_error_is_recorded_once_then_recovers(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    state = {"fail": True}

    def provider(at: datetime) -> dict:
        if state["fail"]:
            raise RuntimeError("sensor unavailable")
        return _radar(received_at=at)

    manager = _manager(tmp_path, start, provider)
    manager.tick(now=start + timedelta(seconds=2))  # t=0,1,2 all fail
    state["fail"] = False
    manager.tick(now=start + timedelta(seconds=4))  # t=3,4 recover

    samples = _events(tmp_path, "2026-09-10", "ld2410_sample")
    assert [s["payload"]["status"] for s in samples] == ["unavailable", "fresh", "fresh"]
    assert samples[0]["payload"]["reason"] == "provider_error"
    assert samples[0]["payload"]["sampled_at"] == start.isoformat()


def test_person_sample_ld2410_still_carries_raw_hex(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at), sample_interval_seconds=0.5)
    manager.record_detection_batch("front", (_event("person", start),), task_id="person_presence", at=start)
    manager.tick(now=start + timedelta(seconds=1))
    person = _events(tmp_path, "2026-09-10", "person_sample")
    assert person and all(s["payload"]["ld2410"]["raw_hex"] == "F4 F3 F2 F1" for s in person)
    radar = _events(tmp_path, "2026-09-10", "ld2410_sample")
    assert radar and all("raw_hex" not in s["payload"] for s in radar)


def test_ld2410_sample_catch_up_is_bounded_after_a_long_stall(tmp_path: Path):
    from towersightai.storage.raw_data import LD2410_SAMPLE_CATCHUP_SECONDS

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    calls: list[datetime] = []
    manager = _manager(tmp_path, start, lambda at: calls.append(at) or _radar("unavailable", target=0))
    manager.tick(now=start + timedelta(hours=1))
    assert len(calls) <= LD2410_SAMPLE_CATCHUP_SECONDS + 2
    assert calls[-1] == start + timedelta(hours=1)


# ---- radar windows ----------------------------------------------------------------------------


def _drive(tracker, start: datetime, sequence: str):
    """Feed one sample per second: p=present, a=absent, s=stale, u=unavailable."""
    events = []
    for index, code in enumerate(sequence):
        at = start + timedelta(seconds=index)
        snapshot = {
            "p": _radar(received_at=at, target=2 if index % 2 else 1, detection_distance_cm=200 + index),
            "a": _radar(received_at=at, target=0),
            "s": _radar("stale", received_at=start - timedelta(seconds=5)),
            "u": _radar("unavailable", target=0),
        }[code]
        events.extend(tracker.observe(at, snapshot))
    return events


def test_radar_window_opens_after_three_present_seconds_backdated_to_the_first(tmp_path: Path):
    from towersightai.storage.raw_data import RadarWindowTracker

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    assert _drive(tracker, start, "ppp") == []  # 0,1,2 s → only 2 s of presence
    assert tracker.open is False

    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    events = _drive(tracker, start, "pppp")
    assert [e[0] for e in events] == ["radar_window_started"]
    event_type, recorded_at, payload = events[0]
    assert recorded_at == start
    assert payload["started_at"] == start.isoformat()
    assert payload["confirm_seconds"] == 3.0
    assert payload["first_target_status"] == 1
    assert payload["first_detection_distance_cm"] == 200
    assert payload["safety_effect"] == "raw_only"
    assert tracker.open is True


def test_radar_window_closes_after_five_absent_seconds_with_aggregates(tmp_path: Path):
    from towersightai.storage.raw_data import RadarWindowTracker

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    events = _drive(tracker, start, "pppppp" + "aaaa")  # present 0..5, absent 6..9 → still open
    assert [e[0] for e in events] == ["radar_window_started"]
    events = tracker.observe(start + timedelta(seconds=10), _radar(target=0, received_at=start))
    assert [e[0] for e in events] == ["radar_window_closed"]
    _event_type, recorded_at, payload = events[0]
    assert recorded_at == start + timedelta(seconds=5)  # last present sample
    assert payload["reason"] == "cleared"
    assert payload["ended_at"] == recorded_at.isoformat()
    assert payload["duration_seconds"] == 5.0
    assert payload["present_sample_count"] == 6
    assert payload["sample_count"] == 11
    assert payload["target_status_counts"] == {"1": 3, "2": 3}
    assert payload["max_moving_energy"] == 10 and payload["max_motionless_energy"] == 63
    assert payload["min_detection_distance_cm"] == 200 and payload["max_detection_distance_cm"] == 205
    assert tracker.open is False


def test_radar_unknown_samples_break_the_streak_but_are_not_absent(tmp_path: Path):
    from towersightai.storage.raw_data import RadarWindowTracker

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    assert _drive(tracker, start, "ppspp") == []  # stale at t=2 resets confirmation
    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    events = _drive(tracker, start, "pppp" + "ssuus")  # present 0..3, unknown 4..8 (5 s)
    assert [e[0] for e in events] == ["radar_window_started", "radar_window_closed"]
    assert events[1][2]["reason"] == "radar_unavailable"
    assert events[1][1] == start + timedelta(seconds=3)


def test_radar_redetection_after_close_starts_a_new_window(tmp_path: Path):
    from towersightai.storage.raw_data import RadarWindowTracker

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    tracker = RadarWindowTracker(confirm_seconds=3.0, clear_seconds=5.0)
    events = _drive(tracker, start, "pppp" + "aaaaa" + "pppp")
    assert [e[0] for e in events] == ["radar_window_started", "radar_window_closed", "radar_window_started"]
    assert events[0][2]["radar_window_id"] != events[2][2]["radar_window_id"]
    assert events[2][1] == start + timedelta(seconds=9)


def test_radar_window_force_close_reasons_and_durability(tmp_path: Path):
    from towersightai.storage import raw_data as raw_module

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at))
    durable: list[str] = []
    original_append = manager.writer.append

    def spy_append(record, *, recorded_at, durable: bool = False):
        if durable:
            durable.append(record["event_type"]) if False else None
        return original_append(record, recorded_at=recorded_at, durable=durable)

    durable_types: list[str] = []

    def spy(record, *, recorded_at, durable=False):
        if durable:
            durable_types.append(record["event_type"])
        return original_append(record, recorded_at=recorded_at, durable=durable)

    manager.writer.append = spy  # type: ignore[method-assign]
    manager.tick(now=start + timedelta(seconds=4))
    assert manager.radar_tracker.open is True

    manager.record_ld2410_status("stopped", {})
    closed = _events(tmp_path, "2026-09-10", "radar_window_closed")
    assert [c["payload"]["reason"] for c in closed] == ["service_stopped"]
    assert closed[0]["recorded_at"] == (start + timedelta(seconds=4)).isoformat()
    assert manager.close_radar_window(reason="service_stopped") is False  # nothing open now

    manager.tick(now=start + timedelta(seconds=9))  # re-detected 5..9 → new window
    assert manager.radar_tracker.open is True
    manager.close()
    closed = _events(tmp_path, "2026-09-10", "radar_window_closed")
    assert [c["payload"]["reason"] for c in closed] == ["service_stopped", "application_stopped"]
    assert manager.close_radar_window(reason="application_stopped") is False  # closed manager: no writes
    assert "radar_window_started" in durable_types and "radar_window_closed" in durable_types
    assert "ld2410_sample" not in durable_types
    assert raw_module._DURABLE_EVENTS >= {"radar_window_started", "radar_window_closed"}


def test_plate_outcome_records_recognition_flag_reads_and_attempts(tmp_path: Path):
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    manager = RawDataManager(RawStorageConfig(local_dir=tmp_path), ("front",), clock=lambda: start)
    manager.record_plate("12가3456", confidence=0.9, reads=3, reason="vote")
    manager.record_plate("미인식", confidence=None, recognized=False, reads=0, reason="aborted:uncertainty")
    manager.record_plate_attempt("12가3456", confidence=0.88, plate_bbox={"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0})
    manager.record_plate_attempt("", accepted=False, reason="no_plate_detected")
    records = {record["event_type"]: [] for record in _records(tmp_path, "2026-08-21")}
    for record in _records(tmp_path, "2026-08-21"):
        records[record["event_type"]].append(record["payload"])
    plates = records["plate_recognized"]
    assert plates[0]["recognized"] is True and plates[0]["reads"] == 3 and plates[0]["reason"] == "vote"
    assert plates[1]["recognized"] is False and plates[1]["plate_number"] == "미인식"
    attempts = records["plate_attempt"]
    assert attempts[0]["accepted"] is True and attempts[0]["plate_bbox"] == {"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0}
    assert attempts[1]["accepted"] is False and attempts[1]["reason"] == "no_plate_detected"
    assert all(payload["safety_effect"] == "raw_only" for payload in attempts)


def test_radar_window_records_camera_state_alongside_the_radar_values(tmp_path: Path):
    """The comparison case: radar claims a person, cameras do not. Without this row the two
    sources cannot be compared at that instant."""
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at))
    manager.tick(now=start + timedelta(seconds=6))

    assert [e["payload"]["radar_window_id"] for e in _events(tmp_path, "2026-09-10", "radar_window_started")]
    window_id = _events(tmp_path, "2026-09-10", "radar_window_started")[0]["payload"]["radar_window_id"]
    samples = _events(tmp_path, "2026-09-10", "radar_sample")
    # The window is confirmed on the 4th present second, so sampling starts there.
    assert [s["payload"]["sampled_at"] for s in samples] == [
        (start + timedelta(seconds=offset)).isoformat() for offset in (3, 4, 5, 6)
    ]
    payload = samples[0]["payload"]
    assert payload["radar_window_id"] == window_id
    assert payload["camera_person_present"] is False
    assert payload["cameras"]["front"] == {
        "person_present": False,
        "last_person_detected_at": None,
        "detections": [],
    }
    assert payload["ld2410"]["target_status"] == 2 and "raw_hex" not in payload["ld2410"]
    assert payload["safety_effect"] == "raw_only"


def test_radar_sample_keeps_recording_when_a_camera_person_window_is_also_open(tmp_path: Path):
    """The agreeing seconds are the whole point: skipping them while a person window is open would
    make every radar window look like permanent disagreement (field data 2026-09-16 showed 0 %
    agreement across 16 windows that cameras had in fact seen)."""
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at))
    manager.tick(now=start + timedelta(seconds=4))  # radar window opens, cameras quiet
    seen_at = start + timedelta(seconds=4, milliseconds=500)
    manager.record_detection_batch("front", (_event("person", seen_at),), task_id="process_monitoring", at=seen_at)
    manager.tick(now=start + timedelta(seconds=5))

    samples = _events(tmp_path, "2026-09-10", "radar_sample")
    assert [s["payload"]["sampled_at"] for s in samples] == [
        (start + timedelta(seconds=offset)).isoformat() for offset in (3, 4, 5)
    ]
    assert [s["payload"]["camera_person_present"] for s in samples] == [False, False, True]
    assert samples[-1]["payload"]["cameras"]["front"]["person_present"] is True
    # person_sample still carries the radar side, so both tables agree on that second.
    person_samples = _events(tmp_path, "2026-09-10", "person_sample")
    assert person_samples and person_samples[0]["payload"]["cameras"]["front"]["person_present"] is True
    assert person_samples[0]["payload"]["ld2410"]["target_status"] == 2


def test_radar_camera_sampling_is_bounded_and_can_be_disabled(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at), radar_sample_seconds=5)
    manager.tick(now=start + timedelta(seconds=9))
    # Window starts (backdated) at t=0, so only samples within 5 s of it are kept.
    assert [s["payload"]["sampled_at"] for s in _events(tmp_path, "2026-09-10", "radar_sample")] == [
        (start + timedelta(seconds=offset)).isoformat() for offset in (3, 4, 5)
    ]

    off = tmp_path / "off"
    manager = _manager(off, start, lambda at: _radar(received_at=at), radar_sample_seconds=0)
    manager.tick(now=start + timedelta(seconds=6))
    assert _events(off, "2026-09-10", "radar_sample") == []
    assert _events(off, "2026-09-10", "radar_window_started")  # the window itself still opens


def test_camera_state_survives_a_closed_person_window(tmp_path: Path):
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    manager = _manager(tmp_path, start, lambda at: _radar(received_at=at))
    manager.record_detection_batch("front", (_event("person", start),), task_id="process_monitoring", at=start)
    sampler = manager.person_sampler
    assert sampler.camera_state(start)["front"]["person_present"] is True
    assert sampler.camera_state(start + timedelta(seconds=2))["front"]["person_present"] is False
    manager.tick(now=start + timedelta(seconds=10))  # window opens and closes
    assert sampler.active is False
    state = sampler.camera_state(start + timedelta(seconds=10))
    assert state["front"]["person_present"] is False
    assert state["front"]["last_person_detected_at"] == start.isoformat()
