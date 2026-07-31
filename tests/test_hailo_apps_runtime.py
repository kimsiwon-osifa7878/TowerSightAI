from __future__ import annotations

import os
from pathlib import Path

from towersightai.cli.hailo_apps_detection import (
    PipelineDiagnostics,
    _all_sources_are_files,
    _configure_tappas_postprocess,
    _redact_rtsp_credentials,
    _use_nonblocking_roundrobin,
)
from towersightai.config.settings import Settings
from towersightai.inference import hailo_apps_runtime
from towersightai.inference.hailo_apps_runtime import hailo_apps_runtime_env


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        tappas_workspace=tmp_path / "hailo-apps",
        hailo_apps_workspace=tmp_path / "hailo-apps",
        hailo_apps_resources=tmp_path / "resources",
        hailo_apps_python=tmp_path / "hailo-apps" / "venv_hailo_apps" / "bin" / "python",
        tappas_postproc_path=tmp_path / "post_processes",
        hailo_hef_path=tmp_path / "model.hef",
        hailo_postprocess_so=tmp_path / "postprocess.so",
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_hailo_apps_runtime_pythonpath_does_not_depend_on_working_directory(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")

    env = hailo_apps_runtime_env(settings)

    expected_root = str(Path(hailo_apps_runtime.__file__).resolve().parents[2])
    assert env["PYTHONPATH"].split(os.pathsep) == [
        expected_root,
        str(settings.hailo_apps_workspace.resolve()),
        "/existing/pythonpath",
    ]
    assert env["TAPPAS_POSTPROC_PATH"] == str(settings.tappas_postproc_path.resolve())
    assert env["tappas_postproc_path"] == str(settings.tappas_postproc_path.resolve())


def test_configure_tappas_postprocess_sets_hailo_apps_lowercase_key(tmp_path: Path, monkeypatch):
    stream_id_so = tmp_path / "post_processes" / "libstream_id_tool.so"
    monkeypatch.delenv("TAPPAS_POSTPROC_PATH", raising=False)
    monkeypatch.delenv("tappas_postproc_path", raising=False)

    _configure_tappas_postprocess(stream_id_so)

    expected_dir = str(stream_id_so.parent)
    assert os.environ["TAPPAS_POSTPROC_PATH"] == expected_dir
    assert os.environ["tappas_postproc_path"] == expected_dir


def test_only_local_file_sources_use_finite_eos_behavior(tmp_path: Path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"sample")

    assert _all_sources_are_files((("smoke", str(image)),)) is True
    assert _all_sources_are_files((("front", "rtsp://camera/stream1"),)) is False


def test_pipeline_diagnostics_distinguishes_ingress_from_inference_stall(tmp_path: Path):
    now = [100.0]
    diagnostics = PipelineDiagnostics(
        ("front", "ceiling"),
        tmp_path / "heartbeat.jsonl",
        stall_seconds=10.0,
        startup_seconds=5.0,
        monotonic=lambda: now[0],
    )

    diagnostics.record_ingress("front")
    diagnostics.record_ingress("ceiling")
    diagnostics.record_stage("rtsp_packet", "front")
    diagnostics.record_stage("roundrobin_output", "front")
    diagnostics.record_inference("front")
    diagnostics.record_inference("ceiling")
    assert diagnostics.snapshot()["status"] == "running"

    now[0] += 11.0
    diagnostics.record_ingress("front")
    stalled = diagnostics.snapshot()

    assert stalled["status"] == "stalled"
    assert stalled["stale_cameras"] == ["front", "ceiling"]
    assert stalled["cameras"]["front"]["ingress_buffers"] == 2
    assert stalled["cameras"]["front"]["inference_buffers"] == 1
    assert stalled["cameras"]["front"]["ingress_age_seconds"] == 0.0
    assert stalled["cameras"]["front"]["inference_age_seconds"] == 11.0
    assert stalled["stages"]["rtsp_packet"]["buffers"] == 1
    assert stalled["stages"]["roundrobin_output"]["cameras"]["front"]["buffers"] == 1


def test_pipeline_diagnostics_records_queue_levels(tmp_path: Path):
    now = [10.0]
    diagnostics = PipelineDiagnostics(
        ("front",),
        tmp_path / "heartbeat.jsonl",
        monotonic=lambda: now[0],
    )
    diagnostics.set_queue_levels_provider(
        lambda: {"cameras": {"front": {"roundrobin_q": 30}}, "shared": {"inference_hailonet_q": 3}}
    )

    now[0] += 2.5
    snapshot = diagnostics.snapshot()

    assert snapshot["queue_levels"]["cameras"]["front"]["roundrobin_q"] == 30


def test_multisource_pipeline_uses_nonblocking_roundrobin():
    pipeline = "source ! hailoroundrobin mode=1 name=robin ! hailonet"

    updated = _use_nonblocking_roundrobin(pipeline)

    assert "hailoroundrobin mode=2 name=robin" in updated
    assert "queue-size=3" in updated
    assert "mode=1" not in updated


def test_hailo_apps_pipeline_logging_redacts_rtsp_credentials():
    pipeline = 'rtspsrc location="rtsp://operator:secret@camera.local/stream1"'

    redacted = _redact_rtsp_credentials(pipeline)

    assert "operator" not in redacted
    assert "secret" not in redacted
    assert "rtsp://***:***@camera.local/stream1" in redacted
