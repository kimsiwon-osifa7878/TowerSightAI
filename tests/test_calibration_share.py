"""Sharing calibration measurements between machines through the NAS.

The checkerboard can only be held in front of a camera on the bench, so the site device has to
receive that measurement rather than make it. These tests drive the whole round trip against a
fake SFTP server — no network, no credentials.
"""

import json
import posixpath
import stat
from pathlib import Path

import pytest

from towersightai.calibration.share import (
    CalibrationEntry,
    fetch_calibration,
    list_remote_calibration,
    local_entries,
    publish_calibration,
    remote_calibration_dir,
)
from towersightai.config.settings import RawStorageConfig


class _Attr:
    def __init__(self, filename: str, directory: bool, size: int = 0) -> None:
        self.filename = filename
        self.st_mode = (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)
        self.st_size = size


class _RemoteFile:
    def __init__(self, store: dict, path: str, mode: str) -> None:
        self._store, self._path, self._mode = store, path, mode
        self._buffer = bytearray()
        if "r" in mode:
            if path not in store:
                raise OSError(f"no such file: {path}")
            self._buffer = bytearray(store[path])
        self._offset = 0

    def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def flush(self) -> None:
        # paramiko's SFTPFile exposes flush(); upload_atomic_verified calls it before rename.
        if "w" in self._mode:
            self._store[self._path] = bytes(self._buffer)

    def read(self, size: int = -1) -> bytes:
        if size < 0 or self._offset + size > len(self._buffer):
            size = len(self._buffer) - self._offset
        chunk = bytes(self._buffer[self._offset : self._offset + size])
        self._offset += size
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        if "w" in self._mode:
            self._store[self._path] = bytes(self._buffer)


