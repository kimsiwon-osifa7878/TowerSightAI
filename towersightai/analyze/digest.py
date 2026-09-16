"""Per-day digest: the compact, JSON-cacheable view of one raw day that every analysis reads.

Building a digest is the only pass over the (large) raw records. Everything downstream —
episodes, coverage, media linkage, timelines — works from this structure so parameter changes
in the dashboard never re-read the shards.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from towersightai.analyze.loader import parse_ts

DIGEST_SCHEMA_VERSION = 3  # v3 adds radar_sample camera state; older cached digests rebuild on read
PERSON_LABELS = frozenset({"person", "human"})
# Inference tasks whose detections carry person labels (vehicle_detection filters cars only).
PERSON_TASKS = ("process_monitoring", "person_presence")
RADAR_STATUS_CODE = {"fresh": 0, "stale": 1, "unavailable": 2}
RADAR_SOURCE_NONE = "none"
RADAR_SOURCE_SAMPLE = "ld2410_sample"
RADAR_SOURCE_PERSON_SAMPLE = "person_sample"
UNRECOGNIZED_PLATE = "미인식"


def compact_radar(snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Reduce an LD2410 snapshot to the fields the dashboard plots."""
    if not isinstance(snapshot, Mapping):
        return None
    status = str(snapshot.get("status") or "unavailable")
    return {
        "s": RADAR_STATUS_CODE.get(status, 2),
        "ts": _int(snapshot.get("target_status")),
        "d": _int(snapshot.get("detection_distance_cm")),
        "md": _int(snapshot.get("moving_distance_cm")),
        "me": _int(snapshot.get("moving_energy")),
        "sd": _int(snapshot.get("motionless_distance_cm")),
        "se": _int(snapshot.get("motionless_energy")),
        "age": _int(snapshot.get("age_ms")),
        "rx": parse_ts(snapshot.get("received_at")),
    }


