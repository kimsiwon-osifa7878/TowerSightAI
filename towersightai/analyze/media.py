"""Evidence media helpers: safe path resolution and MKV→MP4 remux for browser playback."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from towersightai.analyze.config import AnalysisPaths
from towersightai.storage.archive import validate_relative_path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mkv", ".mp4"}


def resolve_media(paths: AnalysisPaths, site: str, host: str, day: str, relative: str) -> Path:
    validate_relative_path(relative)
    root = paths.cache_dir(site, host, day).resolve()
    candidate = (root / Path(*relative.split("/"))).resolve()
    if root not in candidate.parents:
        raise ValueError("media path escapes the day cache")
    return candidate


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ensure_mp4(paths: AnalysisPaths, site: str, host: str, day: str, source: Path, *, ffmpeg: str | None = None) -> Path:
    """Remux an H.264 MKV clip into a faststart MP4 (no re-encode). Cached by content path."""
    if source.suffix.lower() == ".mp4":
        return source
    binary = ffmpeg or ffmpeg_path()
    if binary is None:
        raise RuntimeError("ffmpeg_missing")
    cache_dir = paths.mp4_cache_dir(site, host, day)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
    target = cache_dir / f"{source.stem}-{key}.mp4"
    if target.is_file() and target.stat().st_size > 0:
        return target
    temporary = target.with_name(f".{target.name}.part.mp4")
    subprocess.run(
        [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", "0:v:0", "-c", "copy", "-an", "-movflags", "+faststart", str(temporary)],
        check=True,
        timeout=120,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    temporary.replace(target)
    return target


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".mkv":
        return "video/x-matroska"
    if suffix == ".json":
        return "application/json"
    return "application/octet-stream"


__all__ = ["IMAGE_SUFFIXES", "VIDEO_SUFFIXES", "content_type_for", "ensure_mp4", "ffmpeg_path", "resolve_media"]
