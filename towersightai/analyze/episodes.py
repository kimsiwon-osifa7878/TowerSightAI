"""Rebuild person "episodes" from a day digest with engine-equivalent rules.

An episode is the reviewable unit: "from t0 to t1 source X reported a person". Camera episodes
replay the engine's person watch (confidence gate, consecutive-frame debounce, stale expiry,
camera-role subset); radar episodes replay the raw radar window tracker (fresh & target≠0,
confirm/clear seconds, optional gates). Pairing by time overlap yields the agreement class
``both`` / ``camera_only`` / ``radar_only`` that the dashboard compares.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, Mapping, Sequence

SOURCE_CAMERA = "camera"
SOURCE_RADAR = "radar"
AGREEMENT_BOTH = "both"
AGREEMENT_CAMERA_ONLY = "camera_only"
AGREEMENT_RADAR_ONLY = "radar_only"
MAX_TIMELINE_POINTS = 400
IMAGE_LEAD_SECONDS = 2.0
IMAGE_TAIL_SECONDS = 7.0
VIDEO_LEAD_SECONDS = 6.0
VIDEO_TAIL_SECONDS = 1.0
PERSON_MEDIA_KINDS = frozenset({"person", "person_end", "radar", "radar_end"})
PLATE_MEDIA_KINDS = frozenset({"plate_image", "plate_crop"})
# A plate is read while the car drives in, which can be up to a minute either side of the person
# episode that the driver getting out produces.
PLATE_WINDOW_SECONDS = 60.0
UNRECOGNIZED_PLATE = "미인식"


@dataclass(frozen=True)
class EpisodeParams:
    """Dashboard-adjustable replay parameters. Defaults mirror the current engine/raw defaults."""

    min_confidence: float = 0.2
    consecutive_frames: int = 2
    stale_seconds: float = 3.0
    merge_gap_seconds: float = 5.0
    cameras: tuple[str, ...] = ()  # empty = every camera
    tasks: tuple[str, ...] = ()  # empty = every person-capable task
    radar_confirm_seconds: float = 3.0
    radar_clear_seconds: float = 5.0
    radar_moving_only: bool = False
    radar_max_distance_cm: int = 0  # 0 = no gate
    radar_min_energy: int = 0
    pair_tolerance_seconds: float = 3.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be within [0, 1]")
        if self.consecutive_frames < 1:
            raise ValueError("consecutive_frames must be >= 1")
        for name in ("stale_seconds", "radar_confirm_seconds", "radar_clear_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        for name in ("merge_gap_seconds", "pair_tolerance_seconds", "radar_max_distance_cm", "radar_min_energy"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        object.__setattr__(self, "cameras", tuple(dict.fromkeys(c for c in self.cameras if c)))
        object.__setattr__(self, "tasks", tuple(dict.fromkeys(t for t in self.tasks if t)))

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> "EpisodeParams":
        values: dict[str, Any] = {}
        for item in fields(cls):
            raw = query.get(item.name)
            if raw is None or raw == "":
                continue
            if item.type in ("float",):
                values[item.name] = float(raw)
            elif item.type in ("int",):
                values[item.name] = int(float(raw))
            elif item.type in ("bool",):
                values[item.name] = raw.lower() in {"1", "true", "yes", "on"}
            else:
                values[item.name] = tuple(part.strip() for part in raw.split(",") if part.strip())
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cameras"] = list(self.cameras)
        data["tasks"] = list(self.tasks)
        return data

    @property
    def key(self) -> str:
        return hashlib.sha1(repr(sorted(self.to_dict().items())).encode("utf-8")).hexdigest()[:12]


@dataclass
class Episode:
    id: str
    site: str
    host: str
    day: str
    source: str
    start: float
    end: float
    cameras: list[str] = field(default_factory=list)
    max_confidence: float | None = None
    frame_count: int = 0
    sample_count: int = 0
    agreement: str = ""
    paired_ids: list[str] = field(default_factory=list)
    group_id: str = ""
    timeline: list[list[Any]] = field(default_factory=list)
    radar: dict[str, Any] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)
    media_failures: list[dict[str, Any]] = field(default_factory=list)
    raw_window_ids: list[str] = field(default_factory=list)
    radar_window_ids: list[str] = field(default_factory=list)
    vehicle_session_ids: list[str] = field(default_factory=list)
    partial_radar: bool = False
    # Plate of the vehicle this episode belongs to (from the overlapping vehicle session, else a
    # nearby plate_recognized row). ``plate_recognized=False`` means the entry ended as 미인식.
    plate: str = ""
    plate_confidence: float | None = None
    plate_recognized: bool = False
    plate_reads: int | None = None
    plate_source: str = ""
    plates: list[dict[str, Any]] = field(default_factory=list)
    plate_attempts: list[dict[str, Any]] = field(default_factory=list)
    # What the cameras reported while a radar window covering this episode was open
    # (from radar_sample rows). The direct camera-vs-radar comparison at the same instant.
    camera_check: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        return data


def episode_id(site: str, host: str, source: str, start: float) -> str:
    """Deterministic id: stable across parameter tweaks as long as the episode starts at the
    same second, so labels survive re-indexing. (Ends move more than starts.)"""
    digest = hashlib.sha1(f"{site}|{host}|{source}|{int(round(start))}".encode("utf-8")).hexdigest()
    return digest[:16]


def build_camera_episodes(digest: Mapping[str, Any], params: EpisodeParams) -> list[Episode]:
    site, host, day = str(digest.get("site")), str(digest.get("host")), str(digest.get("day"))
    tasks: list[str] = list(digest.get("tasks") or [])
    allowed_tasks = {tasks.index(t) for t in params.tasks if t in tasks} if params.tasks else None
    frames_by_camera: Mapping[str, list[list[float]]] = digest.get("person_frames") or {}
    intervals: list[tuple[float, float, str, float, int]] = []  # start, end, camera, max_conf, frames
    for camera_id, frames in frames_by_camera.items():
        if params.cameras and camera_id not in params.cameras:
            continue
        streak: list[list[float]] = []
        active_start: float | None = None
        active_last: float | None = None
        active_conf = 0.0
        active_frames = 0
        for row in frames:
            t, conf = float(row[0]), float(row[1])
            task_index = int(row[2]) if len(row) > 2 else 0
            if allowed_tasks is not None and task_index not in allowed_tasks:
                continue
            if conf < params.min_confidence:
                continue
            if active_start is not None:
                if t - float(active_last or t) > params.stale_seconds:
                    intervals.append((active_start, float(active_last), camera_id, active_conf, active_frames))
                    active_start = None
                    streak = []
                else:
                    active_last = t
                    active_conf = max(active_conf, conf)
                    active_frames += 1
                    continue
            if streak and t - streak[-1][0] > params.stale_seconds:
                streak = []
            streak.append([t, conf])
            if len(streak) >= params.consecutive_frames:
                active_start = streak[0][0]
                active_last = t
                active_conf = max(item[1] for item in streak)
                active_frames = len(streak)
                streak = []
        if active_start is not None:
            intervals.append((active_start, float(active_last), camera_id, active_conf, active_frames))

    intervals.sort(key=lambda item: item[0])
    merged: list[dict[str, Any]] = []
    for start, end, camera_id, conf, frames in intervals:
        if merged and start <= merged[-1]["end"] + params.merge_gap_seconds:
            current = merged[-1]
            current["end"] = max(current["end"], end)
            current["cameras"].add(camera_id)
            current["conf"] = max(current["conf"], conf)
            current["frames"] += frames
        else:
            merged.append({"start": start, "end": end, "cameras": {camera_id}, "conf": conf, "frames": frames})

    episodes: list[Episode] = []
    for item in merged:
        episode = Episode(
            id=episode_id(site, host, SOURCE_CAMERA, item["start"]),
            site=site,
            host=host,
            day=day,
            source=SOURCE_CAMERA,
            start=item["start"],
            end=item["end"],
            cameras=sorted(item["cameras"]),
            max_confidence=round(item["conf"], 4),
            frame_count=item["frames"],
        )
        episode.timeline = _camera_timeline(frames_by_camera, episode, params)
        episodes.append(episode)
    return episodes


def radar_present(row: Sequence[Any], params: EpisodeParams) -> str:
    """Classify one compact radar row: present / absent / unknown (mirrors raw_data.radar_presence
    plus the dashboard's optional gates)."""
    status, target = row[1], row[2]
    if status != 0 or target is None:
        return "unknown"
    if int(target) == 0:
        return "absent"
    if params.radar_moving_only and int(target) not in (1, 3):
        return "absent"
    distance = row[3]
    if params.radar_max_distance_cm and distance is not None and int(distance) > params.radar_max_distance_cm:
        return "absent"
    if params.radar_min_energy:
        energy = max(int(row[4] or 0), int(row[5] or 0))
        if energy < params.radar_min_energy:
            return "absent"
    return "present"


def build_radar_episodes(digest: Mapping[str, Any], params: EpisodeParams) -> list[Episode]:
    site, host, day = str(digest.get("site")), str(digest.get("host")), str(digest.get("day"))
    rows: list[list[Any]] = list(digest.get("radar_samples") or [])
    partial = digest.get("radar_source") != "ld2410_sample"
    episodes: list[Episode] = []
    streak: list[list[Any]] = []
    window: list[list[Any]] | None = None
    gap_start: float | None = None
    total_samples = 0

    def close_window() -> None:
        nonlocal window, gap_start, total_samples
        if not window:
            return
        episodes.append(_radar_episode(site, host, day, window, total_samples, partial))
        window = None
        gap_start = None
        total_samples = 0

    for row in rows:
        t = float(row[0])
        presence = radar_present(row, params)
        if window is None:
            if presence != "present":
                streak = []
                continue
            streak.append(row)
            if t - float(streak[0][0]) >= params.radar_confirm_seconds:
                window = list(streak)
                total_samples = len(streak)
                streak = []
            continue
        total_samples += 1
        if presence == "present":
            window.append(row)
            gap_start = None
            continue
        if gap_start is None:
            gap_start = t
        if t - gap_start >= params.radar_clear_seconds:
            close_window()
    close_window()
    return episodes


def pair_episodes(camera: list[Episode], radar: list[Episode], params: EpisodeParams) -> list[Episode]:
    """Assign agreement class and presence-group ids by time overlap (± tolerance)."""
    tol = params.pair_tolerance_seconds
    all_eps = sorted([*camera, *radar], key=lambda e: e.start)
    parent: dict[str, str] = {e.id: e.id for e in all_eps}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for cam in camera:
        cam.paired_ids = []
    for rad in radar:
        rad.paired_ids = []
    for cam in camera:
        for rad in radar:
            if rad.start > cam.end + tol:
                break
            if _overlaps(cam, rad, tol):
                cam.paired_ids.append(rad.id)
                rad.paired_ids.append(cam.id)
                parent[find(cam.id)] = find(rad.id)
    for ep in all_eps:
        ep.group_id = find(ep.id)
        if ep.source == SOURCE_CAMERA:
            ep.agreement = AGREEMENT_BOTH if ep.paired_ids else AGREEMENT_CAMERA_ONLY
        else:
            ep.agreement = AGREEMENT_BOTH if ep.paired_ids else AGREEMENT_RADAR_ONLY
    return all_eps


def attach_context(episodes: Iterable[Episode], digest: Mapping[str, Any]) -> None:
    """Link media, raw person windows, recorded radar windows and vehicle sessions by time."""
    media = list(digest.get("media") or [])
    failures = list(digest.get("media_failures") or [])
    raw_windows = list(digest.get("raw_windows") or [])
    radar_windows = list(digest.get("radar_windows") or [])
    vehicles = list(digest.get("vehicle_sessions") or [])
    plates = list(digest.get("plates") or [])
    attempts = list(digest.get("plate_attempts") or [])
    for ep in episodes:
        img_lo, img_hi = ep.start - IMAGE_LEAD_SECONDS, ep.end + IMAGE_TAIL_SECONDS
        vid_lo, vid_hi = ep.start - VIDEO_LEAD_SECONDS, ep.end + VIDEO_TAIL_SECONDS
        # Person/radar evidence bundles only: vehicle clips can span a whole session (dozens of
        # 5-minute parts) and would drown the episode view; vehicle context is kept as ids.
        ep.media = [
            item
            for item in media
            if item.get("event_kind") in PERSON_MEDIA_KINDS
            and (
                (item["kind"] == "video" and vid_lo <= item["t"] <= vid_hi)
                or (item["kind"] != "video" and img_lo <= item["t"] <= img_hi)
            )
        ]
        ep.media_failures = [item for item in failures if img_lo <= item["t"] <= img_hi]
        ep.raw_window_ids = [
            w["id"] for w in raw_windows if w.get("start") is not None and _span_overlaps(ep, w["start"], w.get("end") or w["start"], 1.0)
        ]
        ep.radar_window_ids = [
            w["id"] for w in radar_windows if w.get("start") is not None and _span_overlaps(ep, w["start"], w.get("end") or w["start"], 1.0)
        ]
        ep.vehicle_session_ids = [
            v["id"] for v in vehicles if _span_overlaps(ep, v["start"], v.get("end") or v["start"], 0.0)
        ]
        _attach_plate(ep, plates, attempts, vehicles, media)
        _attach_camera_check(ep, radar_windows)


def build_day_episodes(digest: Mapping[str, Any], params: EpisodeParams) -> list[Episode]:
    camera = build_camera_episodes(digest, params)
    radar = build_radar_episodes(digest, params)
    episodes = pair_episodes(camera, radar, params)
    attach_context(episodes, digest)
    return episodes


def _attach_camera_check(ep: Episode, radar_windows: list[dict[str, Any]]) -> None:
    """Summarize the camera side of every radar window overlapping this episode."""
    samples = [
        sample
        for window in radar_windows
        if window["id"] in ep.radar_window_ids
        for sample in (window.get("samples") or [])
        if ep.start - 1.0 <= sample["t"] <= ep.end + 1.0
    ]
    if not samples:
        return
    per_camera: dict[str, int] = {}
    best_confidence: float | None = None
    for sample in samples:
        for camera_id, present in (sample.get("cams") or {}).items():
            if present:
                per_camera[camera_id] = per_camera.get(camera_id, 0) + 1
        for value in (sample.get("conf") or {}).values():
            best_confidence = value if best_confidence is None else max(best_confidence, value)
    agreeing = sum(1 for sample in samples if sample["camera_present"])
    ep.camera_check = {
        "samples": len(samples),
        "camera_present_samples": agreeing,
        "agreement": round(agreeing / len(samples), 3),
        "by_camera": per_camera,
        "max_confidence": best_confidence,
    }


def _attach_plate(
    ep: Episode,
    plates: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    media: list[dict[str, Any]],
) -> None:
    """Link the plate of the vehicle this episode belongs to, plus the 1 Hz reads behind it."""
    session_ids = {vid for vid in ep.vehicle_session_ids if vid}
    lo, hi = ep.start - PLATE_WINDOW_SECONDS, ep.end + PLATE_WINDOW_SECONDS
    ep.plates = [
        item
        for item in plates
        if (item.get("vehicle_session_id") in session_ids if item.get("vehicle_session_id") else False) or lo <= item["t"] <= hi
    ]
    ep.plate_attempts = [item for item in attempts if lo <= item["t"] <= hi]
    linked_ids = {str(item.get("event_id")) for item in ep.plates if item.get("event_id")}
    if linked_ids:
        known = {item["event_id"] for item in ep.media}
        ep.media = ep.media + [
            item
            for item in media
            if item["kind"] in PLATE_MEDIA_KINDS
            and str(item.get("related_event_id")) in linked_ids
            and item["event_id"] not in known
        ]

    def rank(item: dict[str, Any]) -> tuple[int, int, float]:
        in_session = 1 if item.get("vehicle_session_id") in session_ids else 0
        return (in_session, int(item.get("reads") or 0), float(item.get("confidence") or 0.0))

    recognized = [item for item in ep.plates if item.get("recognized") and item.get("plate")]
    if recognized:
        best = max(recognized, key=rank)
        ep.plate = str(best["plate"])
        ep.plate_confidence = best.get("confidence")
        ep.plate_recognized = True
        ep.plate_reads = best.get("reads")
        ep.plate_source = "vehicle_session" if best.get("vehicle_session_id") in session_ids else "nearby_read"
        return
    if ep.plates:
        best = max(ep.plates, key=rank)
        ep.plate = UNRECOGNIZED_PLATE
        ep.plate_reads = best.get("reads")
        ep.plate_source = "vehicle_session" if best.get("vehicle_session_id") in session_ids else "nearby_read"
        return
    session_plate = next((v.get("plate") for v in vehicles if v["id"] in session_ids and v.get("plate")), None)
    if session_plate:
        ep.plate = str(session_plate)
        ep.plate_recognized = True
        ep.plate_source = "vehicle_session"


def _overlaps(a: Episode, b: Episode, tol: float) -> bool:
    return a.start - tol <= b.end and b.start - tol <= a.end


def _span_overlaps(ep: Episode, start: float, end: float, tol: float) -> bool:
    return ep.start - tol <= end and start - tol <= ep.end


def _camera_timeline(frames_by_camera: Mapping[str, list[list[float]]], ep: Episode, params: EpisodeParams) -> list[list[Any]]:
    points: list[list[Any]] = []
    lo, hi = ep.start - params.stale_seconds, ep.end + params.stale_seconds
    for camera_id in ep.cameras:
        for row in frames_by_camera.get(camera_id, []):
            if lo <= row[0] <= hi:
                points.append([row[0], camera_id, row[1]])
    points.sort(key=lambda item: item[0])
    return _downsample(points)


def _downsample(points: list[list[Any]], limit: int = MAX_TIMELINE_POINTS) -> list[list[Any]]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(i * step)] for i in range(limit)]