def build_digest(
    records: Iterable[Mapping[str, Any]],
    *,
    site: str,
    host: str,
    day: str,
    timezone_name: str = "Asia/Seoul",
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    tasks: dict[str, int] = {}
    person_frames: dict[str, list[list[float]]] = {}
    raw_windows: dict[str, dict[str, Any]] = {}
    radar_samples: list[list[Any]] = []
    radar_from_sample = False
    radar_windows: dict[str, dict[str, Any]] = {}
    media: list[dict[str, Any]] = []
    media_failures: list[dict[str, Any]] = []
    monitoring_open: dict[str, tuple[float, str]] = {}
    monitoring: list[list[Any]] = []
    radar_open: float | None = None
    radar_cov: list[list[float]] = []
    app_open: float | None = None
    app_cov: list[list[float]] = []
    vehicle_open: dict[str, dict[str, Any]] = {}
    vehicle_sessions: list[dict[str, Any]] = []
    plates: list[dict[str, Any]] = []
    plate_attempts: list[dict[str, Any]] = []
    first_t: float | None = None
    last_t: float | None = None
    sessions: set[str] = set()

    for record in records:
        t = parse_ts(record.get("recorded_at"))
        if t is None:
            continue
        event_type = str(record.get("event_type") or "")
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        counts[event_type] += 1
        first_t = t if first_t is None else min(first_t, t)
        last_t = t if last_t is None else max(last_t, t)
        session_id = record.get("application_session_id")
        if isinstance(session_id, str):
            sessions.add(session_id)

        if event_type == "detection_batch":
            task_id = str(payload.get("task_id") or "")
            camera_id = str(payload.get("camera_id") or "")
            detections = payload.get("detections")
            if not camera_id or not isinstance(detections, list):
                continue
            best = None
            for item in detections:
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("label") or "").strip().lower() not in PERSON_LABELS:
                    continue
                conf = _float(item.get("confidence"))
                if conf is None:
                    continue
                best = conf if best is None else max(best, conf)
            if best is None:
                continue
            task_index = tasks.setdefault(task_id, len(tasks))
            person_frames.setdefault(camera_id, []).append([round(t, 3), round(best, 4), task_index])
        elif event_type == "person_window_started":
            window_id = str(payload.get("person_window_id") or record.get("event_id"))
            raw_windows.setdefault(window_id, _new_raw_window(window_id))
            raw_windows[window_id].update(
                {"start": t, "start_event_id": record.get("event_id"), "trigger_camera": payload.get("camera_id")}
            )
        elif event_type == "person_window_closed":
            window_id = str(payload.get("person_window_id") or "")
            window = raw_windows.setdefault(window_id, _new_raw_window(window_id))
            window["end"] = t
            window["close_event_id"] = record.get("event_id")
        elif event_type == "person_sample":
            window_id = str(payload.get("person_window_id") or "")
            window = raw_windows.setdefault(window_id, _new_raw_window(window_id))
            sample_t = parse_ts(payload.get("sampled_at")) or t
            cameras = payload.get("cameras")
            cams: dict[str, bool] = {}
            confs: dict[str, float] = {}
            if isinstance(cameras, Mapping):
                for camera_id, state in cameras.items():
                    if not isinstance(state, Mapping):
                        continue
                    present = bool(state.get("person_present"))
                    cams[str(camera_id)] = present
                    if present:
                        window["cameras"].setdefault(str(camera_id), 0)
                        window["cameras"][str(camera_id)] += 1
                    dets = state.get("detections")
                    if isinstance(dets, list):
                        best = max((_float(d.get("confidence")) or 0.0 for d in dets if isinstance(d, Mapping)), default=None)
                        if best is not None:
                            confs[str(camera_id)] = round(best, 3)
            ld = compact_radar(payload.get("ld2410"))
            window["samples"].append(
                {"t": round(sample_t, 3), "present": bool(payload.get("person_present")), "cams": cams, "conf": confs, "ld": ld}
            )
            if ld is not None:
                radar_samples.append(_radar_row(sample_t, ld, RADAR_SOURCE_PERSON_SAMPLE))
        elif event_type == "ld2410_sample":
            ld = compact_radar(payload)
            sample_t = parse_ts(payload.get("sampled_at")) or t
            if ld is not None:
                radar_from_sample = True
                radar_samples.append(_radar_row(sample_t, ld, RADAR_SOURCE_SAMPLE))
        elif event_type == "radar_window_started":
            window_id = str(payload.get("radar_window_id") or record.get("event_id"))
            radar_windows.setdefault(window_id, {"id": window_id, "start": None, "end": None})
            radar_windows[window_id].update(
                {
                    "start": parse_ts(payload.get("started_at")) or t,
                    "start_event_id": record.get("event_id"),
                    "first_target_status": payload.get("first_target_status"),
                    "first_detection_distance_cm": payload.get("first_detection_distance_cm"),
                }
            )
        elif event_type == "radar_window_closed":
            window_id = str(payload.get("radar_window_id") or "")
            window = radar_windows.setdefault(window_id, {"id": window_id, "start": None, "end": None})
            window.update(
                {
                    "start": window.get("start") or parse_ts(payload.get("started_at")) or t,
                    "end": parse_ts(payload.get("ended_at")) or t,
                    "close_event_id": record.get("event_id"),
                    "reason": payload.get("reason"),
                    "duration_seconds": payload.get("duration_seconds"),
                    "present_sample_count": payload.get("present_sample_count"),
                    "sample_count": payload.get("sample_count"),
                    "target_status_counts": payload.get("target_status_counts"),
                    "max_moving_energy": payload.get("max_moving_energy"),
                    "max_motionless_energy": payload.get("max_motionless_energy"),
                    "min_detection_distance_cm": payload.get("min_detection_distance_cm"),
                    "max_detection_distance_cm": payload.get("max_detection_distance_cm"),
                }
            )
        elif event_type == "radar_sample":
            window_id = str(payload.get("radar_window_id") or "")
            window = radar_windows.setdefault(window_id, {"id": window_id, "start": None, "end": None})
            sample_t = parse_ts(payload.get("sampled_at")) or t
            cameras = payload.get("cameras")
            cams: dict[str, bool] = {}
            confs: dict[str, float] = {}
            if isinstance(cameras, Mapping):
                for camera_id, state in cameras.items():
                    if not isinstance(state, Mapping):
                        continue
                    cams[str(camera_id)] = bool(state.get("person_present"))
                    dets = state.get("detections")
                    if isinstance(dets, list):
                        best = max((_float(d.get("confidence")) or 0.0 for d in dets if isinstance(d, Mapping)), default=None)
                        if best is not None:
                            confs[str(camera_id)] = round(best, 3)
            window.setdefault("samples", []).append(
                {
                    "t": round(sample_t, 3),
                    "camera_present": bool(payload.get("camera_person_present")),
                    "cams": cams,
                    "conf": confs,
                    "ld": compact_radar(payload.get("ld2410")),
                }
            )
        elif event_type == "media_artifact_created":
            meta = payload.get("metadata")
            meta = dict(meta) if isinstance(meta, Mapping) else {}
            media.append(
                {
                    "t": parse_ts(payload.get("captured_at")) or t,
                    "kind": str(payload.get("kind") or ""),
                    "event_kind": str(meta.get("event_kind") or meta.get("source") or ""),
                    "camera": str(payload.get("camera_id") or ""),
                    "path": str(payload.get("relative_path") or ""),
                    "size": _int(payload.get("size_bytes")),
                    "sha256": payload.get("sha256"),
                    "related_event_id": payload.get("related_event_id"),
                    "event_id": record.get("event_id"),
                    "meta": meta,
                }
            )
        elif event_type == "media_capture_failed":
            media_failures.append(
                {
                    "t": t,
                    "kind": str(payload.get("kind") or ""),
                    "camera": str(payload.get("camera_id") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "related_event_id": payload.get("related_event_id"),
                }
            )
        elif event_type == "ai_started":
            task_id = str(payload.get("task_id") or "")
            if task_id in PERSON_TASKS and not bool(payload.get("simulated")):
                monitoring_open.setdefault(task_id, (t, task_id))
        elif event_type == "ai_stopped":
            task_id = str(payload.get("task_id") or "")
            opened = monitoring_open.pop(task_id, None)
            if opened is not None:
                monitoring.append([opened[0], t, task_id])
        elif event_type == "ld2410_server_status":
            state = str(payload.get("state") or "")
            if state == "client_connected":
                if radar_open is None:
                    radar_open = t
            elif state in {"client_disconnected", "stopped", "error"} and radar_open is not None:
                radar_cov.append([radar_open, t])
                radar_open = None
        elif event_type == "application_started":
            if app_open is not None:
                app_cov.append([app_open, t])
            app_open = t
        elif event_type == "application_stopped":
            if app_open is not None:
                app_cov.append([app_open, t])
                app_open = None
            for task_id, opened in list(monitoring_open.items()):
                monitoring.append([opened[0], t, task_id])
            monitoring_open.clear()
            if radar_open is not None:
                radar_cov.append([radar_open, t])
                radar_open = None
        elif event_type == "vehicle_entered":
            vid = str(record.get("vehicle_session_id") or record.get("event_id"))
            vehicle_open[vid] = {
                "id": vid,
                "start": t,
                "end": None,
                "camera": payload.get("camera_id"),
                "confidence": payload.get("confidence"),
                "simulated": bool(payload.get("simulated")),
                "managed": bool(payload.get("managed")),
                "plate": None,
                "reason": None,
            }
        elif event_type == "vehicle_session_ended":
            vid = str(record.get("vehicle_session_id") or "")
            session = vehicle_open.pop(vid, None)
            if session is not None:
                session["end"] = t
                session["reason"] = payload.get("reason")
                vehicle_sessions.append(session)
        elif event_type == "plate_recognized":
            recognized = payload.get("recognized")
            number = payload.get("plate_number")
            plate = {
                "t": t,
                "event_id": record.get("event_id"),
                "plate": number,
                "confidence": _float(payload.get("confidence")),
                "camera": payload.get("camera_id"),
                "simulated": bool(payload.get("simulated")),
                # Legacy rows (before the 미인식 outcome was recorded) have no flag: anything with
                # a plate string other than the 미인식 marker counts as recognized.
                "recognized": bool(recognized) if recognized is not None else bool(number) and str(number) != UNRECOGNIZED_PLATE,
                "reads": _int(payload.get("reads")),
                "reason": payload.get("reason") or "",
                "bbox": payload.get("plate_bbox"),
                "vehicle_session_id": record.get("vehicle_session_id"),
            }
            plates.append(plate)
            vid = str(record.get("vehicle_session_id") or "")
            if vid in vehicle_open and not vehicle_open[vid]["plate"] and plate["recognized"]:
                vehicle_open[vid]["plate"] = number
        elif event_type == "plate_attempt":
            plate_attempts.append(
                {
                    "t": t,
                    "plate": payload.get("plate_number") or "",
                    "confidence": _float(payload.get("confidence")),
                    "camera": payload.get("camera_id"),
                    "accepted": bool(payload.get("accepted")),
                    "reason": payload.get("reason") or "",
                    "bbox": payload.get("plate_bbox"),
                }
            )

    end_t = last_t if last_t is not None else 0.0
    for task_id, opened in monitoring_open.items():
        monitoring.append([opened[0], end_t, task_id])
    if radar_open is not None:
        radar_cov.append([radar_open, end_t])
    if app_open is not None:
        app_cov.append([app_open, end_t])
    vehicle_sessions.extend(vehicle_open.values())

    for camera_frames in person_frames.values():
        camera_frames.sort(key=lambda row: row[0])
    radar_samples.sort(key=lambda row: row[0])
    if radar_from_sample:
        # Once the 1 Hz stream exists, person_sample snapshots are duplicates of it.
        radar_samples = [row for row in radar_samples if row[-1] == RADAR_SOURCE_SAMPLE]
        radar_source = RADAR_SOURCE_SAMPLE
    elif radar_samples:
        radar_source = RADAR_SOURCE_PERSON_SAMPLE
    else:
        radar_source = RADAR_SOURCE_NONE
    fresh_intervals = _fresh_intervals(radar_samples, max_gap_seconds=5.0)
    radar_cov = merge_intervals([*radar_cov, *fresh_intervals])

    windows = []
    for window in raw_windows.values():
        window["samples"].sort(key=lambda item: item["t"])
        if window["start"] is None and window["samples"]:
            window["start"] = window["samples"][0]["t"]
        if window["end"] is None and window["samples"]:
            window["end"] = window["samples"][-1]["t"]
        if window["start"] is None:
            continue
        windows.append(window)
    windows.sort(key=lambda item: item["start"])

    for window in radar_windows.values():
        samples = window.setdefault("samples", [])
        samples.sort(key=lambda item: item["t"])
        agreeing = sum(1 for item in samples if item["camera_present"])
        window["camera_samples"] = len(samples)
        window["camera_present_samples"] = agreeing
        window["camera_agreement"] = round(agreeing / len(samples), 3) if samples else None

    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "site": site,
        "host": host,
        "day": day,
        "timezone": timezone_name,
        "counts": dict(counts),
        "application_sessions": len(sessions),
        "tasks": sorted(tasks, key=tasks.get),  # type: ignore[arg-type]
        "person_frames": person_frames,
        "raw_windows": windows,
        "radar_source": radar_source,
        "radar_samples": radar_samples,
        "radar_windows": sorted(radar_windows.values(), key=lambda item: item.get("start") or 0.0),
        "media": sorted(media, key=lambda item: item["t"]),
        "media_failures": media_failures,
        "coverage": {
            "monitoring": merge_intervals([[s, e] for s, e, _task in monitoring]),
            "monitoring_by_task": monitoring,
            "radar": radar_cov,
            "app": merge_intervals(app_cov),
        },
        "vehicle_sessions": sorted(vehicle_sessions, key=lambda item: item["start"]),
        "plates": plates,
        "plate_attempts": plate_attempts,
        "first_t": first_t,
        "last_t": last_t,
    }