class FakeSftp:
    """Just enough SFTP for the share path: files, dirs, atomic rename, read-back."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.closed = False

    # -- API used by the module ------------------------------------------------
    def file(self, path: str, mode: str):
        return _RemoteFile(self.files, path, mode)

    def stat(self, path: str):
        if path in self.files:
            return _Attr(posixpath.basename(path), False, len(self.files[path]))
        if path in self.dirs:
            return _Attr(posixpath.basename(path), True)
        raise OSError(f"no such path: {path}")

    def mkdir(self, path: str) -> None:
        self.dirs.add(path.rstrip("/"))

    def rename(self, old: str, new: str) -> None:
        self.files[new] = self.files.pop(old)

    def posix_rename(self, old: str, new: str) -> None:
        # The archive uploader prefers POSIX rename so publication overwrites atomically.
        self.rename(old, new)

    def remove(self, path: str) -> None:
        self.files.pop(path, None)

    def listdir_attr(self, path: str):
        prefix = path.rstrip("/") + "/"
        names: dict[str, bool] = {}
        for candidate in list(self.files) + sorted(self.dirs):
            if not candidate.startswith(prefix):
                continue
            rest = candidate[len(prefix) :]
            if not rest:
                continue
            head, _, tail = rest.partition("/")
            names[head] = names.get(head, False) or bool(tail) or candidate in self.dirs
        if not names and path.rstrip("/") not in self.dirs:
            raise OSError(f"no such directory: {path}")
        return [_Attr(name, directory) for name, directory in sorted(names.items())]

    def close(self) -> None:
        self.closed = True


CONFIG = RawStorageConfig(
    enabled=True,
    local_dir=Path("artifacts/raw"),
    nas_host="nas.example",
    nas_port=45222,
    nas_username="tester",
    nas_password="secret",
    nas_folder="/home/site",
    known_hosts_path=Path("~/.ssh/known_hosts"),
)


def _intrinsics_payload(camera: str, host: str) -> dict:
    return {
        "kind": "camera_intrinsics",
        "camera_id": camera,
        "image_width": 1920,
        "image_height": 1080,
        "rotation_degrees": 0,
        "checkerboard": {"columns": 10, "rows": 7, "square_mm": 25.0},
        "sample_count": 13,
        "rms_reprojection_error_px": 0.25,
        "quality": "good",
        "camera_matrix": [[1356.4, 0.0, 960.0], [0.0, 1360.1, 540.0], [0.0, 0.0, 1.0]],
        "distortion_coefficients": [-0.29, 0.0, 0.0, 0.0, 0.05],
        "per_view_errors_px": [0.25],
        "pose_keys": ["center"],
        "measured_at": "2026-09-16T08:14:07+00:00",
        "source_host": host,
        "reviewed": False,
        "safe_to_operate": False,
    }


def _write_local(root: Path, kind: str, camera: str, payload: dict) -> Path:
    folder = root / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{camera}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_local_entries_take_results_and_skip_capture_sessions(tmp_path: Path):
    root = tmp_path / "calibration"
    _write_local(root, "intrinsics", "opposite_side", _intrinsics_payload("opposite_side", "bench"))
    # Capture frames and their per-session copy must not travel: they are evidence, not config.
    session = root / "intrinsics" / "sessions" / "opposite_side-20260916Z"
    session.mkdir(parents=True)
    (session / "intrinsics.json").write_text(
        json.dumps(_intrinsics_payload("opposite_side", "bench")), encoding="utf-8"
    )
    (session / "pose-01-center.png").write_bytes(b"not really a png")
    # A stray non-calibration JSON is ignored rather than uploaded.
    (root / "intrinsics" / "notes.json").write_text('{"kind": "something-else"}', encoding="utf-8")

    entries = local_entries(root)
    assert [(entry.kind, entry.camera_id) for entry in entries] == [("intrinsics", "opposite_side")]
    assert entries[0].source_host == "bench"
    assert entries[0].quality == "good"
    assert "렌즈 내부 파라미터" in entries[0].label


def test_publish_then_fetch_moves_the_measurement_to_another_machine(tmp_path: Path):
    bench = tmp_path / "bench"
    site = tmp_path / "site"
    _write_local(bench, "intrinsics", "opposite_side", _intrinsics_payload("opposite_side", "bench-pc"))
    _write_local(bench, "ground", "opposite_side", {
        "kind": "camera_ground_pose",
        "camera_id": "opposite_side",
        "image_width": 1920,
        "image_height": 1080,
        "rvec": [0.1, 0.2, 0.3],
        "tvec": [10.0, 20.0, 3000.0],
        "residual_mm": 18.0,
        "quality": "good",
        "measured_at": "2026-09-17T01:00:00+00:00",
        "source_host": "bench-pc",
        "reviewed": False,
        "safe_to_operate": False,
    })
    remote = FakeSftp()

    published = publish_calibration(CONFIG, bench, sftp_factory=lambda _c: remote, host="bench-pc")
    assert published.ok, published.error
    assert published.safe_to_operate is False  # sharing a file is never authorization
    assert len(published.entries) == 2
    assert published.remote_dir == "/home/site/calibration/bench-pc"
    assert "/home/site/calibration/bench-pc/intrinsics/opposite_side.json" in remote.files
    assert not any(path.endswith(".part") for path in remote.files)  # atomic publication

    offered = list_remote_calibration(CONFIG, sftp_factory=lambda _c: remote)
    assert {(entry.kind, entry.camera_id) for entry in offered} == {
        ("intrinsics", "opposite_side"),
        ("ground", "opposite_side"),
    }
    assert all(entry.source_host == "bench-pc" for entry in offered)
    assert offered[0].measured_at >= offered[1].measured_at  # newest first

    lens = next(entry for entry in offered if entry.kind == "intrinsics")
    fetched = fetch_calibration(CONFIG, [lens], site, sftp_factory=lambda _c: remote)
    assert fetched.ok, fetched.error
    landed = site / "intrinsics" / "opposite_side.json"
    assert landed.is_file()
    payload = json.loads(landed.read_text(encoding="utf-8"))
    # The origin is preserved so the console can show it as borrowed, and it stays unapproved.
    assert payload["source_host"] == "bench-pc"
    assert payload["reviewed"] is False
    assert payload["safe_to_operate"] is False
    assert not list((site / "intrinsics").glob("*.part"))


def test_fetch_rewrites_an_approved_flag_that_arrives_set(tmp_path: Path):
    """A file claiming approval must not import that claim onto this machine."""
    bench, site = tmp_path / "bench", tmp_path / "site"
    payload = _intrinsics_payload("front", "bench-pc")
    payload["reviewed"] = True
    payload["safe_to_operate"] = True
    _write_local(bench, "intrinsics", "front", payload)
    remote = FakeSftp()
    publish_calibration(CONFIG, bench, sftp_factory=lambda _c: remote, host="bench-pc")

    offered = list_remote_calibration(CONFIG, sftp_factory=lambda _c: remote)
    fetch_calibration(CONFIG, offered, site, sftp_factory=lambda _c: remote)

    landed = json.loads((site / "intrinsics" / "front.json").read_text(encoding="utf-8"))
    assert landed["reviewed"] is False
    assert landed["safe_to_operate"] is False


def test_corrupted_download_is_refused(tmp_path: Path):
    bench, site = tmp_path / "bench", tmp_path / "site"
    _write_local(bench, "intrinsics", "front", _intrinsics_payload("front", "bench-pc"))
    remote = FakeSftp()
    publish_calibration(CONFIG, bench, sftp_factory=lambda _c: remote, host="bench-pc")
    offered = list_remote_calibration(CONFIG, sftp_factory=lambda _c: remote)

    # Something changed the bytes after the listing was taken.
    remote.files[offered[0].remote_path] = b'{"kind": "camera_intrinsics", "camera_id": "front"}'
    result = fetch_calibration(CONFIG, offered, site, sftp_factory=lambda _c: remote)

    assert result.ok is False
    assert "검증 실패" in result.error
    assert not (site / "intrinsics" / "front.json").exists()


def test_nothing_to_publish_and_unreachable_nas_are_reported_not_raised(tmp_path: Path):
    empty = publish_calibration(CONFIG, tmp_path / "nothing", sftp_factory=lambda _c: FakeSftp())
    assert empty.ok is False
    assert "측정 파일이 없습니다" in empty.summary

    def refuse(_config):
        raise OSError("connection refused")

    _write_local(tmp_path / "bench", "intrinsics", "front", _intrinsics_payload("front", "bench-pc"))
    down = publish_calibration(CONFIG, tmp_path / "bench", sftp_factory=refuse)
    assert down.ok is False and "connection refused" in down.error
    # A listing against a dead NAS is empty rather than an exception, so the page still works.
    assert list_remote_calibration(CONFIG, sftp_factory=refuse) == ()

    nothing = fetch_calibration(CONFIG, [], tmp_path / "site", sftp_factory=lambda _c: FakeSftp())
    assert nothing.ok is False


def test_remote_path_layout_keeps_machines_apart():
    assert remote_calibration_dir(CONFIG) == "/home/site/calibration"
    assert remote_calibration_dir(CONFIG, "site-pc", "ground") == "/home/site/calibration/site-pc/ground"
    with pytest.raises(ValueError):
        remote_calibration_dir(CONFIG, "..")


def test_entry_description_marks_this_machine():
    entry = CalibrationEntry(
        kind="intrinsics",
        camera_id="front",
        source_host="bench-pc",
        measured_at="2026-09-16T08:14:07+00:00",
        quality="good",
        size_bytes=100,
        sha256="abc",
    )
    assert "bench-pc (이 장비)" in entry.describe(local_host="bench-pc")
    assert "(이 장비)" not in entry.describe(local_host="site-pc")
