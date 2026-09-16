from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
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
        "ld2410_server_status",
        "vehicle_exit_started",
        "vehicle_exit_ended",
        "radar_window_started",
        "radar_window_closed",
    }
)
# ld2410_sample is deliberately NOT durable: 1 Hz fsync is too expensive for a telemetry row.
# A tick that fell far behind (suspend, stall) only replays this much history: the LD2410 ring
# buffer is 30 s, so older sample times would all come back "unavailable" anyway.
LD2410_SAMPLE_CATCHUP_SECONDS = 30.0


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

    def camera_state(self, sample_at: datetime) -> dict[str, Any]:
        """Per-camera person state at ``sample_at`` — usable outside a person window.

        The radar-driven sampler needs exactly this view so a radar-only detection records what
        the cameras were reporting at the same instant; without it the two sources cannot be
        compared at all.
        """
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
        return cameras

    def due_samples(self, now: datetime) -> tuple[dict[str, Any], ...]:
        if not self.active or self.next_sample_at is None or self.last_person_at is None:
            return ()
        clear_at = self.last_person_at + timedelta(seconds=self.stale_seconds)
        stop_at = clear_at + timedelta(seconds=self.clear_grace_seconds)
        samples: list[dict[str, Any]] = []
        while self.next_sample_at <= now and self.next_sample_at <= stop_at:
            sample_at = self.next_sample_at
            cameras = self.camera_state(sample_at)
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
            # ``_latest``/``_latest_at`` deliberately survive the window: ``camera_state`` needs
            # them between windows, and the staleness rule still decides presence.
        return tuple(samples)


@dataclass
class _RadarWindow:
    window_id: str
    started_at: datetime
    first_target_status: int | None
    first_detection_distance_cm: int | None
    last_present_at: datetime
    sample_count: int = 0
    present_sample_count: int = 0
    target_status_counts: dict[str, int] | None = None
    max_moving_energy: int | None = None
    max_motionless_energy: int | None = None
    min_detection_distance_cm: int | None = None
    max_detection_distance_cm: int | None = None

    def add_present(self, sample_time: datetime, snapshot: Mapping[str, Any]) -> None:
        self.present_sample_count += 1
        self.last_present_at = sample_time
        counts = self.target_status_counts if self.target_status_counts is not None else {}
        key = str(snapshot.get("target_status"))
        counts[key] = counts.get(key, 0) + 1
        self.target_status_counts = counts
        moving = _as_int(snapshot.get("moving_energy"))
        motionless = _as_int(snapshot.get("motionless_energy"))
        distance = _as_int(snapshot.get("detection_distance_cm"))
        if moving is not None:
            self.max_moving_energy = moving if self.max_moving_energy is None else max(self.max_moving_energy, moving)
        if motionless is not None:
            self.max_motionless_energy = (
                motionless if self.max_motionless_energy is None else max(self.max_motionless_energy, motionless)
            )
        if distance is not None:
            self.min_detection_distance_cm = (
                distance if self.min_detection_distance_cm is None else min(self.min_detection_distance_cm, distance)
            )
            self.max_detection_distance_cm = (
                distance if self.max_detection_distance_cm is None else max(self.max_detection_distance_cm, distance)
            )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def radar_presence(snapshot: Mapping[str, Any] | None) -> str:
    """Classify one LD2410 snapshot: ``present`` / ``absent`` / ``unknown``.

    Only a *fresh* frame is evidence either way; stale or unavailable data is unknown so a
    radar that stopped reporting is never mistaken for "nobody there".
    """
    if not snapshot or snapshot.get("status") != "fresh":
        return "unknown"
    return "present" if _as_int(snapshot.get("target_status")) not in (0, None) else "absent"