def merge_intervals(intervals: Iterable[list[float]], *, gap: float = 0.0) -> list[list[float]]:
    items = sorted([float(s), float(e)] for s, e in ((i[0], i[1]) for i in intervals) if e is not None and s is not None and e >= s)
    merged: list[list[float]] = []
    for start, end in items:
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def coverage_seconds(intervals: Iterable[list[float]], *, start: float | None = None, end: float | None = None) -> float:
    total = 0.0
    for s, e in intervals:
        if start is not None:
            s = max(s, start)
        if end is not None:
            e = min(e, end)
        if e > s:
            total += e - s
    return total


def save_digest(path: Path, digest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(digest), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def load_digest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != DIGEST_SCHEMA_VERSION:
        return None
    return payload


def _new_raw_window(window_id: str) -> dict[str, Any]:
    return {"id": window_id, "start": None, "end": None, "cameras": {}, "samples": [], "trigger_camera": None}


def _radar_row(t: float, ld: Mapping[str, Any], source: str) -> list[Any]:
    return [round(t, 3), ld["s"], ld["ts"], ld["d"], ld["me"], ld["se"], ld["md"], ld["sd"], source]


def _fresh_intervals(radar_samples: list[list[Any]], *, max_gap_seconds: float) -> list[list[float]]:
    intervals: list[list[float]] = []
    for row in radar_samples:
        if row[1] != 0:
            continue
        t = row[0]
        if intervals and t - intervals[-1][1] <= max_gap_seconds:
            intervals[-1][1] = t
        else:
            intervals.append([t, t])
    return intervals


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DIGEST_SCHEMA_VERSION",
    "PERSON_TASKS",
    "RADAR_SOURCE_NONE",
    "RADAR_SOURCE_PERSON_SAMPLE",
    "RADAR_SOURCE_SAMPLE",
    "build_digest",
    "compact_radar",
    "coverage_seconds",
    "load_digest",
    "merge_intervals",
    "save_digest",
]
