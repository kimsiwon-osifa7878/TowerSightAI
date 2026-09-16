"""Offline analysis package: digest, episodes, coverage, labels, metrics — no NAS, no hardware."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from towersightai.analyze.config import AnalysisPaths, SiteConfig, SitesStore, site_from_env
from towersightai.analyze.digest import build_digest, coverage_seconds, load_digest, merge_intervals, save_digest
from towersightai.analyze.episodes import (
    AGREEMENT_BOTH,
    AGREEMENT_CAMERA_ONLY,
    AGREEMENT_RADAR_ONLY,
    EpisodeParams,
    build_camera_episodes,
    build_day_episodes,
    build_radar_episodes,
    episode_id,
)
from towersightai.analyze.labels import LabelStore, reviewer_agreement
from towersightai.analyze.loader import load_day_records
from towersightai.analyze.metrics import coverage_for_sources, evaluate_against_reference, summarize, sweep_camera
from towersightai.analyze.store import AnalysisStore

T0 = datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc)


def _iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _record(event_type: str, seconds: float, payload: dict, event_id: str | None = None) -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id or f"{event_type}-{seconds}",
        "event_type": event_type,
        "recorded_at": _iso(seconds),
        "application_session_id": "app-1",
        "vehicle_session_id": None,
        "payload": payload,
    }


def _person_batch(seconds: float, camera: str, confidence: float, task: str = "process_monitoring") -> dict:
    return _record(
        "detection_batch",
        seconds,
        {
            "task_id": task,
            "camera_id": camera,
            "detections": [{"label": "person", "confidence": confidence, "camera_id": camera, "bbox": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.2}}],
        },
    )


def _ld(seconds: float, target: int, status: str = "fresh", distance: int = 220, moving: int = 10, still: int = 40) -> dict:
    return _record(
        "ld2410_sample",
        seconds,
        {
            "sampled_at": _iso(seconds),
            "status": status,
            "target_status": target,
            "detection_distance_cm": distance,
            "moving_distance_cm": distance,
            "moving_energy": moving,
            "motionless_distance_cm": distance,
            "motionless_energy": still,
            "age_ms": 120,
            "received_at": _iso(seconds - 0.1),
            "safety_effect": "raw_only",
        },
    )


def _synthetic_day() -> list[dict]:
    records = [
        _record("application_started", 0, {"camera_ids": ["front", "opposite_side"]}),
        _record("ai_started", 1, {"task_id": "process_monitoring", "camera_ids": ["front"], "simulated": False}),
        _record("ld2410_server_status", 1, {"state": "client_connected", "details": {"client_ip": "10.0.0.5"}}),
    ]
    # Camera episode A (front): 10 frames at 10 fps from t=100, conf 0.8 → both sources.
    for i in range(10):
        records.append(_person_batch(100 + i * 0.1, "front", 0.8))
    # Radar present 100..106 (7 samples ≥ 3 s confirm).
    for s in range(95, 100):
        records.append(_ld(s, 0))
    for s in range(100, 107):
        records.append(_ld(s, 2))
    for s in range(107, 120):
        records.append(_ld(s, 0))
    # Camera blip B (opposite_side): one frame at conf 0.35 → filtered by 2-frame debounce.
    records.append(_person_batch(200, "opposite_side", 0.35))
    # Camera episode C (opposite_side): 4 frames conf 0.5 at t=300 → camera only (radar absent).
    for i in range(4):
        records.append(_person_batch(300 + i * 0.1, "opposite_side", 0.5))
    for s in range(295, 320):
        records.append(_ld(s, 0))
    # Radar-only episode D at 400..410 (moving), camera silent.
    for s in range(400, 411):
        records.append(_ld(s, 1, distance=150, moving=60))
    for s in range(411, 420):
        records.append(_ld(s, 0))
    # Radar unknown (stale) streak: must not count as absent nor present.
    for s in range(500, 510):
        records.append(_ld(s, 2, status="stale"))
    # raw person window + samples + media for episode A.
    records.append(_record("person_window_started", 100, {"person_window_id": "pw-A", "camera_id": "front"}, event_id="pw-A-start"))
    for k in range(4):
        records.append(
            _record(
                "person_sample",
                100 + k * 0.5,
                {
                    "person_window_id": "pw-A",
                    "sampled_at": _iso(100 + k * 0.5),
                    "person_present": True,
                    "cameras": {"front": {"person_present": True, "detections": [{"confidence": 0.8}]}},
                    "ld2410": {"status": "fresh", "target_status": 2, "detection_distance_cm": 220, "age_ms": 50, "received_at": _iso(100)},
                },
            )
        )
    records.append(_record("person_window_closed", 107, {"person_window_id": "pw-A"}, event_id="pw-A-close"))
    records.append(
        _record(
            "media_artifact_created",
            100,
            {"kind": "snapshot", "camera_id": "front", "relative_path": "media/images/000140-000000-person-front.jpg", "size_bytes": 10, "sha256": "x", "captured_at": _iso(100), "related_event_id": "pw-A-start", "metadata": {"event_kind": "person"}},
        )
    )
    records.append(
        _record(
            "media_artifact_created",
            100,
            {"kind": "video", "camera_id": "front", "relative_path": "media/videos/000140-000000-person-front-part001.mkv", "size_bytes": 10, "sha256": "y", "captured_at": _iso(100), "related_event_id": "pw-A-start", "metadata": {"event_kind": "person"}},
        )
    )
    # A long vehicle clip must not attach to person episodes.
    records.append(
        _record(
            "media_artifact_created",
            99,
            {"kind": "video", "camera_id": "opposite_side", "relative_path": "media/videos/vehicle-part001.mkv", "size_bytes": 10, "sha256": "z", "captured_at": _iso(99), "related_event_id": "veh", "metadata": {"event_kind": "vehicle"}},
        )
    )
    records.append(_record("ai_stopped", 600, {"task_id": "process_monitoring", "reason": "requested"}))
    records.append(_record("ld2410_server_status", 650, {"state": "client_disconnected", "details": {}}))
    records.append(_record("application_stopped", 700, {}))
    return records


def _write_day(day_dir: Path, records: list[dict]) -> None:
    day_dir.mkdir(parents=True, exist_ok=True)
    half = len(records) // 2
    with gzip.open(day_dir / "events-20260903-0900.jsonl.gz", "wt", encoding="utf-8") as fp:
        for record in records[:half]:
            fp.write(json.dumps(record) + "\n")
    with (day_dir / "events-20260903-0900-01.jsonl").open("w", encoding="utf-8") as fp:
        fp.write("not json\n")
        for record in records[half:]:
            fp.write(json.dumps(record) + "\n")


@pytest.fixture
def digest():
    return build_digest(_synthetic_day(), site="s", host="h", day="2026-09-03", timezone_name="UTC")


def test_loader_reads_gz_and_plain_shards_sorted_and_skips_bad_lines(tmp_path: Path):
    _write_day(tmp_path / "d", _synthetic_day())
    records = load_day_records(tmp_path / "d")
    assert len(records) == len(_synthetic_day())
    assert records == sorted(records, key=lambda r: r["recorded_at"])


def test_digest_collects_frames_radar_media_and_coverage(digest):
    assert digest["counts"]["detection_batch"] == 15
    assert set(digest["person_frames"]) == {"front", "opposite_side"}
    assert digest["radar_source"] == "ld2410_sample"
    # person_sample snapshots are dropped once the 1 Hz stream exists.
    assert all(row[-1] == "ld2410_sample" for row in digest["radar_samples"])
    assert len(digest["raw_windows"]) == 1 and digest["raw_windows"][0]["start_event_id"] == "pw-A-start"
    assert coverage_seconds(digest["coverage"]["monitoring"]) == pytest.approx(599.0)
    assert coverage_seconds(digest["coverage"]["radar"]) == pytest.approx(649.0)
    assert coverage_seconds(digest["coverage"]["app"]) == pytest.approx(700.0)


def test_digest_falls_back_to_person_sample_radar_when_no_stream():
    records = [r for r in _synthetic_day() if r["event_type"] != "ld2410_sample"]
    digest = build_digest(records, site="s", host="h", day="2026-09-03")
    assert digest["radar_source"] == "person_sample"
    assert len(digest["radar_samples"]) == 4
    radar = build_radar_episodes(digest, EpisodeParams(radar_confirm_seconds=1.0))
    assert radar and all(ep.partial_radar for ep in radar)


def test_camera_episodes_follow_debounce_and_confidence_gate(digest):
    eps = build_camera_episodes(digest, EpisodeParams())
    assert [(round(e.start - T0.timestamp()), e.cameras) for e in eps] == [(100, ["front"]), (300, ["opposite_side"])]
    assert eps[0].frame_count == 10 and eps[0].max_confidence == pytest.approx(0.8)
    # Raising the gate above 0.5 removes episode C; excluding the camera removes it too.
    assert [e.cameras for e in build_camera_episodes(digest, EpisodeParams(min_confidence=0.6))] == [["front"]]
    assert [e.cameras for e in build_camera_episodes(digest, EpisodeParams(cameras=("front",)))] == [["front"]]
    # A single frame qualifies only with consecutive_frames=1.
    assert len(build_camera_episodes(digest, EpisodeParams(consecutive_frames=1))) == 3


def test_radar_episodes_confirm_clear_and_gates(digest):
    params = EpisodeParams()
    eps = build_radar_episodes(digest, params)
    starts = [round(e.start - T0.timestamp()) for e in eps]
    assert starts == [100, 400]
    assert eps[0].end - eps[0].start == pytest.approx(6.0)
    assert eps[1].radar["moving_fraction"] == 1.0 and eps[1].radar["median_distance_cm"] == 150
    # Stale streak never becomes an episode.
    assert all(round(e.start - T0.timestamp()) != 500 for e in eps)
    assert [round(e.start - T0.timestamp()) for e in build_radar_episodes(digest, EpisodeParams(radar_moving_only=True))] == [400]
    assert [round(e.start - T0.timestamp()) for e in build_radar_episodes(digest, EpisodeParams(radar_max_distance_cm=200))] == [400]
    assert build_radar_episodes(digest, EpisodeParams(radar_confirm_seconds=20.0)) == []


def test_pairing_media_and_context(digest):
    eps = build_day_episodes(digest, EpisodeParams())
    by_start = {round(e.start - T0.timestamp()): e for e in eps if e.source == "camera"}
    radar_by_start = {round(e.start - T0.timestamp()): e for e in eps if e.source == "radar"}
    assert by_start[100].agreement == AGREEMENT_BOTH and radar_by_start[100].agreement == AGREEMENT_BOTH
    assert by_start[100].group_id == radar_by_start[100].group_id
    assert by_start[300].agreement == AGREEMENT_CAMERA_ONLY
    assert radar_by_start[400].agreement == AGREEMENT_RADAR_ONLY
    kinds = sorted(m["kind"] for m in by_start[100].media)
    assert kinds == ["snapshot", "video"]  # the vehicle clip is excluded
    assert by_start[100].raw_window_ids == ["pw-A"]
    assert by_start[100].id == episode_id("s", "h", "camera", by_start[100].start)


def test_episode_id_is_stable_within_the_same_second():
    assert episode_id("s", "h", "camera", 100.2) == episode_id("s", "h", "camera", 100.4)
    assert episode_id("s", "h", "camera", 100.0) != episode_id("s", "h", "radar", 100.0)


def test_params_from_query_and_validation():
    params = EpisodeParams.from_query({"min_confidence": "0.5", "consecutive_frames": "3", "cameras": "front, rear_side", "radar_moving_only": "true", "radar_max_distance_cm": "300"})
    assert params.min_confidence == 0.5 and params.consecutive_frames == 3
    assert params.cameras == ("front", "rear_side") and params.radar_moving_only and params.radar_max_distance_cm == 300
    with pytest.raises(ValueError):
        EpisodeParams(min_confidence=1.5)
    with pytest.raises(ValueError):
        EpisodeParams(consecutive_frames=0)
    assert EpisodeParams().key != params.key


def test_labels_latest_history_and_reviewer_agreement(tmp_path: Path):
    store = LabelStore(tmp_path / "labels.jsonl")
    common = dict(site="s", host="h", day="2026-09-03", source="camera", start=1.0, end=2.0)
    store.append(episode_id="e1", verdict="person", reviewer="a", **common)
    store.append(episode_id="e1", verdict="no_person", reviewer="b", tags=["문 밖 사람"], **common)
    store.append(episode_id="e2", verdict="person", reviewer="a", **common)
    store.append(episode_id="e2", verdict="person", reviewer="b", **common)
    with pytest.raises(ValueError):
        store.append(episode_id="e3", verdict="maybe", **common)
    assert store.latest()["e1"]["verdict"] == "no_person"
    assert len(store.history("e1")) == 2
    agreement = reviewer_agreement(store.latest_by_reviewer())
    assert agreement["reviewers"] == ["a", "b"]
    assert agreement["pairs"][0]["shared"] == 2 and agreement["pairs"][0]["agreement"] == 0.5
    assert agreement["pairs"][0]["conflicts"] == ["e1"]


def test_summary_metrics_use_labels_groups_and_coverage(digest):
    eps = build_day_episodes(digest, EpisodeParams())
    cam = {round(e.start - T0.timestamp()): e for e in eps if e.source == "camera"}
    rad = {round(e.start - T0.timestamp()): e for e in eps if e.source == "radar"}
    labels = {
        cam[100].id: {"verdict": "person"},
        cam[300].id: {"verdict": "no_person"},
        rad[400].id: {"verdict": "no_person"},
    }
    summary = summarize(eps, labels, coverage=coverage_for_sources([digest]), timezone_name="UTC")
    camera, radar = summary["per_source"]["camera"], summary["per_source"]["radar"]
    assert camera["precision"] == 0.5 and radar["precision"] == 0.0
    # One true group (episode A); camera caught it, radar caught it via the paired radar episode.
    assert camera["mutual_recall"] == 1.0 and radar["mutual_recall"] == 1.0
    assert camera["false_per_hour"] == pytest.approx(1 / (599 / 3600), rel=1e-3)
    assert summary["agreement_matrix"]["camera_only"]["no_person"] == 1
    assert summary["latency_seconds"] == [0.0]
    assert "상호 재현율" in summary["note"]


def test_evaluate_and_sweep_against_labeled_reference(digest):
    params = EpisodeParams()
    reference = build_day_episodes(digest, params)
    cam = {round(e.start - T0.timestamp()): e for e in reference if e.source == "camera"}
    labels = {cam[100].id: {"verdict": "person"}, cam[300].id: {"verdict": "no_person"}}
    strict = build_camera_episodes(digest, EpisodeParams(min_confidence=0.6))
    score = evaluate_against_reference(strict, [e for e in reference if e.source == "camera"], labels, tolerance=3.0)
    assert score == {"candidates": 1, "tp": 1, "fp": 0, "precision": 1.0, "recall": 1.0, "true_reference": 1, "labeled_reference": 2}
    rows = sweep_camera([digest], params, reference, labels, confidences=(0.2, 0.6), frames=(2,))
    assert [(r["min_confidence"], r["fp"]) for r in rows] == [(0.2, 1), (0.6, 0)]


def test_store_caches_digest_and_invalidates_on_new_shards(tmp_path: Path):
    paths = AnalysisPaths(tmp_path / "analysis")
    site = SiteConfig(name="s", nas_host="nas.example.com", nas_username="u", nas_password="p", nas_folder="/home/x", timezone_name="UTC")
    day_dir = paths.cache_dir("s", "h", "2026-09-03")
    _write_day(day_dir, _synthetic_day())
    store = AnalysisStore(site, paths)
    assert store.days()[0]["day"] == "2026-09-03"
    digest = store.digest("h", "2026-09-03")
    assert digest is not None and paths.digest_path("s", "h", "2026-09-03").is_file()
    assert load_digest(paths.digest_path("s", "h", "2026-09-03"))["counts"] == digest["counts"]
    eps = store.episodes("h", "2026-09-03", EpisodeParams())
    assert store.find_episode(eps[0].id, EpisodeParams()) is not None
    # Appending a shard changes the fingerprint → digest rebuilt.
    with (day_dir / "events-20260903-1000.jsonl").open("w", encoding="utf-8") as fp:
        fp.write(json.dumps(_person_batch(3700, "front", 0.9)) + "\n")
    store2 = AnalysisStore(site, paths)
    assert store2.digest("h", "2026-09-03")["counts"]["detection_batch"] == 16


def test_sites_store_roundtrip_env_import_and_redaction(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "SYNOLOGY_NAS_HOST=nas.example.com\nSYNOLOGY_NAS_PORT=45222\nSYNOLOGY_NAS_ID=user\nSYNOLOGY_NAS_PW=secret\nSYNOLOGY_NAS_FOLDER=/home/share\n",
        encoding="utf-8",
    )
    site = site_from_env(env, name="siteA", label="현장 A")
    assert site.configured and site.nas_port == 45222 and site.remote_raw_dir == "/home/share/raw"
    public = site.to_public_dict()
    assert "nas_password" not in public and public["has_password"] is True
    assert "secret" not in repr(site)
    store = SitesStore(tmp_path / "sites.json")
    store.upsert(site)
    # Blank password on update keeps the stored one.
    store.upsert(SiteConfig(name="siteA", nas_host="nas2.example.com", nas_username="user", nas_folder="/home/share"))
    reloaded = store.get("siteA")
    assert reloaded.nas_host == "nas2.example.com" and reloaded.nas_password == "secret"
    with pytest.raises(ValueError):
        SiteConfig(name="bad name")
    with pytest.raises(ValueError):
        SiteConfig(name="x", nas_host="sftp://nas:22/path")
    assert store.remove("siteA") and store.load() == []


def test_merge_intervals_and_digest_roundtrip(tmp_path: Path, digest):
    assert merge_intervals([[0, 5], [4, 8], [10, 11]]) == [[0, 8], [10, 11]]
    assert merge_intervals([[0, 5], [6, 8]], gap=1.0) == [[0, 8]]
    save_digest(tmp_path / "d.json", digest)
    assert load_digest(tmp_path / "d.json")["radar_source"] == "ld2410_sample"
    assert load_digest(tmp_path / "missing.json") is None


def test_analyze_package_never_imports_safety_modules():
    """Run in a fresh interpreter: the suite itself imports the engine/UI, which would mask a leak."""
    import subprocess
    import sys

    script = (
        "import importlib, sys\n"
        "for name in ('towersightai.analyze.server', 'towersightai.analyze.store', 'towersightai.analyze.metrics', 'towersightai.cli.analyze'):\n"
        "    importlib.import_module(name)\n"
        "loaded = {n for n in sys.modules if n.startswith('towersightai.')}\n"
        "forbidden = {'towersightai.process.engine', 'towersightai.state_machine.core', 'towersightai.plc.adapter', 'towersightai.ui.pyqt_app', 'towersightai.ui.model'}\n"
        "leak = loaded & forbidden\n"
        "print(sorted(leak))\n"
        "raise SystemExit(1 if leak else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def _plate_day() -> list[dict]:
    """One camera episode inside a vehicle session whose plate vote succeeded, plus a second
    session that ended 미인식."""
    records = [
        _record("application_started", 0, {"camera_ids": ["front"]}),
        _record("ai_started", 1, {"task_id": "process_monitoring", "camera_ids": ["front"], "simulated": False}),
        _record("vehicle_entered", 90, {"camera_id": "opposite_side", "confidence": 0.8, "managed": True}, event_id="veh-1"),
    ]
    records[-1]["vehicle_session_id"] = "sess-1"
    for index, (plate, accepted, reason) in enumerate(
        [("12가3456", True, ""), ("12가3456", True, ""), ("99라9999", False, "above_entry_line"), ("", False, "no_plate_detected")]
    ):
        item = _record("plate_attempt", 92 + index, {"plate_number": plate, "confidence": 0.8, "camera_id": "front", "accepted": accepted, "reason": reason})
        item["vehicle_session_id"] = "sess-1"
        records.append(item)
    plate = _record("plate_recognized", 96, {"plate_number": "12가3456", "confidence": 0.82, "camera_id": "front", "simulated": False, "recognized": True, "reads": 2, "reason": "vote"}, event_id="plate-1")
    plate["vehicle_session_id"] = "sess-1"
    records.append(plate)
    records.append(
        _record(
            "media_artifact_created",
            96,
            {"kind": "plate_crop", "camera_id": "front", "relative_path": "media/images/plate-crop.jpg", "size_bytes": 5, "sha256": "p", "captured_at": _iso(96), "related_event_id": "plate-1", "metadata": {"source": "lpr"}},
        )
    )
    for i in range(10):  # camera person episode inside the session
        records.append(_person_batch(100 + i * 0.1, "front", 0.8))
    end = _record("vehicle_session_ended", 130, {"reason": "parking_started"})
    end["vehicle_session_id"] = "sess-1"
    records.append(end)
    # Second session: entry happened, plate never read.
    second = _record("vehicle_entered", 500, {"camera_id": "opposite_side", "managed": True}, event_id="veh-2")
    second["vehicle_session_id"] = "sess-2"
    records.append(second)
    miss = _record("plate_recognized", 520, {"plate_number": "미인식", "confidence": None, "camera_id": "front", "simulated": False, "recognized": False, "reads": 0, "reason": "aborted:uncertainty"}, event_id="plate-2")
    miss["vehicle_session_id"] = "sess-2"
    records.append(miss)
    for i in range(10):
        records.append(_person_batch(510 + i * 0.1, "front", 0.8))
    end2 = _record("vehicle_session_ended", 540, {"reason": "uncertainty"})
    end2["vehicle_session_id"] = "sess-2"
    records.append(end2)
    return records


def test_digest_collects_plate_outcomes_and_attempts():
    digest = build_digest(_plate_day(), site="s", host="h", day="2026-09-03", timezone_name="UTC")
    assert [p["plate"] for p in digest["plates"]] == ["12가3456", "미인식"]
    assert digest["plates"][0]["recognized"] is True and digest["plates"][0]["reads"] == 2
    assert digest["plates"][1]["recognized"] is False
    assert [(a["plate"], a["accepted"], a["reason"]) for a in digest["plate_attempts"]] == [
        ("12가3456", True, ""),
        ("12가3456", True, ""),
        ("99라9999", False, "above_entry_line"),
        ("", False, "no_plate_detected"),
    ]
    # Only a recognized plate is promoted onto the vehicle session.
    sessions = {v["id"]: v for v in digest["vehicle_sessions"]}
    assert sessions["sess-1"]["plate"] == "12가3456" and sessions["sess-2"]["plate"] is None


def test_digest_treats_legacy_plate_rows_without_the_flag_as_recognized():
    records = [_record("plate_recognized", 10, {"plate_number": "34나5678", "confidence": 0.7, "camera_id": "front"})]
    digest = build_digest(records, site="s", host="h", day="2026-09-03")
    assert digest["plates"][0]["recognized"] is True
    miss = [_record("plate_recognized", 10, {"plate_number": "미인식", "camera_id": "front"})]
    assert build_digest(miss, site="s", host="h", day="2026-09-03")["plates"][0]["recognized"] is False


def test_episodes_carry_the_plate_of_their_vehicle_session():
    digest = build_digest(_plate_day(), site="s", host="h", day="2026-09-03", timezone_name="UTC")
    episodes = build_day_episodes(digest, EpisodeParams())
    by_start = {round(e.start - T0.timestamp()): e for e in episodes if e.source == "camera"}
    first = by_start[100]
    assert first.plate == "12가3456" and first.plate_recognized is True
    assert first.plate_confidence == 0.82 and first.plate_reads == 2 and first.plate_source == "vehicle_session"
    assert [a["reason"] for a in first.plate_attempts] == ["", "", "above_entry_line", "no_plate_detected"]
    assert any(m["kind"] == "plate_crop" for m in first.media)  # plate media joins the bundle
    second = by_start[510]
    assert second.plate == "미인식" and second.plate_recognized is False and second.plate_source == "vehicle_session"
    assert not second.plate_attempts


def test_episode_without_a_vehicle_has_no_plate(digest):
    episodes = build_day_episodes(digest, EpisodeParams())
    assert all(not ep.plate for ep in episodes)


def _radar_window_day() -> list[dict]:
    """A radar window where the cameras stayed silent, then one second where a camera agreed."""
    records = [
        _record("application_started", 0, {"camera_ids": ["front"]}),
        _record("ai_started", 1, {"task_id": "process_monitoring", "camera_ids": ["front"], "simulated": False}),
        _record("radar_window_started", 100, {"radar_window_id": "rw-1", "started_at": _iso(100), "confirm_seconds": 3.0}, event_id="rw-start"),
    ]
    for s in range(100, 112):
        records.append(_ld(s, 2))
        agreed = s == 108
        records.append(
            _record(
                "radar_sample",
                s,
                {
                    "radar_window_id": "rw-1",
                    "sampled_at": _iso(s),
                    "camera_person_present": agreed,
                    "cameras": {
                        "front": {
                            "person_present": agreed,
                            "last_person_detected_at": _iso(s) if agreed else None,
                            "detections": [{"confidence": 0.44, "label": "person"}] if agreed else [],
                        }
                    },
                    "ld2410": {"status": "fresh", "target_status": 2, "detection_distance_cm": 210, "moving_energy": 5, "motionless_energy": 50},
                    "safety_effect": "raw_only",
                },
            )
        )
    for s in range(112, 122):
        records.append(_ld(s, 0))
    records.append(
        _record(
            "radar_window_closed",
            111,
            {"radar_window_id": "rw-1", "started_at": _iso(100), "ended_at": _iso(111), "duration_seconds": 11.0, "reason": "cleared", "present_sample_count": 12},
            event_id="rw-close",
        )
    )
    return records


def test_digest_summarizes_camera_state_during_a_radar_window():
    digest = build_digest(_radar_window_day(), site="s", host="h", day="2026-09-03", timezone_name="UTC")
    window = digest["radar_windows"][0]
    assert window["camera_samples"] == 12
    assert window["camera_present_samples"] == 1
    assert window["camera_agreement"] == round(1 / 12, 3)
    assert window["samples"][0]["cams"] == {"front": False}
    agreed = [item for item in window["samples"] if item["camera_present"]]
    assert agreed and agreed[0]["conf"] == {"front": 0.44}
    assert agreed[0]["ld"]["ts"] == 2 and agreed[0]["ld"]["d"] == 210


def test_radar_episode_carries_the_camera_comparison():
    digest = build_digest(_radar_window_day(), site="s", host="h", day="2026-09-03", timezone_name="UTC")
    episodes = build_day_episodes(digest, EpisodeParams())
    radar = [e for e in episodes if e.source == "radar"]
    assert len(radar) == 1
    check = radar[0].camera_check
    assert check["samples"] == 12 and check["camera_present_samples"] == 1
    assert check["agreement"] == round(1 / 12, 3)
    assert check["by_camera"] == {"front": 1}
    assert check["max_confidence"] == 0.44
    assert radar[0].radar_window_ids == ["rw-1"]


def test_episode_without_radar_samples_has_no_camera_check(digest):
    assert all(not ep.camera_check for ep in build_day_episodes(digest, EpisodeParams()))