def _radar_episode(site: str, host: str, day: str, rows: list[list[Any]], total_samples: int, partial: bool) -> Episode:
    start, end = float(rows[0][0]), float(rows[-1][0])
    status_counts: dict[str, int] = {}
    distances = [int(r[3]) for r in rows if r[3] is not None]
    moving = [int(r[4]) for r in rows if r[4] is not None]
    still = [int(r[5]) for r in rows if r[5] is not None]
    for r in rows:
        key = str(r[2])
        status_counts[key] = status_counts.get(key, 0) + 1
    ep = Episode(
        id=episode_id(site, host, SOURCE_RADAR, start),
        site=site,
        host=host,
        day=day,
        source=SOURCE_RADAR,
        start=start,
        end=end,
        sample_count=total_samples,
        partial_radar=partial,
        radar={
            "present_samples": len(rows),
            "target_status_counts": status_counts,
            "min_distance_cm": min(distances) if distances else None,
            "max_distance_cm": max(distances) if distances else None,
            "median_distance_cm": sorted(distances)[len(distances) // 2] if distances else None,
            "max_moving_energy": max(moving) if moving else None,
            "max_motionless_energy": max(still) if still else None,
            "moving_fraction": round(sum(1 for r in rows if r[2] in (1, 3)) / len(rows), 3),
        },
    )
    ep.timeline = _downsample([[r[0], r[2], r[3], r[4], r[5]] for r in rows])
    return ep


__all__ = [
    "AGREEMENT_BOTH",
    "AGREEMENT_CAMERA_ONLY",
    "AGREEMENT_RADAR_ONLY",
    "Episode",
    "EpisodeParams",
    "SOURCE_CAMERA",
    "SOURCE_RADAR",
    "attach_context",
    "build_camera_episodes",
    "build_day_episodes",
    "build_radar_episodes",
    "episode_id",
    "pair_episodes",
    "radar_present",
]