class RadarWindowTracker:
    """Turn 1 Hz LD2410 samples into ``radar_window_started`` / ``radar_window_closed`` events.

    Pure state machine (``idle → confirming → open → closing``), driven by the sample clock so
    it is deterministic in tests. Analysis only: nothing here feeds the engine or the gate.
    """

    def __init__(self, *, confirm_seconds: float = 3.0, clear_seconds: float = 5.0) -> None:
        self.confirm_seconds = confirm_seconds
        self.clear_seconds = clear_seconds
        self.window: _RadarWindow | None = None
        self._streak_start: datetime | None = None
        self._streak_samples: list[tuple[datetime, dict[str, Any]]] = []
        self._gap_start: datetime | None = None
        self._gap_saw_absent = False

    @property
    def open(self) -> bool:
        return self.window is not None

    def observe(self, sample_time: datetime, snapshot: Mapping[str, Any] | None) -> tuple[tuple[str, datetime, dict[str, Any]], ...]:
        """Feed one sample; return ``(event_type, recorded_at, payload)`` tuples to record."""
        presence = radar_presence(snapshot)
        data = dict(snapshot or {})
        events: list[tuple[str, datetime, dict[str, Any]]] = []
        if self.window is None:
            if presence != "present":
                self._streak_start = None
                self._streak_samples = []
                return ()
            if self._streak_start is None:
                self._streak_start = sample_time
                self._streak_samples = []
            self._streak_samples.append((sample_time, data))
            if (sample_time - self._streak_start).total_seconds() >= self.confirm_seconds:
                first_time, first = self._streak_samples[0]
                self.window = _RadarWindow(
                    window_id=uuid.uuid4().hex,
                    started_at=first_time,
                    first_target_status=_as_int(first.get("target_status")),
                    first_detection_distance_cm=_as_int(first.get("detection_distance_cm")),
                    last_present_at=first_time,
                )
                for streak_time, streak_snapshot in self._streak_samples:
                    self.window.sample_count += 1
                    self.window.add_present(streak_time, streak_snapshot)
                self._streak_start = None
                self._streak_samples = []
                self._gap_start = None
                self._gap_saw_absent = False
                events.append(
                    (
                        "radar_window_started",
                        first_time,
                        {
                            "radar_window_id": self.window.window_id,
                            "started_at": first_time.isoformat(),
                            "confirm_seconds": self.confirm_seconds,
                            "first_target_status": self.window.first_target_status,
                            "first_detection_distance_cm": self.window.first_detection_distance_cm,
                            "safety_effect": "raw_only",
                        },
                    )
                )
            return tuple(events)

        window = self.window
        window.sample_count += 1
        if presence == "present":
            window.add_present(sample_time, data)
            self._gap_start = None
            self._gap_saw_absent = False
            return ()
        # The gap is measured from the last present sample, so "absent for 5 s" means five
        # seconds since the radar last reported a target, whatever the sample cadence.
        if self._gap_start is None:
            self._gap_start = window.last_present_at
            self._gap_saw_absent = False
        if presence == "absent":
            self._gap_saw_absent = True
        if (sample_time - self._gap_start).total_seconds() >= self.clear_seconds:
            reason = "cleared" if self._gap_saw_absent else "radar_unavailable"
            events.append(self._close_event(reason))
        return tuple(events)

    def close(self, reason: str) -> tuple[str, datetime, dict[str, Any]] | None:
        """Force-close an open window (service/application stop). Confirming streaks are dropped."""
        self._streak_start = None
        self._streak_samples = []
        if self.window is None:
            return None
        return self._close_event(reason)

    def _close_event(self, reason: str) -> tuple[str, datetime, dict[str, Any]]:
        window = self.window
        assert window is not None
        self.window = None
        self._gap_start = None
        self._gap_saw_absent = False
        ended_at = window.last_present_at
        return (
            "radar_window_closed",
            ended_at,
            {
                "radar_window_id": window.window_id,
                "started_at": window.started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": round((ended_at - window.started_at).total_seconds(), 3),
                "reason": reason,
                "sample_count": window.sample_count,
                "present_sample_count": window.present_sample_count,
                "target_status_counts": dict(window.target_status_counts or {}),
                "max_moving_energy": window.max_moving_energy,
                "max_motionless_energy": window.max_motionless_energy,
                "min_detection_distance_cm": window.min_detection_distance_cm,
                "max_detection_distance_cm": window.max_detection_distance_cm,
                "safety_effect": "raw_only",
            },
        )


