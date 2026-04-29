from pathlib import Path

import pytest

from towersightai.config.settings import Settings


def test_unique_camera_ids_required(tmp_path: Path):
    with pytest.raises(ValueError):
        Settings(
            tappas_workspace=tmp_path,
            hailo_hef_path=tmp_path / "m.hef",
            hailo_postprocess_so=tmp_path / "pp.so",
            camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
            camera_2={"id": "front", "role": "ceiling", "rtsp_url": "rtsp://b"},
            camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
            camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
            calibration_path=tmp_path / "calibration.json",
            plc_endpoint="tcp://127.0.0.1:502",
        )


def test_production_requires_calibration_file(tmp_path: Path):
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            tappas_workspace=tmp_path,
            hailo_hef_path=tmp_path / "m.hef",
            hailo_postprocess_so=tmp_path / "pp.so",
            camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
            camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
            camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
            camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
            calibration_path=tmp_path / "missing.json",
            plc_endpoint="tcp://127.0.0.1:502",
        )
