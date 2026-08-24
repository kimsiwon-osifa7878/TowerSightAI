from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from towersightai.storage.archive import build_day_manifest, manifest_sha256, write_manifest_atomic
from towersightai.storage.hourly_writer import HourlyJsonlWriter


def test_hourly_writer_rotates_and_gzips_closed_shard(tmp_path: Path):
    writer = HourlyJsonlWriter(tmp_path, "UTC", shard_minutes=60)
    writer.append({"value": 1}, recorded_at=datetime(2026, 8, 24, 1, 59, tzinfo=timezone.utc))
    writer.append({"value": 2}, recorded_at=datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc))
    writer.wait_for_compression()

    compressed = tmp_path / "2026-08-24/events-20260824-0100.jsonl.gz"
    assert compressed.is_file()
    with gzip.open(compressed, "rt", encoding="utf-8") as fp:
        assert json.loads(fp.readline()) == {"value": 1}
    assert (tmp_path / "2026-08-24/events-20260824-0200.jsonl").is_file()
    writer.close()


def test_restarting_with_compressed_hour_uses_sequence_shard(tmp_path: Path):
    day_dir = tmp_path / "2026-08-24"
    day_dir.mkdir()
    (day_dir / "events-20260824-0100.jsonl.gz").write_bytes(b"already sealed")
    writer = HourlyJsonlWriter(tmp_path, "UTC")
    path = writer.append({"value": 2}, recorded_at=datetime(2026, 8, 24, 1, 10, tzinfo=timezone.utc))
    assert path.name == "events-20260824-0100-01.jsonl"
    writer.close()


def test_late_previous_hour_record_does_not_seal_newer_hour(tmp_path: Path):
    writer = HourlyJsonlWriter(tmp_path, "UTC")
    newer = writer.append({"value": "new"}, recorded_at=datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc))
    late = writer.append({"value": "late"}, recorded_at=datetime(2026, 8, 24, 1, 59, tzinfo=timezone.utc))

    assert newer.is_file()
    assert late.is_file()
    assert not newer.with_suffix(".jsonl.gz").exists()
    writer.close()


def test_manifest_lists_json_and_media_with_stable_sha(tmp_path: Path):
    day_dir = tmp_path / "2026-08-24"
    (day_dir / "media/images").mkdir(parents=True)
    (day_dir / "events-20260824-0100.jsonl").write_text("{}\n", encoding="utf-8")
    (day_dir / "media/images/a.jpg").write_bytes(b"jpeg")
    manifest = build_day_manifest(day_dir, "2026-08-24")
    write_manifest_atomic(day_dir, manifest)

    assert manifest["schema_version"] == 2
    assert [item["relative_path"] for item in manifest["files"]] == [
        "events-20260824-0100.jsonl",
        "media/images/a.jpg",
    ]
    assert len(manifest_sha256(manifest)) == 64
