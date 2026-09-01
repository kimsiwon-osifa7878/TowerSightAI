"""Operator-triggered Synology NAS connectivity check.

The check writes a small payload into ``<SYNOLOGY_NAS_FOLDER>/connectiontest/<run>/`` using the
same strict-host-key SFTP, atomic ``.part`` publication, and SHA-256 verification as the
operational archive path. It is diagnostic only: a passing result proves that the NAS write path
works, never that the parking machine is safe to operate. It must not change safety state,
calibration state, or PLC output.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from towersightai.config.settings import RawStorageConfig
from towersightai.storage.archive import (
    mkdirs,
    media_type,
    read_remote_bytes,
    sha256_path,
    upload_atomic_verified,
    validate_relative_path,
)


CONNECTION_TEST_ROOT = "connectiontest"
SUMMARY_FILENAME = "connection-test.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class UploadedArtifact:
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


@dataclass(frozen=True)
class NasConnectionTestResult:
    ok: bool
    summary: str
    remote_dir: str = ""
    artifacts: tuple[UploadedArtifact, ...] = field(default_factory=tuple)
    elapsed_seconds: float = 0.0
    error: str = ""
    # A NAS check never authorizes parking-machine operation.
    safe_to_operate: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(artifact.size_bytes for artifact in self.artifacts)


class ConnectionTestUploader(Protocol):
    def upload(self, local_dir: Path, remote_dir: str) -> tuple[UploadedArtifact, ...]:
        ...


class ParamikoConnectionTestUploader:
    """Upload one local directory with the archive path's strict-host-key SFTP rules."""

    def __init__(self, config: RawStorageConfig) -> None:
        self.config = config

    def upload(self, local_dir: Path, remote_dir: str) -> tuple[UploadedArtifact, ...]:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - packaging guard.
            raise RuntimeError("paramiko is required for the Synology NAS check") from exc

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
                mkdirs(sftp, remote_dir)
                artifacts: list[UploadedArtifact] = []
                for path in sorted(local_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(local_dir).as_posix()
                    validate_relative_path(relative)
                    digest = sha256_path(path)
                    remote_path = posixpath.join(remote_dir, relative)
                    mkdirs(sftp, posixpath.dirname(remote_path))
                    upload_atomic_verified(sftp, path, remote_path, digest)
                    if hashlib.sha256(read_remote_bytes(sftp, remote_path)).hexdigest() != digest:
                        raise OSError(f"remote read-back verification failed: {relative}")
                    artifacts.append(
                        UploadedArtifact(
                            relative_path=relative,
                            size_bytes=path.stat().st_size,
                            sha256=digest,
                            media_type=media_type(path),
                        )
                    )
                return tuple(artifacts)
        finally:
            client.close()


def connection_test_run_name(started_at: datetime) -> str:
    return started_at.astimezone(timezone.utc).strftime("check-%Y%m%d-%H%M%SZ")


def remote_connection_test_dir(config: RawStorageConfig, run_name: str) -> str:
    return posixpath.join(config.nas_folder.rstrip("/"), CONNECTION_TEST_ROOT, run_name)


def build_connection_test_payload(
    payload_dir: Path,
    *,
    started_at: datetime,
    metadata: Mapping[str, Any] | None = None,
    video_path: Path | None = None,
) -> Path:
    """Write the summary file, and copy the optional clip, into ``payload_dir``."""
    payload_dir.mkdir(parents=True, exist_ok=True)
    video_name = ""
    if video_path is not None and video_path.is_file():
        video_name = video_path.name
        if video_path.resolve(strict=False) != (payload_dir / video_name).resolve(strict=False):
            shutil.copy2(video_path, payload_dir / video_name)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "TowerSightAI NAS write check. Diagnostic only; not operational data.",
        "safe_to_operate": False,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "source_host": socket.gethostname(),
        "video_file": video_name,
        **{str(key): value for key, value in (metadata or {}).items()},
    }
    path = payload_dir / SUMMARY_FILENAME
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run_nas_connection_test(
    config: RawStorageConfig,
    *,
    work_dir: Path,
    metadata: Mapping[str, Any] | None = None,
    video_path: Path | None = None,
    uploader: ConnectionTestUploader | None = None,
    now: datetime | None = None,
) -> NasConnectionTestResult:
    """Write a test payload to the NAS and verify it. Never raises; failures are reported."""
    started_at = now or datetime.now(timezone.utc)
    missing = [
        name
        for name, value in (
            ("SYNOLOGY_NAS_HOST", config.nas_host),
            ("SYNOLOGY_NAS_ID", config.nas_username),
            ("SYNOLOGY_NAS_PW", config.nas_password),
            ("SYNOLOGY_NAS_FOLDER", config.nas_folder),
        )
        if not value
    ]
    if missing:
        return NasConnectionTestResult(
            ok=False,
            summary="NAS 설정이 없습니다.",
            error="설정 누락: " + ", ".join(missing),
        )

    run_name = connection_test_run_name(started_at)
    remote_dir = remote_connection_test_dir(config, run_name)
    payload_dir = Path(work_dir) / run_name
    started = time.monotonic()
    try:
        build_connection_test_payload(
            payload_dir,
            started_at=started_at,
            metadata=metadata,
            video_path=video_path,
        )
        active_uploader = uploader or ParamikoConnectionTestUploader(config)
        artifacts = active_uploader.upload(payload_dir, remote_dir)
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary reports instead of crashing the UI.
        return NasConnectionTestResult(
            ok=False,
            summary="NAS 저장 실패",
            remote_dir=remote_dir,
            elapsed_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = time.monotonic() - started
    total_bytes = sum(artifact.size_bytes for artifact in artifacts)
    return NasConnectionTestResult(
        ok=True,
        summary=f"NAS 저장 확인: {len(artifacts)}개 파일 {total_bytes:,}B, {elapsed:.1f}s",
        remote_dir=remote_dir,
        artifacts=artifacts,
        elapsed_seconds=elapsed,
    )
