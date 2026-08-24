from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from towersightai.config.settings import RawStorageConfig
from towersightai.inference.events import DetectionEvent
from towersightai.storage.archive import (
    ParamikoManifestUploader,
    build_day_manifest,
    manifest_sha256,
    write_manifest_atomic,
)
from towersightai.storage.hourly_writer import HourlyJsonlWriter, WriterBusyError

SCHEMA_VERSION = 2
DEFAULT_TIMEZONE = "Asia/Seoul"
PERSON_LABELS = frozenset({"person", "human"})
VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle", "vehicle"})
_DURABLE_EVENTS = frozenset(
    {
        "application_started",
        "application_stopped",
        "vehicle_entered",
        "vehicle_session_ended",
        "plate_recognized",
        "person_window_started",
        "person_window_closed",
        "media_artifact_created",
        "media_capture_failed",
    }
)


@dataclass(frozen=True)
class SyncResult:
    uploaded_days: tuple[str, ...] = ()
    retained_days: tuple[str, ...] = ()
    deleted_days: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class DayUploader(Protocol):
    def upload_day(self, day: str, day_dir: Path, manifest: Mapping[str, Any]) -> str: ...


class PersonWindowSampler:
    """Sample all camera states while a person may be present and through the clear tail."""

    def __init__(
        self,
        camera_ids: Iterable[str],
        *,
        sample_interval_seconds: float = 0.5,
        stale_seconds: float = 1.0,
        clear_grace_seconds: float = 5.0,
    ) -> None:
        self.camera_ids = tuple(dict.fromkeys(camera_ids))
        self.sample_interval_seconds = sample_interval_seconds
        self.stale_seconds = stale_seconds
        self.clear_grace_seconds = clear_grace_seconds
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self.last_person_at: datetime | None = None
        self.next_sample_at: datetime | None = None
        self._latest: dict[str, tuple[DetectionEvent, ...]] = {}
        self._latest_at: dict[str, datetime] = {}

    @property
    def active(self) -> bool:
        return self.session_id is not None

    def observe(self, camera_id: str, detections: Iterable[DetectionEvent], *, observed_at: datetime) -> bool:
        person_events = tuple(event for event in detections if event.label.strip().lower() in PERSON_LABELS)
        if not person_events:
            return False
        self._latest[camera_id] = person_events
        self._latest_at[camera_id] = observed_at
        self.last_person_at = observed_at
        started = not self.active
        if started:
            self.session_id = uuid.uuid4().hex
            self.started_at = observed_at
            self.next_sample_at = observed_at
        return started

    def due_samples(self, now: datetime) -> tuple[dict[str, Any], ...]:
        if not self.active or self.next_sample_at is None or self.last_person_at is None:
            return ()
        clear_at = self.last_person_at + timedelta(seconds=self.stale_seconds)
        stop_at = clear_at + timedelta(seconds=self.clear_grace_seconds)
        samples: list[dict[str, Any]] = []
        while self.next_sample_at <= now and self.next_sample_at <= stop_at:
            sample_at = self.next_sample_at
            cameras: dict[str, Any] = {}
            for camera_id in self.camera_ids:
                latest_at = self._latest_at.get(camera_id)
                present = bool(
                    latest_at
                    and sample_at >= latest_at
                    and (sample_at - latest_at).total_seconds() <= self.stale_seconds
                )
                events = self._latest.get(camera_id, ()) if present else ()
                cameras[camera_id] = {
                    "person_present": present,
                    "last_person_detected_at": latest_at.isoformat() if latest_at else None,
                    "detections": [event.to_dict() for event in events],
                }
            samples.append(
                {
                    "person_window_id": self.session_id,
                    "sampled_at": sample_at.isoformat(),
                    "person_present": any(item["person_present"] for item in cameras.values()),
                    "cameras": cameras,
                }
            )
            self.next_sample_at += timedelta(seconds=self.sample_interval_seconds)
        if now >= stop_at and self.next_sample_at > stop_at:
            self.session_id = None
            self.started_at = None
            self.last_person_at = None
            self.next_sample_at = None
            self._latest.clear()
            self._latest_at.clear()
        return tuple(samples)


