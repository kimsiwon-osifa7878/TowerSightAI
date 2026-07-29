from __future__ import annotations

import os
from pathlib import Path

from towersightai.cli.hailo_apps_detection import _all_sources_are_files, _configure_tappas_postprocess
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
