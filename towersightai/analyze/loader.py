"""Read cached raw-day directories (schema v2 JSONL shards) into plain records."""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SHARD = re.compile(r"^events(?:-\d{8}-\d{4}(?:-\d+)?)?\.jsonl(?:\.gz)?$")  # legacy single events.jsonl too


def is_shard_name(name: str) -> bool:
    return bool(_SHARD.match(name))


def list_shards(day_dir: Path) -> list[Path]:
    if not day_dir.is_dir():
        return []
    return sorted(path for path in day_dir.iterdir() if path.is_file() and is_shard_name(path.name))


def iter_shard(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fp:  # type: ignore[operator]
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and "event_type" in record and "recorded_at" in record:
                    yield record
    except (OSError, EOFError, gzip.BadGzipFile):
        return


def load_day_records(day_dir: Path) -> list[dict[str, Any]]:
    """All records of one day, sorted by ``recorded_at`` (shards can interleave after restarts)."""
    records: list[dict[str, Any]] = []
    for shard in list_shards(day_dir):
        records.extend(iter_shard(shard))
    records.sort(key=lambda record: str(record.get("recorded_at") or ""))
    return records


def parse_ts(value: Any) -> float | None:
    """ISO-8601 → epoch seconds (UTC). Naive values are treated as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def find_records(day_dir: Path, *, event_ids: set[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """On-demand lookup of raw records by ``event_id`` (used by the '원본 보기' panel)."""
    found: list[dict[str, Any]] = []
    if not event_ids:
        return found
    for shard in list_shards(day_dir):
        for record in iter_shard(shard):
            if record.get("event_id") in event_ids:
                found.append(record)
                if len(found) >= limit:
                    return found
    return found


__all__ = ["find_records", "is_shard_name", "iter_shard", "list_shards", "load_day_records", "parse_ts"]
