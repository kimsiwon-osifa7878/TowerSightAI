"""In-process analysis store: cached day digests + memoized episode sets per parameter set."""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from towersightai.analyze.config import AnalysisPaths, SiteConfig
from towersightai.analyze.digest import build_digest, load_digest, save_digest
from towersightai.analyze.episodes import Episode, EpisodeParams, build_day_episodes
from towersightai.analyze.loader import load_day_records
from towersightai.analyze.nas_reader import local_days


class AnalysisStore:
    def __init__(self, site: SiteConfig, paths: AnalysisPaths) -> None:
        self.site = site
        self.paths = paths
        self._digests: dict[tuple[str, str], dict[str, Any]] = {}
        self._episodes: dict[tuple[str, str, str], list[Episode]] = {}
        self._lock = threading.RLock()
        self.on_progress: Any = None  # callable(stage, detail, done, total) for the dashboard loading bar

    # ------------------------------------------------------------------ days

    def days(self) -> list[dict[str, Any]]:
        return local_days(self.paths, self.site.name)

    def days_in_range(self, start: str | None, end: str | None, *, host: str | None = None, hosts: set[str] | None = None) -> list[dict[str, Any]]:
        """``host`` = one host; ``hosts`` = allowed set (None = every host)."""
        result = []
        for item in self.days():
            if start and item["day"] < start:
                continue
            if end and item["day"] > end:
                continue
            if host and item["host"] != host:
                continue
            if hosts is not None and item["host"] not in hosts:
                continue
            result.append(item)
        return result

    def hosts(self) -> list[str]:
        return sorted({item["host"] for item in self.days()})

    # ------------------------------------------------------------------ digests

    def digest(self, host: str, day: str, *, rebuild: bool = False) -> dict[str, Any] | None:
        key = (host, day)
        with self._lock:
            if not rebuild and key in self._digests:
                return self._digests[key]
        path = self.paths.digest_path(self.site.name, host, day)
        day_dir = self.paths.cache_dir(self.site.name, host, day)
        digest = None if rebuild else load_digest(path)
        if digest is not None and not _digest_is_current(digest, day_dir):
            digest = None
        if digest is None:
            if not day_dir.is_dir():
                return None
            self._report("digest", f"{host}/{day} 원본 읽는 중")
            records = load_day_records(day_dir)
            if not records:
                return None
            self._report("digest", f"{host}/{day} 다이제스트 계산 ({len(records):,} 레코드)")
            digest = build_digest(records, site=self.site.name, host=host, day=day, timezone_name=self.site.timezone_name)
            digest["source_fingerprint"] = _fingerprint(day_dir)
            save_digest(path, digest)
        with self._lock:
            self._digests[key] = digest
            for cached in [k for k in self._episodes if k[0] == host and k[1] == day]:
                self._episodes.pop(cached, None)
        return digest

    def _report(self, stage: str, detail: str, done: int | None = None, total: int | None = None) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(stage, detail, done, total)
            except Exception:  # noqa: BLE001 - progress reporting must never break analysis.
                pass

    def warm(self) -> int:
        """Build every missing/stale digest up front (server start) so first page loads are fast."""
        days = self.days()
        built = 0
        for index, item in enumerate(days, start=1):
            self._report("warm", f"{item['host']}/{item['day']} 준비", index, len(days))
            if self.digest(item["host"], item["day"]) is not None:
                built += 1
        self._report("idle", "", None, None)
        return built

    def invalidate(self, host: str | None = None, day: str | None = None) -> None:
        with self._lock:
            for key in list(self._digests):
                if (host is None or key[0] == host) and (day is None or key[1] == day):
                    self._digests.pop(key, None)
            for key in list(self._episodes):
                if (host is None or key[0] == host) and (day is None or key[1] == day):
                    self._episodes.pop(key, None)

    # ------------------------------------------------------------------ episodes

    def episodes(self, host: str, day: str, params: EpisodeParams) -> list[Episode]:
        key = (host, day, params.key)
        with self._lock:
            cached = self._episodes.get(key)
        if cached is not None:
            return cached
        digest = self.digest(host, day)
        if digest is None:
            return []
        episodes = build_day_episodes(digest, params)
        with self._lock:
            self._episodes[key] = episodes
        return episodes

    def episodes_in_range(self, start: str | None, end: str | None, params: EpisodeParams, *, host: str | None = None, hosts: set[str] | None = None) -> list[Episode]:
        result: list[Episode] = []
        days = self.days_in_range(start, end, host=host, hosts=hosts)
        for index, item in enumerate(days, start=1):
            self._report("episodes", f"{item['host']}/{item['day']} 에피소드 재구성", index, len(days))
            result.extend(self.episodes(item["host"], item["day"], params))
        self._report("idle", "", None, None)
        result.sort(key=lambda ep: ep.start)
        return result

    def digests_in_range(self, start: str | None, end: str | None, *, host: str | None = None, hosts: set[str] | None = None) -> list[dict[str, Any]]:
        result = []
        for item in self.days_in_range(start, end, host=host, hosts=hosts):
            digest = self.digest(item["host"], item["day"])
            if digest is not None:
                result.append(digest)
        return result

    def find_episode(self, episode_id: str, params: EpisodeParams, *, host: str | None = None, day: str | None = None) -> Episode | None:
        candidates = [d for d in self.days() if (host is None or d["host"] == host) and (day is None or d["day"] == day)]
        for item in candidates:
            for ep in self.episodes(item["host"], item["day"], params):
                if ep.id == episode_id:
                    return ep
        return None


    def move_day(self, day: str, from_host: str, to_host: str) -> bool:
        """Re-attribute one cached day to another host (cache + digest + remuxed clips)."""
        import shutil

        moved = False
        pairs = (
            (self.paths.cache_dir(self.site.name, from_host, day), self.paths.cache_dir(self.site.name, to_host, day)),
            (self.paths.digest_path(self.site.name, from_host, day), self.paths.digest_path(self.site.name, to_host, day)),
            (self.paths.mp4_cache_dir(self.site.name, from_host, day), self.paths.mp4_cache_dir(self.site.name, to_host, day)),
        )
        for source, target in pairs:
            if not source.exists():
                continue
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved = True
        marker = self.paths.cache_dir(self.site.name, to_host, day) / ".analysis-sync.json"
        if marker.is_file():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                data["host"] = to_host
                marker.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass
        self.invalidate(from_host, day)
        self.invalidate(to_host, day)
        return moved

    def delete_host_cache(self, host: str) -> int:
        """Remove every locally cached day, digest and remuxed clip of one host (labels are kept)."""
        import shutil

        removed = 0
        for root in (self.paths.site_root(self.site.name) / "cache", self.paths.site_root(self.site.name) / "index", self.paths.site_root(self.site.name) / "mp4"):
            target = root / host
            if target.is_dir():
                shutil.rmtree(target)
                removed += 1
        self.invalidate(host)
        return removed


def _fingerprint(day_dir: Path) -> str:
    parts = []
    for path in sorted(day_dir.iterdir()):
        if path.is_file() and path.name.startswith("events-"):
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def _digest_is_current(digest: Mapping[str, Any], day_dir: Path) -> bool:
    if not day_dir.is_dir():
        return True
    return digest.get("source_fingerprint") == _fingerprint(day_dir)


def parse_day(value: str | None) -> str | None:
    if not value:
        return None
    return date.fromisoformat(value).isoformat()


__all__ = ["AnalysisStore", "parse_day"]
