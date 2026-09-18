"""Share calibration measurements between machines through the NAS.

Why this exists
---------------
Lens intrinsics are measured with a printed checkerboard held in front of the camera, which is
only practical on the bench. The site device cannot do it — and without intrinsics the
`지면 기준점` page has nothing to undistort clicks with, so it refuses to solve. Rather than
re-measuring on site, the bench publishes its measurement to the NAS and the site device pulls it.

What travels
------------
Only the *result* JSONs: ``intrinsics/<camera>.json`` and ``ground/<camera>.json``. The capture
sessions (``intrinsics/sessions/**``, hundreds of PNG frames) stay local — they are evidence for
re-running a measurement, not something another machine needs.

Remote layout, per originating host so two machines never overwrite each other:

    <SYNOLOGY_NAS_FOLDER>/calibration/<source_host>/<kind>/<camera_id>.json

Rules this module keeps
-----------------------
* Every upload and download is SHA-256 verified, and files are published atomically
  (``.part`` → rename), the same as the raw-data archive.
* ``reviewed`` and ``safe_to_operate`` are never rewritten. A file that arrives from another
  machine is still an unreviewed measurement.
* A downloaded measurement keeps its original ``source_host``. The console compares that against
  the local hostname and shows it as borrowed, so a bench measurement can never masquerade as
  this camera's own.
* Sharing a measurement is not authorization. Nothing here touches the safety gate or the PLC.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from towersightai.config.settings import RawStorageConfig
from towersightai.storage.archive import (
    mkdirs,
    read_remote_bytes,
    sha256_path,
    upload_atomic_verified,
)
from towersightai.storage.connection_test import connect_ssh_client

CALIBRATION_ROOT = "calibration"
#: Sub-folder name → the "kind" recorded in each file.
KINDS: dict[str, str] = {"intrinsics": "camera_intrinsics", "ground": "camera_ground_pose"}
KIND_LABELS = {"intrinsics": "렌즈 내부 파라미터", "ground": "지면 기준점"}

ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class CalibrationEntry:
    """One shareable measurement file, local or remote."""

    kind: str  # "intrinsics" | "ground"
    camera_id: str
    source_host: str
    measured_at: str
    quality: str
    size_bytes: int
    sha256: str
    local_path: Path | None = None
    remote_path: str = ""

    @property
    def label(self) -> str:
        return f"{KIND_LABELS.get(self.kind, self.kind)} · {self.camera_id}"

    def describe(self, *, local_host: str = "") -> str:
        origin = self.source_host or "알 수 없는 장비"
        if local_host and self.source_host == local_host:
            origin = f"{origin} (이 장비)"
        stamp = self.measured_at[:19].replace("T", " ") if self.measured_at else "시각 미기록"
        return f"{self.label} · {self.quality or '등급 미기록'} · {stamp} · {origin}"


@dataclass(frozen=True)
class CalibrationShareResult:
    ok: bool
    summary: str
    entries: tuple[CalibrationEntry, ...] = field(default_factory=tuple)
    remote_dir: str = ""
    elapsed_seconds: float = 0.0
    error: str = ""
    #: Publishing or fetching a measurement never authorizes parking-machine operation.
    safe_to_operate: bool = False


class SftpSession(Protocol):
    def listdir_attr(self, path: str) -> Sequence[Any]: ...
    def file(self, path: str, mode: str) -> Any: ...
    def stat(self, path: str) -> Any: ...
    def rename(self, old: str, new: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def remove(self, path: str) -> None: ...


SftpFactory = Callable[[RawStorageConfig], Any]


def paramiko_sftp(config: RawStorageConfig) -> Any:
    """Strict-host-key SFTP, the same policy the archive uploader uses."""
    client = connect_ssh_client(config)
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(120.0)

    class _ClosingSftp:
        def __init__(self, client: Any, sftp: Any) -> None:
            self._client, self._sftp = client, sftp

        def __getattr__(self, name: str) -> Any:
            return getattr(self._sftp, name)

        def close(self) -> None:
            try:
                self._sftp.close()
            finally:
                self._client.close()

    return _ClosingSftp(client, sftp)


def remote_calibration_dir(config: RawStorageConfig, host: str = "", kind: str = "") -> str:
    parts = [CALIBRATION_ROOT]
    for part in (host, kind):
        if part:
            if part in {".", ".."} or "/" in part:
                raise ValueError(f"invalid calibration path segment: {part!r}")
            parts.append(part)
    return posixpath.join(config.nas_folder.rstrip("/"), *parts)


def _entry_from_payload(
    payload: dict[str, Any], kind: str, *, size_bytes: int, digest: str, local_path: Path | None = None,
    remote_path: str = "",
) -> CalibrationEntry:
    return CalibrationEntry(
        kind=kind,
        camera_id=str(payload.get("camera_id", "")),
        source_host=str(payload.get("source_host", "")),
        measured_at=str(payload.get("measured_at", "")),
        quality=str(payload.get("quality", "")),
        size_bytes=size_bytes,
        sha256=digest,
        local_path=local_path,
        remote_path=remote_path,
    )


def local_entries(root: Path) -> tuple[CalibrationEntry, ...]:
    """Result JSONs under ``<root>/{intrinsics,ground}/``. Capture sessions are skipped."""
    found: list[CalibrationEntry] = []
    for kind, expected in KINDS.items():
        folder = Path(root) / kind
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("kind") != expected:
                continue
            found.append(
                _entry_from_payload(
                    payload, kind, size_bytes=path.stat().st_size, digest=sha256_path(path), local_path=path
                )
            )
    return tuple(found)


def publish_calibration(
    config: RawStorageConfig,
    root: Path,
    *,
    entries: Sequence[CalibrationEntry] | None = None,
    sftp_factory: SftpFactory | None = None,
    host: str = "",
    progress: ProgressCallback | None = None,
) -> CalibrationShareResult:
    """Upload this machine's calibration results to the NAS."""
    started = time.monotonic()
    host = host or socket.gethostname()
    selected = tuple(entries) if entries is not None else local_entries(root)
    if not selected:
        return CalibrationShareResult(False, "공유할 측정 파일이 없습니다. 먼저 카메라 캘리브레이션을 실행하세요.")

    factory = sftp_factory or paramiko_sftp
    remote_dir = remote_calibration_dir(config, host)
    try:
        sftp = factory(config)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator verbatim
        return CalibrationShareResult(False, "NAS 접속 실패", error=str(exc))

    published: list[CalibrationEntry] = []
    try:
        total = len(selected)
        for index, entry in enumerate(selected, start=1):
            if entry.local_path is None or not entry.local_path.is_file():
                continue
            if progress is not None:
                progress(index, total, entry.label)
            kind_dir = remote_calibration_dir(config, host, entry.kind)
            mkdirs(sftp, kind_dir)
            remote_path = posixpath.join(kind_dir, f"{entry.camera_id}.json")
            digest = sha256_path(entry.local_path)
            upload_atomic_verified(sftp, entry.local_path, remote_path, digest)
            if hashlib.sha256(read_remote_bytes(sftp, remote_path)).hexdigest() != digest:
                raise OSError(f"업로드 검증 실패: {entry.label}")
            published.append(
                CalibrationEntry(**{**entry.__dict__, "remote_path": remote_path, "sha256": digest})
            )
    except Exception as exc:  # noqa: BLE001
        return CalibrationShareResult(
            False,
            f"{len(published)}/{len(selected)}개 공유 후 실패",
            tuple(published),
            remote_dir,
            time.monotonic() - started,
            str(exc),
        )
    finally:
        close = getattr(sftp, "close", None)
        if callable(close):
            close()

    return CalibrationShareResult(
        True,
        f"{len(published)}개 측정 파일을 NAS에 올렸습니다 ({host})",
        tuple(published),
        remote_dir,
        time.monotonic() - started,
    )


