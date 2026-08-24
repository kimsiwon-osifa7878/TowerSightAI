from __future__ import annotations

import fcntl
import gzip
import json
import os
import re
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


_ACTIVE_SHARD = re.compile(r"events-(\d{8})-(\d{4})(?:-(\d{2}))?\.jsonl$")


class WriterBusyError(RuntimeError):
    pass


class HourlyJsonlWriter:
    """Append JSONL into bounded time shards and gzip immutable shards safely."""

    def __init__(self, root: Path, timezone_name: str, *, shard_minutes: int = 60) -> None:
        self.root = Path(root)
        self.timezone = ZoneInfo(timezone_name)
        self.shard_minutes = shard_minutes
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raw-jsonl-gzip")
        self._futures: list[Future[Path]] = []
        self._compression_sources: set[Path] = set()
        self._writer_lock_fp = None
        self._last_fsync_monotonic = 0.0

    def append(self, record: Mapping[str, Any], *, recorded_at: datetime, durable: bool = False) -> Path:
        if recorded_at.tzinfo is None:
            raise ValueError("Raw record timestamp must be timezone-aware.")
        import time

        local = recorded_at.astimezone(self.timezone)
        with self._lock:
            self._ensure_writer_lock()
            self._recover_older_shards(local)
            event_path = self._active_path(local)
            event_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            with event_path.open("a", encoding="utf-8") as fp:
                fp.write(line)
                fp.flush()
                now = time.monotonic()
                if durable or now - self._last_fsync_monotonic >= 1.0:
                    os.fsync(fp.fileno())
                    self._last_fsync_monotonic = now
            return event_path

    def finalize_day(self, day: date, *, require_exclusive: bool = False) -> tuple[Path, ...]:
        with self._lock:
            temporary_lock = False
            if require_exclusive and self._writer_lock_fp is None:
                self._ensure_writer_lock(nonblocking=True)
                temporary_lock = True
            day_dir = self.root / day.isoformat()
            sealed: list[Path] = []
            if day_dir.is_dir():
                for path in sorted(day_dir.glob("events-*.jsonl")):
                    if _ACTIVE_SHARD.fullmatch(path.name):
                        sealed.append(self._seal(path))
                for path in sorted(day_dir.glob("events-*.sealed.jsonl")):
                    self._submit_compression(path)
            self.wait_for_compression()
            if temporary_lock:
                self._release_writer_lock()
            return tuple(path.with_name(path.name.removesuffix(".sealed.jsonl") + ".jsonl.gz") for path in sealed)

    def finalize_before(self, day: date) -> None:
        with self._lock:
            if not self.root.is_dir():
                return
            for day_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
                try:
                    candidate = date.fromisoformat(day_dir.name)
                except ValueError:
                    continue
                if candidate < day:
                    self.finalize_day(candidate)
            self.wait_for_compression()

    def wait_for_compression(self) -> None:
        pending, self._futures = self._futures, []
        for future in pending:
            source = getattr(future, "_raw_source", None)
            try:
                future.result()
            finally:
                if source is not None:
                    self._compression_sources.discard(source)

    def close(self) -> None:
        with self._lock:
            self.wait_for_compression()
            self._release_writer_lock()
            self._executor.shutdown(wait=True)

    def _active_path(self, local: datetime) -> Path:
        minute = (local.minute // self.shard_minutes) * self.shard_minutes
        shard_start = local.replace(minute=minute, second=0, microsecond=0)
        day_dir = self.root / local.date().isoformat()
        base = day_dir / f"events-{shard_start:%Y%m%d-%H%M}.jsonl"
        if base.exists() or not base.with_suffix(".jsonl.gz").exists():
            return base
        sequence = 1
        while True:
            candidate = day_dir / f"events-{shard_start:%Y%m%d-%H%M}-{sequence:02d}.jsonl"
            if candidate.exists() or not candidate.with_suffix(".jsonl.gz").exists():
                return candidate
            sequence += 1

    def _recover_older_shards(self, local: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        active = self._active_path(local)
        active_key = _shard_key(active)
        for day_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            for path in sorted(day_dir.glob("events-*.sealed.jsonl")):
                self._submit_compression(path)
            for path in sorted(day_dir.glob("events-*.jsonl")):
                if path != active and _ACTIVE_SHARD.fullmatch(path.name) and _shard_key(path) < active_key:
                    self._submit_compression(self._seal(path))

    def _seal(self, path: Path) -> Path:
        if path.name.endswith(".sealed.jsonl"):
            return path
        sealed = path.with_name(path.name.removesuffix(".jsonl") + ".sealed.jsonl")
        if path.exists():
            path.replace(sealed)
        self._submit_compression(sealed)
        return sealed

    def _submit_compression(self, sealed: Path) -> None:
        final = sealed.with_name(sealed.name.removesuffix(".sealed.jsonl") + ".jsonl.gz")
        if final.is_file():
            sealed.unlink(missing_ok=True)
            return
        if sealed in self._compression_sources:
            return
        future = self._executor.submit(_compress_atomic, sealed, final)
        future._raw_source = sealed  # type: ignore[attr-defined]
        self._futures.append(future)
        self._compression_sources.add(sealed)

    def _ensure_writer_lock(self, *, nonblocking: bool = True) -> None:
        if self._writer_lock_fp is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        fp = (self.root / ".writer.lock").open("a+")
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(fp.fileno(), flags)
        except BlockingIOError as exc:
            fp.close()
            raise WriterBusyError("raw-data writer is active") from exc
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        self._writer_lock_fp = fp

    def _release_writer_lock(self) -> None:
        if self._writer_lock_fp is None:
            return
        try:
            fcntl.flock(self._writer_lock_fp.fileno(), fcntl.LOCK_UN)
        finally:
            self._writer_lock_fp.close()
            self._writer_lock_fp = None


def _compress_atomic(source: Path, final: Path) -> Path:
    if not source.is_file():
        return final
    temporary = final.with_name(f".{final.name}.{os.getpid()}.part")
    with source.open("rb") as src, temporary.open("wb") as raw_out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
            shutil.copyfileobj(src, gz, length=1024 * 1024)
        raw_out.flush()
        os.fsync(raw_out.fileno())
    with gzip.open(temporary, "rb") as check:
        while check.read(1024 * 1024):
            pass
    temporary.replace(final)
    source.unlink(missing_ok=True)
    return final


def _shard_key(path: Path) -> tuple[str, str]:
    match = _ACTIVE_SHARD.fullmatch(path.name)
    if match is None:
        raise ValueError(f"invalid raw shard name: {path.name}")
    return match.group(1), match.group(2)
