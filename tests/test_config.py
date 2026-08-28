from pathlib import Path

import pytest

from towersightai.config.settings import BirdviewMode, LD2410Config, Settings


def test_disabled_birdview_excludes_ceiling_from_active_cameras(tmp_path: Path):
    settings = Settings(
        tappas_workspace=tmp_path,
        hailo_hef_path=tmp_path / "m.hef",
        hailo_postprocess_so=tmp_path / "pp.so",
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
        birdview_mode="disabled",
    )

    assert settings.birdview_mode is BirdviewMode.disabled
    assert settings.birdview_enabled is False
    assert [camera.role.value for camera in settings.active_cameras] == ["front", "rear_side", "opposite_side"]


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


def test_camera_rotation_must_be_right_angle(tmp_path: Path):
    with pytest.raises(ValueError, match="Camera rotation"):
        Settings(
            tappas_workspace=tmp_path,
            hailo_hef_path=tmp_path / "m.hef",
            hailo_postprocess_so=tmp_path / "pp.so",
            camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a", "rotation_degrees": 45},
            camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
            camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
            camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
            calibration_path=tmp_path / "calibration.json",
            plc_endpoint="tcp://127.0.0.1:502",
        )


def test_ld2410_config_rejects_invalid_network_and_timing_values():
    with pytest.raises(ValueError, match="LD2410_TCP_PORT"):
        LD2410Config(port=0)
    with pytest.raises(ValueError, match="LD2410_BUFFER_SECONDS"):
        LD2410Config(buffer_seconds=0)


def test_ld2410_raw_only_integration_requires_raw_storage(tmp_path: Path):
    with pytest.raises(ValueError, match="RAW_DATA_ENABLED"):
        Settings(
            tappas_workspace=tmp_path,
            hailo_hef_path=tmp_path / "m.hef",
            hailo_postprocess_so=tmp_path / "pp.so",
            camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
            camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
            camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
            camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
            calibration_path=tmp_path / "calibration.json",
            plc_endpoint="tcp://127.0.0.1:502",
            ld2410={"enabled": True},
        )