class RawDataManager:
    def __init__(
        self,
        config: RawStorageConfig,
        camera_ids: Iterable[str],
        *,
        uploader: DayUploader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.camera_ids = tuple(dict.fromkeys(camera_ids))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.writer = HourlyJsonlWriter(
            config.local_dir,
            config.timezone_name,
            shard_minutes=config.shard_minutes,
        )
        self.person_sampler = PersonWindowSampler(
            self.camera_ids,
            sample_interval_seconds=config.sample_interval_seconds,
            stale_seconds=config.person_stale_seconds,
            clear_grace_seconds=config.person_clear_grace_seconds,
        )
        self.uploader = uploader or ParamikoManifestUploader(config)
        self.application_session_id = uuid.uuid4().hex
        self.vehicle_session_id: str | None = None
        self._active_ai_tasks: set[str] = set()
        self._sync_lock = threading.Lock()
        self._sync_running = False
        self._event_sink: Callable[[Mapping[str, Any]], None] | None = None
        self._closed = False

    def set_event_sink(self, sink: Callable[[Mapping[str, Any]], None] | None) -> None:
        self._event_sink = sink

    def record_application_started(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        self.record("application_started", payload=dict(metadata or {}))

    def record_application_stopped(self) -> None:
        self.record("application_stopped")

    def record(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        sink_payload: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> Path:
        if self._closed:
            raise RuntimeError("raw-data manager is closed")
        recorded_at = at or self.clock()
        record = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "recorded_at": recorded_at.isoformat(),
            "application_session_id": self.application_session_id,
            "vehicle_session_id": self.vehicle_session_id,
            "payload": dict(payload or {}),
        }
        path = self.writer.append(record, recorded_at=recorded_at, durable=event_type in _DURABLE_EVENTS)
        if self._event_sink is not None and not event_type.startswith("media_"):
            try:
                sink_record = record
                if sink_payload:
                    sink_record = {**record, "payload": {**record["payload"], **dict(sink_payload)}}
                self._event_sink(sink_record)
            except Exception:  # noqa: BLE001 - evidence failure must not suppress the raw event.
                logging.getLogger(__name__).exception("raw evidence sink failed event_type=%s", event_type)
        return path

    def record_media_artifact(
        self,
        *,
        related_event_id: str,
        kind: str,
        camera_id: str,
        relative_path: str,
        size_bytes: int,
        sha256: str,
        captured_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.record(
            "media_artifact_created",
            payload={
                "related_event_id": related_event_id,
                "kind": kind,
                "camera_id": camera_id,
                "relative_path": relative_path,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "captured_at": captured_at.isoformat(),
                "metadata": dict(metadata or {}),
            },
            at=captured_at,
        )

    def record_media_failure(
        self,
        *,
        related_event_id: str,
        kind: str,
        camera_id: str,
        reason: str,
        at: datetime | None = None,
    ) -> None:
        self.record(
            "media_capture_failed",
            payload={
                "related_event_id": related_event_id,
                "kind": kind,
                "camera_id": camera_id,
                "reason": reason[:240],
            },
            at=at,
        )

    def record_ai_started(self, task_id: str, camera_ids: Iterable[str], *, simulated: bool = False) -> None:
        if task_id in self._active_ai_tasks:
            return
        self._active_ai_tasks.add(task_id)
        self.record("ai_started", payload={"task_id": task_id, "camera_ids": list(camera_ids), "simulated": simulated})

    def record_ai_stopped(self, task_id: str, *, reason: str = "requested") -> None:
        if task_id not in self._active_ai_tasks:
            return
        self._active_ai_tasks.remove(task_id)
        self.record("ai_stopped", payload={"task_id": task_id, "reason": reason})

    def record_vehicle_entry(
        self,
        *,
        camera_id: str,
        confidence: float | None = None,
        simulated: bool = False,
        at: datetime | None = None,
    ) -> str:
        if self.vehicle_session_id is None:
            self.vehicle_session_id = uuid.uuid4().hex
            self.record(
                "vehicle_entered",
                payload={"camera_id": camera_id, "confidence": confidence, "simulated": simulated},
                at=at,
            )
        return self.vehicle_session_id

    def record_plate(
        self,
        plate_number: str,
        *,
        confidence: float | None = None,
        camera_id: str = "front",
        simulated: bool = False,
        source_image_path: str | None = None,
        plate_bbox: Mapping[str, int] | None = None,
        at: datetime | None = None,
    ) -> None:
        self.record(
            "plate_recognized",
            payload={
                "plate_number": plate_number,
                "confidence": confidence,
                "camera_id": camera_id,
                "simulated": simulated,
                "plate_bbox": dict(plate_bbox) if plate_bbox else None,
            },
            sink_payload={"source_image_path": source_image_path} if source_image_path else None,
            at=at,
        )

    def record_detection_batch(
        self,
        camera_id: str,
        detections: Iterable[DetectionEvent],
        *,
        task_id: str,
        at: datetime | None = None,
    ) -> None:
        recorded_at = at or self.clock()
        events = tuple(detections)
        if not events:
            return
        self.record(
            "detection_batch",
            payload={"task_id": task_id, "camera_id": camera_id, "detections": [event.to_dict() for event in events]},
            at=recorded_at,
        )
        vehicles = [event for event in events if event.label.strip().lower() in VEHICLE_LABELS]
        if vehicles and task_id == "vehicle_detection":
            self.record_vehicle_entry(
                camera_id=camera_id,
                confidence=max(event.confidence for event in vehicles),
                at=recorded_at,
            )
        if self.person_sampler.observe(camera_id, events, observed_at=recorded_at):
            self.record(
                "person_window_started",
                payload={"person_window_id": self.person_sampler.session_id, "camera_id": camera_id},
                at=recorded_at,
            )

    def tick(self, *, now: datetime | None = None) -> int:
        sampled_at = now or self.clock()
        closing_window_id = self.person_sampler.session_id
        was_active = self.person_sampler.active
        samples = self.person_sampler.due_samples(sampled_at)
        for sample in samples:
            self.record("person_sample", payload=sample, at=datetime.fromisoformat(sample["sampled_at"]))
        if was_active and not self.person_sampler.active:
            self.record("person_window_closed", payload={"person_window_id": closing_window_id}, at=sampled_at)
        return len(samples)

    def end_vehicle_session(self, *, reason: str) -> None:
        if self.vehicle_session_id is None:
            return
        self.record("vehicle_session_ended", payload={"reason": reason})
        self.vehicle_session_id = None

    def sync_completed_days(
        self,
        *,
        now: datetime | None = None,
        include_current_day: bool = False,
    ) -> SyncResult:
        current = now or self.clock()
        today = current.astimezone(ZoneInfo(self.config.timezone_name)).date()
        uploaded: list[str] = []
        retained: list[str] = []
        deleted: list[str] = []
        errors: list[str] = []
        self.config.local_dir.mkdir(parents=True, exist_ok=True)
        for day_dir in sorted(path for path in self.config.local_dir.iterdir() if path.is_dir()):
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day > today or (day == today and not include_current_day):
                continue
            try:
                self.writer.finalize_day(day, require_exclusive=day == today)
                manifest = build_day_manifest(day_dir, day.isoformat())
                if not manifest["files"]:
                    continue
                write_manifest_atomic(day_dir, manifest)
                digest = manifest_sha256(manifest)
                marker_path = day_dir / ".nas-upload.json"
                marker = _read_json(marker_path)
                uploaded_ok = marker.get("manifest_sha256") == digest
                if not uploaded_ok:
                    remote_dir = self.uploader.upload_day(day.isoformat(), day_dir, manifest)
                    _write_json_atomic(
                        marker_path,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "manifest_sha256": digest,
                            "remote_dir": remote_dir,
                            "uploaded_at": self.clock().isoformat(),
                        },
                    )
                    uploaded.append(day.isoformat())
                    uploaded_ok = True
                age_days = (today - day).days
                if uploaded_ok and age_days >= self.config.retention_days:
                    shutil.rmtree(day_dir)
                    deleted.append(day.isoformat())
                else:
                    retained.append(day.isoformat())
            except Exception as exc:  # noqa: BLE001 - one day must not block later retries.
                logging.getLogger(__name__).exception("raw-data upload failed day=%s", day)
                errors.append(f"{day.isoformat()}:{type(exc).__name__}")
                retained.append(day.isoformat())
        return SyncResult(tuple(uploaded), tuple(retained), tuple(deleted), tuple(errors))

    def start_background_sync(self) -> bool:
        with self._sync_lock:
            if self._sync_running:
                return False
            self._sync_running = True

        def run() -> None:
            try:
                result = self.sync_completed_days()
                logging.getLogger(__name__).info(
                    "raw-data sync complete uploaded=%s retained=%s deleted=%s errors=%s",
                    result.uploaded_days,
                    result.retained_days,
                    result.deleted_days,
                    result.errors,
                )
            finally:
                with self._sync_lock:
                    self._sync_running = False

        threading.Thread(target=run, name="raw-data-nas-sync", daemon=True).start()
        return True

    def close(self) -> None:
        if self._closed:
            return
        self.writer.close()
        self._closed = True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as fp:
        json.dump(dict(payload), fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    temporary.replace(path)


__all__ = [
    "DEFAULT_TIMEZONE",
    "PersonWindowSampler",
    "RawDataManager",
    "SyncResult",
    "WriterBusyError",
]
