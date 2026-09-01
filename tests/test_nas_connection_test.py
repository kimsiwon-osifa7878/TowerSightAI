import json
from datetime import datetime, timezone
from pathlib import Path

from towersightai.config.settings import RawStorageConfig
from towersightai.storage.connection_test import (
    CONNECTION_TEST_ROOT,
    SUMMARY_FILENAME,
    NasConnectionTestResult,
    UploadedArtifact,
    build_connection_test_payload,
    connection_test_run_name,
    remote_connection_test_dir,
    run_nas_connection_test,
)
from towersightai.storage.archive import sha256_path


STARTED_AT = datetime(2026, 9, 1, 4, 5, 6, tzinfo=timezone.utc)


def _config(tmp_path: Path, **overrides) -> RawStorageConfig:
    values = {
        "enabled": True,
        "local_dir": tmp_path / "raw",
        "nas_host": "nas.example.test",
        "nas_port": 45222,
        "nas_username": "uploader",
        "nas_password": "secret",
        "nas_folder": "/home/site",
        "known_hosts_path": tmp_path / "known_hosts",
    }
    values.update(overrides)
    return RawStorageConfig(**values)


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def upload(self, local_dir: Path, remote_dir: str) -> tuple[UploadedArtifact, ...]:
        self.calls.append((local_dir, remote_dir))
        artifacts = []
        for path in sorted(local_dir.rglob("*")):
            if path.is_file():
                artifacts.append(
                    UploadedArtifact(
                        relative_path=path.relative_to(local_dir).as_posix(),
                        size_bytes=path.stat().st_size,
                        sha256=sha256_path(path),
                        media_type="application/json",
                    )
                )
        return tuple(artifacts)


class _FailingUploader:
    def upload(self, local_dir: Path, remote_dir: str) -> tuple[UploadedArtifact, ...]:
        raise OSError("remote SHA-256 verification failed")


def test_remote_dir_is_under_the_connectiontest_folder(tmp_path: Path):
    config = _config(tmp_path)
    run_name = connection_test_run_name(STARTED_AT)
    assert run_name == "check-20260901-040506Z"
    assert remote_connection_test_dir(config, run_name) == f"/home/site/{CONNECTION_TEST_ROOT}/{run_name}"


def test_payload_contains_summary_and_optional_clip(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake-clip-bytes")
    payload_dir = tmp_path / "payload"

    build_connection_test_payload(
        payload_dir,
        started_at=STARTED_AT,
        metadata={"camera_id": "front", "frame_count": 20},
        video_path=clip,
    )

    summary = json.loads((payload_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert summary["safe_to_operate"] is False
    assert summary["started_at"] == "2026-09-01T04:05:06+00:00"
    assert summary["camera_id"] == "front"
    assert summary["frame_count"] == 20
    assert summary["video_file"] == "clip.mp4"
    assert (payload_dir / "clip.mp4").read_bytes() == b"fake-clip-bytes"
    assert "secret" not in json.dumps(summary)


def test_payload_without_clip_records_an_empty_video_file(tmp_path: Path):
    payload_dir = tmp_path / "payload"
    build_connection_test_payload(payload_dir, started_at=STARTED_AT)
    summary = json.loads((payload_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert summary["video_file"] == ""
    assert sorted(p.name for p in payload_dir.iterdir()) == [SUMMARY_FILENAME]


def test_run_uploads_payload_and_reports_verified_artifacts(tmp_path: Path):
    config = _config(tmp_path)
    uploader = _RecordingUploader()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0" * 32)

    result = run_nas_connection_test(
        config,
        work_dir=tmp_path / "work",
        metadata={"camera_id": "front"},
        video_path=clip,
        uploader=uploader,
        now=STARTED_AT,
    )

    assert result.ok is True
    assert result.safe_to_operate is False
    assert result.remote_dir == "/home/site/connectiontest/check-20260901-040506Z"
    assert {artifact.relative_path for artifact in result.artifacts} == {SUMMARY_FILENAME, "clip.mp4"}
    assert result.total_bytes > 0
    local_dir, remote_dir = uploader.calls[0]
    assert local_dir == tmp_path / "work" / "check-20260901-040506Z"
    assert remote_dir == result.remote_dir


def test_run_without_camera_clip_still_uploads_the_summary(tmp_path: Path):
    uploader = _RecordingUploader()
    result = run_nas_connection_test(
        _config(tmp_path),
        work_dir=tmp_path / "work",
        uploader=uploader,
        now=STARTED_AT,
    )
    assert result.ok is True
    assert [artifact.relative_path for artifact in result.artifacts] == [SUMMARY_FILENAME]


def test_missing_nas_settings_fail_without_contacting_the_nas(tmp_path: Path):
    uploader = _RecordingUploader()
    config = _config(tmp_path, enabled=False, nas_host="", nas_username="", nas_password="", nas_folder="")

    result = run_nas_connection_test(config, work_dir=tmp_path / "work", uploader=uploader, now=STARTED_AT)

    assert result.ok is False
    assert result.safe_to_operate is False
    assert "SYNOLOGY_NAS_HOST" in result.error
    assert uploader.calls == []


def test_upload_failure_is_reported_instead_of_raising(tmp_path: Path):
    result = run_nas_connection_test(
        _config(tmp_path),
        work_dir=tmp_path / "work",
        uploader=_FailingUploader(),
        now=STARTED_AT,
    )

    assert isinstance(result, NasConnectionTestResult)
    assert result.ok is False
    assert result.safe_to_operate is False
    assert "OSError" in result.error
    assert result.remote_dir.endswith("connectiontest/check-20260901-040506Z")


def test_disabled_raw_storage_with_credentials_can_still_be_checked(tmp_path: Path):
    """Operators must be able to verify the NAS before turning archiving on."""
    config = _config(tmp_path, enabled=False)
    result = run_nas_connection_test(
        config,
        work_dir=tmp_path / "work",
        uploader=_RecordingUploader(),
        now=STARTED_AT,
    )
    assert result.ok is True
