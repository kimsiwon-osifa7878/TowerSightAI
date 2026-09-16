from pathlib import Path

from towersightai.config.settings import RawStorageConfig
from towersightai.storage.archive import sha256_path
from towersightai.storage.connection_test import UploadedArtifact
from towersightai.storage.file_transfer import (
    FILE_TRANSFER_ROOT,
    NasFileTransferResult,
    describe_result,
    remote_file_transfer_dir,
    upload_files_to_nas,
    validate_transfer_files,
)


def _config(tmp_path: Path, **overrides) -> RawStorageConfig:
    values = {
        "enabled": True,
        "local_dir": tmp_path / "raw",
        "nas_host": "nas.example.test",
        "nas_port": 45222,
        "nas_username": "uploader",
        "nas_password": "secret",
        "nas_folder": "/home/site/",
        "known_hosts_path": tmp_path / "known_hosts",
    }
    values.update(overrides)
    return RawStorageConfig(**values)


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Path, ...], str]] = []
        self.progress: list[tuple[int, int, str]] = []

    def upload(self, files, remote_dir, progress=None):
        self.calls.append((tuple(files), remote_dir))
        artifacts = []
        for index, path in enumerate(files, start=1):
            if progress is not None:
                progress(index, len(files), path.name)
            artifacts.append(
                UploadedArtifact(
                    relative_path=path.name,
                    size_bytes=path.stat().st_size,
                    sha256=sha256_path(path),
                    media_type="application/octet-stream",
                )
            )
        return tuple(artifacts)


class _FailingUploader:
    def upload(self, files, remote_dir, progress=None):
        raise OSError("remote SHA-256 verification failed")


def test_remote_dir_is_the_single_transfer_folder(tmp_path: Path):
    config = _config(tmp_path)
    assert remote_file_transfer_dir(config) == f"/home/site/{FILE_TRANSFER_ROOT}"


def test_files_are_uploaded_flat_into_the_transfer_folder(tmp_path: Path):
    first = tmp_path / "report.txt"
    first.write_text("hello", encoding="utf-8")
    second = tmp_path / "nested" / "clip.mp4"
    second.parent.mkdir()
    second.write_bytes(b"\x00" * 32)
    uploader = _RecordingUploader()
    seen: list[tuple[int, int, str]] = []

    result = upload_files_to_nas(
        _config(tmp_path),
        [first, second],
        uploader=uploader,
        progress=lambda index, total, name: seen.append((index, total, name)),
    )

    assert result.ok is True
    assert result.safe_to_operate is False
    assert result.remote_dir == "/home/site/transfer"
    assert uploader.calls == [((first, second), "/home/site/transfer")]
    assert [artifact.relative_path for artifact in result.artifacts] == ["report.txt", "clip.mp4"]
    assert result.total_bytes == 5 + 32
    assert result.artifacts[0].sha256 == sha256_path(first)
    assert seen == [(1, 2, "report.txt"), (2, 2, "clip.mp4")]
    assert "2개 파일" in result.summary
    assert describe_result(result)["safe_to_operate"] is False


def test_missing_nas_settings_fail_without_contacting_the_nas(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("x", encoding="utf-8")
    uploader = _RecordingUploader()

    result = upload_files_to_nas(
        _config(tmp_path, enabled=False, nas_host="", nas_password=""), [payload], uploader=uploader
    )

    assert result.ok is False
    assert result.safe_to_operate is False
    assert "SYNOLOGY_NAS_HOST" in result.error
    assert "SYNOLOGY_NAS_PW" in result.error
    assert uploader.calls == []


def test_missing_or_duplicate_files_are_rejected_before_upload(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("x", encoding="utf-8")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    duplicate = other_dir / "a.txt"
    duplicate.write_text("y", encoding="utf-8")
    uploader = _RecordingUploader()

    assert validate_transfer_files([]) == "보낼 파일이 없습니다."
    assert "파일이 없습니다" in validate_transfer_files([tmp_path / "missing.bin"])
    assert "같은 이름" in validate_transfer_files([payload, duplicate])
    assert validate_transfer_files([payload]) == ""

    result = upload_files_to_nas(_config(tmp_path), [payload, tmp_path / "missing.bin"], uploader=uploader)
    assert result.ok is False
    assert "missing.bin" in result.error
    assert uploader.calls == []

    result = upload_files_to_nas(_config(tmp_path), [payload, duplicate], uploader=uploader)
    assert result.ok is False
    assert "같은 이름" in result.error
    assert uploader.calls == []


def test_upload_failure_is_reported_instead_of_raising(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("x", encoding="utf-8")

    result = upload_files_to_nas(_config(tmp_path), [payload], uploader=_FailingUploader())

    assert isinstance(result, NasFileTransferResult)
    assert result.ok is False
    assert result.safe_to_operate is False
    assert result.remote_dir == "/home/site/transfer"
    assert "OSError" in result.error
    assert result.artifacts == ()
