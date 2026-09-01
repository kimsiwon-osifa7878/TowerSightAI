from __future__ import annotations

import hashlib
import json
import os
import posixpath
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from towersightai.config.settings import RawStorageConfig


MANIFEST_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FileArtifact:
    relative_path: str
    size_bytes: int
    sha256: str
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }


def build_day_manifest(day_dir: Path, day: str) -> dict[str, Any]:
    artifacts: list[FileArtifact] = []
    for path in sorted(day_dir.rglob("*")):
        if not path.is_file() or _excluded(path, day_dir):
            continue
        relative = path.relative_to(day_dir).as_posix()
        validate_relative_path(relative)
        artifacts.append(
            FileArtifact(
                relative_path=relative,
                size_bytes=path.stat().st_size,
                sha256=sha256_path(path),
                media_type=media_type(path),
            )
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "day": day,
        "source_host": socket.gethostname(),
        "files": [artifact.to_dict() for artifact in artifacts],
    }


def write_manifest_atomic(day_dir: Path, manifest: Mapping[str, Any]) -> Path:
    path = day_dir / "manifest.json"
    payload = canonical_manifest_bytes(manifest)
    temporary = day_dir / f".manifest.json.{os.getpid()}.part"
    with temporary.open("wb") as fp:
        fp.write(payload)
        fp.flush()
        os.fsync(fp.fileno())
    temporary.replace(path)
    return path


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


class ParamikoManifestUploader:
    def __init__(self, config: RawStorageConfig) -> None:
        self.config = config

    def upload_day(self, day: str, day_dir: Path, manifest: Mapping[str, Any]) -> str:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - packaging guard.
            raise RuntimeError("paramiko is required for Synology SFTP upload") from exc

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.config.known_hosts_path.is_file():
            client.load_host_keys(str(self.config.known_hosts_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.config.nas_host,
                port=self.config.nas_port,
                username=self.config.nas_username,
                password=self.config.nas_password,
                allow_agent=False,
                look_for_keys=False,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
            )
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(60.0)
                remote_dir = posixpath.join(self.config.nas_folder.rstrip("/"), "raw", day)
                mkdirs(sftp, remote_dir)
                remote_manifest = _read_remote_json(sftp, posixpath.join(remote_dir, "manifest.json"))
                remote_hashes = {
                    str(item.get("relative_path")): str(item.get("sha256"))
                    for item in remote_manifest.get("files", ())
                    if isinstance(item, dict)
                }
                for item in manifest.get("files", ()):
                    if not isinstance(item, dict):
                        continue
                    relative = str(item["relative_path"])
                    validate_relative_path(relative)
                    if remote_hashes.get(relative) == item.get("sha256"):
                        continue
                    local_path = day_dir / PurePosixPath(relative)
                    remote_path = posixpath.join(remote_dir, relative)
                    mkdirs(sftp, posixpath.dirname(remote_path))
                    upload_atomic_verified(sftp, local_path, remote_path, str(item["sha256"]))
                manifest_bytes = canonical_manifest_bytes(manifest)
                remote_manifest_path = posixpath.join(remote_dir, "manifest.json")
                _upload_bytes_atomic(sftp, manifest_bytes, remote_manifest_path)
                remote_bytes = read_remote_bytes(sftp, remote_manifest_path)
                if hashlib.sha256(remote_bytes).hexdigest() != hashlib.sha256(manifest_bytes).hexdigest():
                    raise OSError("remote manifest SHA-256 verification failed")
                return remote_dir
        finally:
            client.close()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(path: Path, day_dir: Path) -> bool:
    relative = path.relative_to(day_dir)
    if path.name in {"manifest.json", ".nas-upload.json"} or path.name.endswith(".part"):
        return True
    if ".buffer" in relative.parts or path.name == ".writer.lock":
        return True
    return path.name.endswith(".sealed.jsonl") or bool(path.name.startswith(".manifest.json."))


def media_type(path: Path) -> str:
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith(".jsonl.gz"):
        return "application/x-ndjson+gzip"
    if path.suffix.lower() == ".jsonl":
        return "application/x-ndjson"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if path.suffix.lower() == ".mkv":
        return "video/x-matroska"
    if path.suffix.lower() == ".mp4":
        return "video/mp4"
    if path.suffix.lower() == ".json":
        return "application/json"
    return "application/octet-stream"


def validate_relative_path(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe archive relative path: {relative}")


def mkdirs(sftp: Any, path: str) -> None:
    current = "/" if path.startswith("/") else ""
    for part in PurePosixPath(path).parts:
        if part in {"/", ".", ""}:
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def upload_atomic_verified(sftp: Any, local_path: Path, remote_path: str, digest: str) -> None:
    part_path = remote_path + ".part"
    with local_path.open("rb") as source, sftp.file(part_path, "wb") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
        target.flush()
    remote_digest = _remote_sha256(sftp, part_path)
    if remote_digest != digest:
        raise OSError("remote SHA-256 verification failed")
    sftp.posix_rename(part_path, remote_path)


def _upload_bytes_atomic(sftp: Any, payload: bytes, remote_path: str) -> None:
    part_path = remote_path + ".part"
    with sftp.file(part_path, "wb") as target:
        target.write(payload)
        target.flush()
    sftp.posix_rename(part_path, remote_path)


def read_remote_bytes(sftp: Any, path: str) -> bytes:
    payload = bytearray()
    with sftp.file(path, "rb") as remote:
        while chunk := remote.read(1024 * 1024):
            payload.extend(chunk)
    return bytes(payload)


def _remote_sha256(sftp: Any, path: str) -> str:
    digest = hashlib.sha256()
    with sftp.file(path, "rb") as remote:
        while chunk := remote.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_remote_json(sftp: Any, path: str) -> dict[str, Any]:
    try:
        payload = json.loads(read_remote_bytes(sftp, path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
