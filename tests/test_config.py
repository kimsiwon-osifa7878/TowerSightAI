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


def test_vehicle_envelope_defaults_match_the_approval_drawing():
    from towersightai.config.settings import CameraRole, VehicleEnvelopeConfig

    config = VehicleEnvelopeConfig()
    # 구로 신안타워 승인도 J001: 주차구획 5,350 x 2,200, 레일 내폭 2,106, 주차구획 높이 1,600/1,900,
    # 설계기준 L5205 x W2000(W2100 with mirrors) x H1550/1850, 바퀴폭 2,000 이하.
    assert (config.pallet_length_mm, config.pallet_width_mm) == (5350.0, 2200.0)
    assert config.rail_inner_width_mm == 2106.0
    assert (config.bay_height_sedan_mm, config.bay_height_suv_mm) == (1600.0, 1900.0)
    assert (config.max_vehicle_length_mm, config.max_vehicle_width_mm) == (5205.0, 2000.0)
    assert config.max_vehicle_width_with_mirrors_mm == 2100.0
    assert (config.max_vehicle_height_sedan_mm, config.max_vehicle_height_suv_mm) == (1550.0, 1850.0)
    assert config.max_wheel_track_mm == 2000.0
    assert config.front_left_role is CameraRole.rear_side
    assert config.rear_right_role is CameraRole.opposite_side


def test_vehicle_envelope_rejects_limits_that_exceed_the_bay_or_bad_cameras():
    from towersightai.config.settings import VehicleEnvelopeConfig

    with pytest.raises(ValueError, match="VEHICLE_BOX_PALLET_LENGTH_MM"):
        VehicleEnvelopeConfig(pallet_length_mm=0)
    with pytest.raises(ValueError, match="VEHICLE_BOX_MAX_LENGTH_MM"):
        VehicleEnvelopeConfig(max_vehicle_length_mm=5400)
    with pytest.raises(ValueError, match="VEHICLE_BOX_MAX_WIDTH_MM"):
        VehicleEnvelopeConfig(max_vehicle_width_mm=2300)
    with pytest.raises(ValueError, match="VEHICLE_BOX_MAX_WIDTH_WITH_MIRRORS_MM"):
        VehicleEnvelopeConfig(max_vehicle_width_with_mirrors_mm=1900)
    with pytest.raises(ValueError, match="VEHICLE_BOX_MAX_HEIGHT_SUV_MM"):
        VehicleEnvelopeConfig(max_vehicle_height_suv_mm=2000)
    with pytest.raises(ValueError, match="VEHICLE_BOX_MAX_WHEEL_TRACK_MM"):
        VehicleEnvelopeConfig(max_wheel_track_mm=2200)
    with pytest.raises(ValueError, match="VEHICLE_BOX_FRONT_LEFT_CAMERA"):
        VehicleEnvelopeConfig(front_left_camera_role="ceiling")
    with pytest.raises(ValueError, match="must differ"):
        VehicleEnvelopeConfig(front_left_camera_role="opposite_side")


def test_settings_carry_a_vehicle_envelope_with_swappable_side_cameras(tmp_path: Path):
    from towersightai.config.settings import CameraRole

    settings = Settings(
        tappas_workspace="/tmp/tappas",
        hailo_hef_path="/tmp/model.hef",
        hailo_postprocess_so="/tmp/post.so",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
        vehicle_envelope={"front_left_camera_role": "opposite_side", "rear_right_camera_role": "rear_side"},
    )
    assert settings.vehicle_envelope.front_left_role is CameraRole.opposite_side
    assert settings.vehicle_envelope.rear_right_role is CameraRole.rear_side
    assert settings.vehicle_envelope.pallet_length_mm == 5350.0


def test_radar_raw_logging_defaults_and_validation(tmp_path: Path):
    from towersightai.config.settings import RawStorageConfig

    config = RawStorageConfig(local_dir=tmp_path)
    assert config.ld2410_sample_interval_seconds == 1.0
    assert config.radar_window_min_seconds == 3.0
    assert config.radar_window_clear_seconds == 5.0
    assert config.media_radar_evidence is True
    assert config.media_radar_min_interval_seconds == 60.0
    assert config.media_radar_clip_max_seconds == 30.0
    assert RawStorageConfig(local_dir=tmp_path, ld2410_sample_interval_seconds=0).ld2410_sample_interval_seconds == 0
    with pytest.raises(ValueError, match="RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS"):
        RawStorageConfig(local_dir=tmp_path, ld2410_sample_interval_seconds=-1)
    with pytest.raises(ValueError, match="RAW_DATA_RADAR_WINDOW_MIN_SECONDS"):
        RawStorageConfig(local_dir=tmp_path, radar_window_min_seconds=0)
    with pytest.raises(ValueError, match="RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS"):
        RawStorageConfig(local_dir=tmp_path, radar_window_clear_seconds=0)
    with pytest.raises(ValueError, match="RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS"):
        RawStorageConfig(local_dir=tmp_path, media_radar_min_interval_seconds=0)
    with pytest.raises(ValueError, match="RAW_MEDIA_RADAR_CLIP_MAX_SECONDS must be at least"):
        RawStorageConfig(local_dir=tmp_path, media_segment_seconds=2, media_radar_clip_max_seconds=1)
