"""Operator-triggered file relay through the Synology NAS.

The site edge device can be reached remotely, but the remote session cannot carry files. This
module lets the operator pick local files and drop them into one fixed NAS folder
(``<SYNOLOGY_NAS_FOLDER>/transfer/``) so they can be collected from the NAS side.

It reuses the archive path's strict-host-key SFTP, atomic ``.part`` publication, and SHA-256
read-back verification. It is a convenience/relay feature only: a successful transfer proves that
bytes reached the NAS, never that the parking machine is safe to operate. It must not change safety
state, calibration state, or PLC output.
"""

from __future__ import annotations

import hashlib
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from towersightai.config.settings import RawStorageConfig
from towersightai.storage.archive import (
    media_type,
    mkdirs,
    read_remote_bytes,
    sha256_path,
    upload_atomic_verified,
)
from towersightai.storage.connection_test import UploadedArtifact, connect_ssh_client


FILE_TRANSFER_ROOT = "transfer"
ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class NasFileTransferResult:
    ok: bool
    summary: str
    remote_dir: str = ""
    artifacts: tuple[UploadedArtifact, ...] = field(default_factory=tuple)
    elapsed_seconds: float = 0.0
    error: str = ""
    # A file relay never authorizes parking-machine operation.
    safe_to_operate: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(artifact.size_bytes for artifact in self.artifacts)


class FileTransferUploader(Protocol):
    def upload(
        self,
        files: Sequence[Path],
        remote_dir: str,
        progress: ProgressCallback | None = None,
    ) -> tuple[UploadedArtifact, ...]:
        ...


class ParamikoFileTransferUploader:
    """Upload individual files into one NAS folder with the archive path's SFTP rules."""

    def __init__(self, config: RawStorageConfig) -> None:
        self.config = config

    def upload(
        self,
        files: Sequence[Path],
        remote_dir: str,
        progress: ProgressCallback | None = None,
    ) -> tuple[UploadedArtifact, ...]:
        client = connect_ssh_client(self.config)
        try:
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(120.0)
                mkdirs(sftp, remote_dir)
                artifacts: list[UploadedArtifact] = []
                total = len(files)
                for index, path in enumerate(files, start=1):
                    if progress is not None:
                        progress(index, total, path.name)
                    digest = sha256_path(path)
                    remote_path = posixpath.join(remote_dir, path.name)
                    upload_atomic_verified(sftp, path, remote_path, digest)
                    if hashlib.sha256(read_remote_bytes(sftp, remote_path)).hexdigest() != digest:
                        raise OSError(f"remote read-back verification failed: {path.name}")
                    artifacts.append(
                        UploadedArtifact(
                            relative_path=path.name,
                            size_bytes=path.stat().st_size,
                            sha256=digest,
                            media_type=media_type(path),
                        )
                    )
                return tuple(artifacts)
        finally:
            client.close()


def remote_file_transfer_dir(config: RawStorageConfig) -> str:
    return posixpath.join(config.nas_folder.rstrip("/"), FILE_TRANSFER_ROOT)


def validate_transfer_files(files: Sequence[Path]) -> str:
    """Return an empty string when every file is a readable regular file with a unique name."""
    if not files:
        return "보낼 파일이 없습니다."
    names: set[str] = set()
    for path in files:
        if not path.is_file():
            return f"파일이 없습니다: {path}"
        name = path.name
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return f"잘못된 파일 이름입니다: {path}"
        if name in names:
            return f"같은 이름의 파일이 두 번 선택되었습니다: {name}"
        names.add(name)
    return ""


def upload_files_to_nas(
    config: RawStorageConfig,
    files: Sequence[Path],
    *,
    uploader: FileTransferUploader | None = None,
    progress: ProgressCallback | None = None,
) -> NasFileTransferResult:
    """Send the given files into the NAS transfer folder. Never raises; failures are reported."""
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
        return NasFileTransferResult(
            ok=False,
            summary="NAS 설정이 없습니다.",
            error="설정 누락: " + ", ".join(missing),
        )

    paths = tuple(Path(item) for item in files)
    problem = validate_transfer_files(paths)
    if problem:
        return NasFileTransferResult(ok=False, summary="파일 전송 불가", error=problem)

    remote_dir = remote_file_transfer_dir(config)
    started = time.monotonic()
    try:
        active_uploader = uploader or ParamikoFileTransferUploader(config)
        artifacts = active_uploader.upload(paths, remote_dir, progress)
    except Exception as exc:  # noqa: BLE001 - relay boundary reports instead of crashing the UI.
        return NasFileTransferResult(
            ok=False,
            summary="NAS 파일 전송 실패",
            remote_dir=remote_dir,
            elapsed_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = time.monotonic() - started
    total_bytes = sum(artifact.size_bytes for artifact in artifacts)
    return NasFileTransferResult(
        ok=True,
        summary=f"NAS 전송 완료: {len(artifacts)}개 파일 {total_bytes:,}B, {elapsed:.1f}s",
        remote_dir=remote_dir,
        artifacts=artifacts,
        elapsed_seconds=elapsed,
    )


def describe_result(result: NasFileTransferResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "summary": result.summary,
        "remote_dir": result.remote_dir,
        "files": [artifact.to_dict() for artifact in result.artifacts],
        "error": result.error,
        "safe_to_operate": result.safe_to_operate,
    }