class RawDataManager:
    def __init__(
        self,
        config: RawStorageConfig,
        camera_ids: Iterable[str],
        *,
        uploader: DayUploader | None = None,
        clock: Callable[[], datetime] | None = None,
        ld2410_snapshot_provider: Callable[[datetime], Mapping[str, Any]] | None = None,
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
        self._last_current_day_sync = float("-inf")
        self._event_sink: Callable[[Mapping[str, Any]], None] | None = None
        self._ld2410_snapshot_provider = ld2410_snapshot_provider
        self.radar_tracker = RadarWindowTracker(
            confirm_seconds=config.radar_window_min_seconds,
            clear_seconds=config.radar_window_clear_seconds,
        )
        self._ld2410_next_sample_at: datetime | None = self.clock() if self._ld2410_sampling_enabled else None
        self._ld2410_last_recorded_received_at: str | None = None
        self._ld2410_provider_error_recorded = False
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

    def record_ld2410_status(self, state: str, details: Mapping[str, Any] | None = None) -> None:
        self.record(
            "ld2410_server_status",
            payload={"state": state, "details": dict(details or {}), "safety_effect": "raw_only"},
        )
        if state == "stopped":
            self.close_radar_window(reason="service_stopped")

    @property
    def _ld2410_sampling_enabled(self) -> bool:
        return self._ld2410_snapshot_provider is not None and self.config.ld2410_sample_interval_seconds > 0

    def close_radar_window(self, *, reason: str) -> bool:
        """Close an open radar window (service/application stop). Returns True if one was closed."""
        event = self.radar_tracker.close(reason)
        if event is None or self._closed:
            return False
        event_type, recorded_at, payload = event
        self.record(event_type, payload=payload, at=recorded_at)
        return True

    def _sample_ld2410(self, now: datetime) -> int:
        """Record due 1 Hz ``ld2410_sample`` rows and drive the radar window tracker."""
        if not self._ld2410_sampling_enabled or self._ld2410_next_sample_at is None:
            return 0
        interval = timedelta(seconds=self.config.ld2410_sample_interval_seconds)
        catch_up_floor = now - timedelta(seconds=LD2410_SAMPLE_CATCHUP_SECONDS)
        if self._ld2410_next_sample_at < catch_up_floor:
            behind = (catch_up_floor - self._ld2410_next_sample_at) // interval
            self._ld2410_next_sample_at += interval * (int(behind) + 1)
        recorded = 0
        assert self._ld2410_snapshot_provider is not None
        while self._ld2410_next_sample_at <= now:
            sample_time = self._ld2410_next_sample_at
            self._ld2410_next_sample_at += interval
            snapshot: dict[str, Any] | None
            try:
                snapshot = dict(self._ld2410_snapshot_provider(sample_time))
            except Exception:  # noqa: BLE001 - sensor failure stays explicit and raw-only.
                snapshot = None
                if not self._ld2410_provider_error_recorded:
                    logging.getLogger(__name__).exception("LD2410 sample snapshot failed")
                    self._ld2410_provider_error_recorded = True
                    self.record(
                        "ld2410_sample",
                        payload={
                            "sampled_at": sample_time.isoformat(),
                            "status": "unavailable",
                            "source": "ld2410_tcp",
                            "received_at": None,
                            "age_ms": None,
                            "reason": "provider_error",
                            "safety_effect": "raw_only",
                        },
                        at=sample_time,
                    )
                    recorded += 1
            else:
                self._ld2410_provider_error_recorded = False
                status = snapshot.get("status")
                received_at = snapshot.get("received_at")
                should_record = status == "fresh" or (
                    status == "stale" and received_at != self._ld2410_last_recorded_received_at
                )
                if should_record:
                    payload = {key: value for key, value in snapshot.items() if key != "raw_hex"}
                    payload = {"sampled_at": sample_time.isoformat(), **payload, "safety_effect": "raw_only"}
                    self.record("ld2410_sample", payload=payload, at=sample_time)
                    self._ld2410_last_recorded_received_at = received_at
                    recorded += 1
            for event_type, recorded_at, payload in self.radar_tracker.observe(sample_time, snapshot):
                self.record(event_type, payload=payload, at=recorded_at)
            self._sample_radar_cameras(sample_time, snapshot)
        return recorded

    def _sample_radar_cameras(self, sample_time: datetime, snapshot: Mapping[str, Any] | None) -> None:
        """While a radar window is open, record what the cameras were reporting at the same second.

        This is the mirror image of ``person_sample`` (camera window carrying the radar snapshot).
        It records for the whole radar window, including seconds where a camera person window is
        also open: skipping those would blank out exactly the agreeing seconds and make the window
        look like permanent disagreement. The small overlap with ``person_sample`` is deliberate —
        each radar window stays self-contained. Bounded by ``RAW_DATA_RADAR_SAMPLE_SECONDS``
        because a radar window can sit open on static clutter for hours.
        """
        window = self.radar_tracker.window
        if window is None or self.config.radar_sample_seconds <= 0:
            return
        if (sample_time - window.started_at).total_seconds() > self.config.radar_sample_seconds:
            return
        cameras = self.person_sampler.camera_state(sample_time)
        self.record(
            "radar_sample",
            payload={
                "radar_window_id": window.window_id,
                "sampled_at": sample_time.isoformat(),
                "camera_person_present": any(item["person_present"] for item in cameras.values()),
                "cameras": cameras,
                "ld2410": {key: value for key, value in dict(snapshot or {}).items() if key != "raw_hex"},
                "safety_effect": "raw_only",
            },
            at=sample_time,
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
        managed: bool = False,
        at: datetime | None = None,
    ) -> str:
        """``managed=True`` marks a process-engine session whose evidence clip stays
        open until ``vehicle_session_ended`` instead of the fixed post-roll close."""
        if self.vehicle_session_id is None:
            self.vehicle_session_id = uuid.uuid4().hex
            self.record(
                "vehicle_entered",
                payload={
                    "camera_id": camera_id,
                    "confidence": confidence,
                    "simulated": simulated,
                    "managed": managed,
                },
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
        recognized: bool = True,
        reads: int | None = None,
        reason: str = "",
        at: datetime | None = None,
    ) -> None:
        """Plate outcome. ``recognized=False`` records a 미인식 result (an entry that produced no
        usable read) so a vehicle session is never silent about its plate."""
        self.record(
            "plate_recognized",
            payload={
                "plate_number": plate_number,
                "confidence": confidence,
                "camera_id": camera_id,
                "simulated": simulated,
                "plate_bbox": dict(plate_bbox) if plate_bbox else None,
                "recognized": recognized,
                "reads": reads,
                "reason": reason,
            },
            sink_payload={"source_image_path": source_image_path} if source_image_path else None,
            at=at,
        )

    def record_vehicle_exit_start(self, *, camera_id: str, at: datetime | None = None) -> None:
        """A retrieval (출고), not an entry: the car is side-on to the front camera and shows no
        plate. Recorded separately so entry statistics and plate hit rate are not polluted."""
        self.record(
            "vehicle_exit_started",
            payload={"camera_id": camera_id, "simulated": False, "safety_effect": "raw_only"},
            at=at,
        )

    def record_vehicle_exit_end(self, *, reason: str, at: datetime | None = None) -> None:
        self.record("vehicle_exit_ended", payload={"reason": reason, "safety_effect": "raw_only"}, at=at)

    def record_plate_attempt(
        self,
        plate_number: str,
        *,
        confidence: float | None = None,
        camera_id: str = "front",
        accepted: bool = True,
        reason: str = "",
        plate_bbox: Mapping[str, float] | None = None,
        at: datetime | None = None,
    ) -> None:
        """One 1 Hz front-camera LPR read during a vehicle entry (analysis only).

        Rejected reads are recorded too (``accepted=False`` with a reason such as
        ``above_entry_line`` or ``no_plate_detected``): they are the denominator for "how often did
        LPR actually see a plate while a car was entering". Not durable — at most ~30 rows per entry.
        """
        self.record(
            "plate_attempt",
            payload={
                "plate_number": plate_number,
                "confidence": confidence,
                "camera_id": camera_id,
                "accepted": accepted,
                "reason": reason,
                "plate_bbox": dict(plate_bbox) if plate_bbox else None,
                "safety_effect": "raw_only",
            },
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
            sample_time = datetime.fromisoformat(sample["sampled_at"])
            if self._ld2410_snapshot_provider is not None:
                try:
                    sample["ld2410"] = dict(self._ld2410_snapshot_provider(sample_time))
                except Exception:  # noqa: BLE001 - sensor context failure must remain explicit and raw-only.
                    logging.getLogger(__name__).exception("LD2410 person-sample snapshot failed")
                    sample["ld2410"] = {
                        "status": "unavailable",
                        "source": "ld2410_tcp",
                        "received_at": None,
                        "age_ms": None,
                        "reason": "provider_error",
                    }
            self.record("person_sample", payload=sample, at=sample_time)
        if was_active and not self.person_sampler.active:
            self.record("person_window_closed", payload={"person_window_id": closing_window_id}, at=sampled_at)
        self._sample_ld2410(sampled_at)
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

    def request_current_day_sync(self, *, min_interval_seconds: float = 60.0) -> bool:
        """Immediate NAS upload of the current day (operator "즉시" upload mode).

        Debounced: at most one run per ``min_interval_seconds``. Uses the same
        single-flight lock as the scheduled sync. Note ``include_current_day=True``
        finalizes today's live shard (safe in-process — the writer lock is already
        held — but it rotates the active shard on every call, hence the debounce).
        """
        now = time.monotonic()
        with self._sync_lock:
            if self._sync_running:
                return False
            if now - self._last_current_day_sync < min_interval_seconds:
                return False
            self._sync_running = True
            self._last_current_day_sync = now

        def run() -> None:
            try:
                result = self.sync_completed_days(include_current_day=True)
                logging.getLogger(__name__).info(
                    "raw-data immediate sync complete uploaded=%s errors=%s",
                    result.uploaded_days,
                    result.errors,
                )
            finally:
                with self._sync_lock:
                    self._sync_running = False

        threading.Thread(target=run, name="raw-data-nas-sync-now", daemon=True).start()
        return True

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.close_radar_window(reason="application_stopped")
        except Exception:  # noqa: BLE001 - shutdown must proceed even if the last write fails.
            logging.getLogger(__name__).exception("radar window close on shutdown failed")
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
    "RadarWindowTracker",
    "RawDataManager",
    "radar_presence",
    "SyncResult",
    "WriterBusyError",
]
