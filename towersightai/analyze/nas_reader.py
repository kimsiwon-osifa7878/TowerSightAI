"""Read-only NAS access: list archived days and mirror them into the local cache.

Supports both remote layouts — the legacy ``raw/YYYY-MM-DD/`` (host taken from the manifest)
and the per-host ``raw/<source_host>/YYYY-MM-DD/`` layout. Every downloaded file is verified
against the manifest SHA-256 when a manifest exists; days without one are marked ``partial``.
The SFTP client is injectable so tests never open a socket.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from towersightai.analyze.config import AnalysisPaths, SiteConfig
from towersightai.analyze.loader import is_shard_name
from towersightai.storage.archive import validate_relative_path

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYNC_MARKER = ".analysis-sync.json"
UNKNOWN_HOST = "unknown-host"


@dataclass
class RemoteDay:
    host: str
    day: str
    remote_dir: str
    layout: str  # "legacy" | "per_host"
    has_manifest: bool = False
    files: int = 0
    bytes: int = 0
    event_files: int = 0
    media_files: int = 0
    cached_events: bool = False
    cached_media: bool = False
    partial: bool = False
    manifest_sha256: str = ""
    changed: bool = False  # remote manifest differs from the cached copy (or partial day grew)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class SyncProgress:
    running: bool = False
    site: str = ""
    current: str = ""
    done: int = 0
    total: int = 0
    bytes_done: int = 0
    errors: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


SftpFactory = Callable[[SiteConfig], Any]


def paramiko_sftp(site: SiteConfig) -> Any:
    """Open a strict-host-key SFTP session (same policy as the archive uploader)."""
    import paramiko

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if site.known_hosts_path.is_file():
        client.load_host_keys(str(site.known_hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        hostname=site.nas_host,
        port=site.nas_port,
        username=site.nas_username,
        password=site.nas_password,
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
    )
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(120.0)
    return _ClosingSftp(client, sftp)


class _ClosingSftp:
    def __init__(self, client: Any, sftp: Any) -> None:
        self._client = client
        self._sftp = sftp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sftp, name)

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()


class NasReader:
    def __init__(self, site: SiteConfig, paths: AnalysisPaths, *, sftp_factory: SftpFactory | None = None) -> None:
        if not site.configured:
            raise ValueError(f"site {site.name!r} is missing NAS settings: {', '.join(site.missing)}")
        self.site = site
        self.paths = paths
        self._factory = sftp_factory or paramiko_sftp

    # ------------------------------------------------------------------ listing

    def list_days(self) -> list[RemoteDay]:
        sftp = self._factory(self.site)
        try:
            return self._list_days(sftp)
        finally:
            sftp.close()

    def _list_days(self, sftp: Any) -> list[RemoteDay]:
        root = self.site.remote_raw_dir
        days: list[RemoteDay] = []
        for entry in _listdir(sftp, root):
            if not stat.S_ISDIR(entry.st_mode):
                continue
            if _DAY.match(entry.filename):
                days.append(self._describe_day(sftp, posixpath.join(root, entry.filename), entry.filename, host=None, layout="legacy"))
            else:
                host_dir = posixpath.join(root, entry.filename)
                for sub in _listdir(sftp, host_dir):
                    if stat.S_ISDIR(sub.st_mode) and _DAY.match(sub.filename):
                        days.append(self._describe_day(sftp, posixpath.join(host_dir, sub.filename), sub.filename, host=entry.filename, layout="per_host"))
        days.sort(key=lambda item: (item.day, item.host))
        return days

    def _describe_day(self, sftp: Any, remote_dir: str, day: str, *, host: str | None, layout: str) -> RemoteDay:
        manifest_bytes = _read_remote_bytes(sftp, posixpath.join(remote_dir, "manifest.json"))
        manifest = _decode_json(manifest_bytes)
        files = [item for item in manifest.get("files", ()) if isinstance(item, dict)] if manifest else []
        # Attribution: host folder > manifest source_host > the site's default (legacy field
        # uploader without a manifest) > unknown.
        resolved_host = host or self.site.day_owners.get(day) or str(manifest.get("source_host") or "") or self.site.default_host or UNKNOWN_HOST
        info = RemoteDay(host=_safe_host(resolved_host), day=day, remote_dir=remote_dir, layout=layout, has_manifest=bool(manifest))
        if files:
            info.files = len(files)
            info.bytes = sum(int(item.get("size_bytes") or 0) for item in files)
            info.event_files = sum(1 for item in files if is_shard_name(str(item.get("relative_path") or "")))
            info.media_files = sum(1 for item in files if str(item.get("relative_path") or "").startswith("media/"))
        else:
            info.partial = True
            names = [entry.filename for entry in _listdir(sftp, remote_dir) if stat.S_ISREG(entry.st_mode)]
            info.event_files = sum(1 for name in names if is_shard_name(name))
            info.files = len(names)
        local_dir = self.paths.cache_dir(self.site.name, info.host, day)
        marker = _read_local_json(local_dir / SYNC_MARKER)
        info.cached_events = bool(marker.get("events_complete"))
        info.cached_media = bool(marker.get("media_complete"))
        if manifest_bytes:
            info.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            info.changed = info.manifest_sha256 != str(marker.get("manifest_sha256") or "")
        else:
            # No manifest (upload in progress): compare the shard size total we saw last time.
            sizes = sum(int(entry.st_size or 0) for entry in _listdir(sftp, remote_dir) if stat.S_ISREG(entry.st_mode) and is_shard_name(entry.filename))
            info.changed = sizes != int(marker.get("partial_shard_bytes") or -1)
            info.bytes = sizes
        return info

    def plan_update(self) -> list[RemoteDay]:
        """Days worth syncing now: never cached, cached before the remote manifest changed, or
        still-partial days that grew. Cheap: one manifest read per remote day."""
        return [info for info in self.list_days() if not info.cached_events or info.changed]

    # ------------------------------------------------------------------ sync

    def sync_days(
        self,
        days: Iterable[RemoteDay],
        *,
        media: bool = False,
        progress: SyncProgress | None = None,
        stop: threading.Event | None = None,
    ) -> SyncProgress:
        progress = progress or SyncProgress()
        progress.running = True
        progress.site = self.site.name
        targets = list(days)
        progress.total = len(targets)
        sftp = self._factory(self.site)
        try:
            for info in targets:
                if stop is not None and stop.is_set():
                    progress.cancelled = True
                    break
                progress.current = f"{info.host}/{info.day}"
                try:
                    self._sync_day(sftp, info, media=media, progress=progress, stop=stop)
                    progress.finished.append(progress.current)
                except Exception as exc:  # noqa: BLE001 - one day must not block the others.
                    progress.errors.append(f"{progress.current}: {type(exc).__name__}: {exc}"[:200])
                progress.done += 1
        finally:
            sftp.close()
            progress.running = False
            progress.current = ""
        return progress

    def _sync_day(self, sftp: Any, info: RemoteDay, *, media: bool, progress: SyncProgress, stop: threading.Event | None) -> None:
        local_dir = self.paths.cache_dir(self.site.name, info.host, info.day)
        local_dir.mkdir(parents=True, exist_ok=True)
        manifest_bytes = _read_remote_bytes(sftp, posixpath.join(info.remote_dir, "manifest.json"))
        manifest = _decode_json(manifest_bytes)
        entries: list[tuple[str, str | None, int]] = []
        if manifest:
            (local_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            for item in manifest.get("files", ()):
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("relative_path") or "")
                validate_relative_path(relative)
                entries.append((relative, str(item.get("sha256") or "") or None, int(item.get("size_bytes") or 0)))
        else:
            for entry in _listdir(sftp, info.remote_dir):
                if stat.S_ISREG(entry.st_mode) and is_shard_name(entry.filename):
                    entries.append((entry.filename, None, int(entry.st_size or 0)))
        wanted = [item for item in entries if is_shard_name(posixpath.basename(item[0])) or (media and item[0].startswith("media/"))]
        for relative, digest, size in wanted:
            if stop is not None and stop.is_set():
                progress.cancelled = True
                return
            target = local_dir / Path(*relative.split("/"))
            if target.is_file() and (digest is None and target.stat().st_size == size or digest is not None and _sha256(target) == digest):
                continue
            _download_verified(sftp, posixpath.join(info.remote_dir, relative), target, digest)
            progress.bytes_done += size
        marker = _read_local_json(local_dir / SYNC_MARKER)
        marker.update(
            {
                "host": info.host,
                "day": info.day,
                "remote_dir": info.remote_dir,
                "layout": info.layout,
                "has_manifest": bool(manifest),
                "partial": not manifest,
                "events_complete": not progress.cancelled,
                "media_complete": bool(marker.get("media_complete")) or (media and not progress.cancelled),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest() if manifest_bytes else "",
                "partial_shard_bytes": sum(size for relative, _digest, size in entries if is_shard_name(posixpath.basename(relative))) if not manifest else None,
            }
        )
        (local_dir / SYNC_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def ensure_media(self, host: str, day: str, relative: str) -> Path:
        """Fetch one media file on demand (dashboard episode view) and verify it if possible."""
        validate_relative_path(relative)
        local_dir = self.paths.cache_dir(self.site.name, host, day)
        target = local_dir / Path(*relative.split("/"))
        if target.is_file():
            return target
        marker = _read_local_json(local_dir / SYNC_MARKER)
        remote_dir = str(marker.get("remote_dir") or "")
        if not remote_dir:
            raise FileNotFoundError(f"day {host}/{day} is not cached; sync it first")
        manifest = _read_local_json(local_dir / "manifest.json")
        digest = next(
            (str(item.get("sha256")) for item in manifest.get("files", ()) if isinstance(item, dict) and item.get("relative_path") == relative),
            None,
        )
        sftp = self._factory(self.site)
        try:
            _download_verified(sftp, posixpath.join(remote_dir, relative), target, digest)
        finally:
            sftp.close()
        return target


def local_days(paths: AnalysisPaths, site: str) -> list[dict[str, Any]]:
    """Cached (host, day) pairs with their sync markers — works without any NAS access."""
    root = paths.site_root(site) / "cache"
    result: list[dict[str, Any]] = []
    if not root.is_dir():
        return result
    for host_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for day_dir in sorted(path for path in host_dir.iterdir() if path.is_dir() and _DAY.match(path.name)):
            marker = _read_local_json(day_dir / SYNC_MARKER)
            shards = sum(1 for path in day_dir.iterdir() if path.is_file() and is_shard_name(path.name))
            if not shards:
                continue
            result.append(
                {
                    "host": host_dir.name,
                    "day": day_dir.name,
                    "event_files": shards,
                    "layout": marker.get("layout") or "legacy",
                    "has_manifest": bool(marker.get("has_manifest", (day_dir / "manifest.json").is_file())),
                    "partial": bool(marker.get("partial")),
                    "events_complete": bool(marker.get("events_complete", True)),
                    "media_complete": bool(marker.get("media_complete")),
                    "remote_dir": marker.get("remote_dir"),
                }
            )
    return result


def _listdir(sftp: Any, path: str) -> list[Any]:
    try:
        return list(sftp.listdir_attr(path))
    except OSError:
        return []


def _read_remote_bytes(sftp: Any, path: str) -> bytes:
    try:
        with sftp.open(path, "rb") as fp:
            return bytes(fp.read())
    except OSError:
        return b""


def _decode_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_remote_json(sftp: Any, path: str) -> dict[str, Any]:
    return _decode_json(_read_remote_bytes(sftp, path))


def _read_local_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _download_verified(sftp: Any, remote_path: str, target: Path, digest: str | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.part")
    hasher = hashlib.sha256()
    with sftp.open(remote_path, "rb") as source, temporary.open("wb") as sink:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
            sink.write(chunk)
    if digest and hasher.hexdigest() != digest:
        temporary.unlink(missing_ok=True)
        raise OSError(f"SHA-256 mismatch for {remote_path}")
    temporary.replace(target)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_host(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-.")
    return cleaned[:128] or UNKNOWN_HOST


__all__ = ["NasReader", "RemoteDay", "SYNC_MARKER", "SyncProgress", "local_days", "paramiko_sftp"]
