from __future__ import annotations

from pathlib import Path

from towersightai.config.settings import Settings
from towersightai.inference.model_discovery import discover_hailo_hef_models


def _settings(tmp_path: Path, hef_path: Path) -> Settings:
    so = tmp_path / "post.so"
    so.write_text("so", encoding="utf-8")
    return Settings(
        tappas_workspace=tmp_path / "tappas",
        hailo_model_dir=tmp_path / "hailo-models",
        hailo_hef_path=hef_path,
        hailo_postprocess_so=so,
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_discover_hailo_hef_models_scans_configured_and_project_model_dirs(tmp_path: Path):
    default_dir = tmp_path / "models"
    default_dir.mkdir()
    default_hef = default_dir / "b_default.hef"
    default_hef.write_text("hef", encoding="utf-8")
    project_hef_dir = tmp_path / "hailo-models" / "vehicle_detection"
    project_hef_dir.mkdir(parents=True)
    project_hef = project_hef_dir / "a_vehicle.hef"
    project_hef.write_text("hef", encoding="utf-8")
    (project_hef_dir / "ignore.txt").write_text("txt", encoding="utf-8")

    models = discover_hailo_hef_models(_settings(tmp_path, default_hef))

    assert models == (project_hef, default_hef)


def test_discover_hailo_hef_models_falls_back_to_config_path_when_no_hef_found(tmp_path: Path):
    configured_hef = tmp_path / "missing" / "model.hef"

    models = discover_hailo_hef_models(_settings(tmp_path, configured_hef))

    assert models == (configured_hef,)