def list_remote_calibration(
    config: RawStorageConfig, *, sftp_factory: SftpFactory | None = None
) -> tuple[CalibrationEntry, ...]:
    """Everything available on the NAS, newest measurement first."""
    factory = sftp_factory or paramiko_sftp
    root = remote_calibration_dir(config)
    try:
        sftp = factory(config)
    except Exception:  # noqa: BLE001 - an unreachable NAS simply means nothing to offer
        return ()
    found: list[CalibrationEntry] = []
    try:
        for host_entry in _listdir(sftp, root):
            if not _is_dir(host_entry):
                continue
            host = host_entry.filename
            for kind in KINDS:
                kind_dir = posixpath.join(root, host, kind)
                for item in _listdir(sftp, kind_dir):
                    if _is_dir(item) or not item.filename.endswith(".json"):
                        continue
                    remote_path = posixpath.join(kind_dir, item.filename)
                    try:
                        payload_bytes = read_remote_bytes(sftp, remote_path)
                        payload = json.loads(payload_bytes.decode("utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if payload.get("kind") != KINDS[kind]:
                        continue
                    found.append(
                        _entry_from_payload(
                            payload,
                            kind,
                            size_bytes=len(payload_bytes),
                            digest=hashlib.sha256(payload_bytes).hexdigest(),
                            remote_path=remote_path,
                        )
                    )
    finally:
        close = getattr(sftp, "close", None)
        if callable(close):
            close()
    return tuple(sorted(found, key=lambda entry: entry.measured_at, reverse=True))


def fetch_calibration(
    config: RawStorageConfig,
    entries: Sequence[CalibrationEntry],
    destination_root: Path,
    *,
    sftp_factory: SftpFactory | None = None,
    progress: ProgressCallback | None = None,
) -> CalibrationShareResult:
    """Download the chosen measurements into ``<destination_root>/<kind>/<camera>.json``.

    The file is written exactly as measured — including its original ``source_host`` — so the
    console can tell the operator it came from another machine.
    """
    started = time.monotonic()
    if not entries:
        return CalibrationShareResult(False, "가져올 측정 파일을 고르세요.")

    factory = sftp_factory or paramiko_sftp
    try:
        sftp = factory(config)
    except Exception as exc:  # noqa: BLE001
        return CalibrationShareResult(False, "NAS 접속 실패", error=str(exc))

    fetched: list[CalibrationEntry] = []
    try:
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            if progress is not None:
                progress(index, total, entry.label)
            payload_bytes = read_remote_bytes(sftp, entry.remote_path)
            digest = hashlib.sha256(payload_bytes).hexdigest()
            if entry.sha256 and digest != entry.sha256:
                raise OSError(f"내려받기 검증 실패: {entry.label}")
            payload = json.loads(payload_bytes.decode("utf-8"))
            if payload.get("kind") != KINDS.get(entry.kind):
                raise OSError(f"측정 파일 형식이 아닙니다: {entry.label}")
            # A shared measurement stays unreviewed and unauthorized, whatever the file claims.
            payload["reviewed"] = False
            payload["safe_to_operate"] = False
            target_dir = Path(destination_root) / entry.kind
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{entry.camera_id}.json"
            temporary = target.with_suffix(".json.part")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            fetched.append(CalibrationEntry(**{**entry.__dict__, "local_path": target, "sha256": digest}))
    except Exception as exc:  # noqa: BLE001
        return CalibrationShareResult(
            False,
            f"{len(fetched)}/{len(entries)}개 가져온 뒤 실패",
            tuple(fetched),
            "",
            time.monotonic() - started,
            str(exc),
        )
    finally:
        close = getattr(sftp, "close", None)
        if callable(close):
            close()

    return CalibrationShareResult(
        True,
        f"{len(fetched)}개 측정 파일을 가져왔습니다. 다른 장비 측정값은 '빌려 씀'으로 표시됩니다.",
        tuple(fetched),
        "",
        time.monotonic() - started,
    )


def _listdir(sftp: Any, path: str) -> list[Any]:
    try:
        return list(sftp.listdir_attr(path))
    except OSError:
        return []


def _is_dir(entry: Any) -> bool:
    return stat.S_ISDIR(getattr(entry, "st_mode", 0) or 0)
