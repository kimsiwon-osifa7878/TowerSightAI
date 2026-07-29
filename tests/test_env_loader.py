from pathlib import Path

import pytest

from towersightai.config.env_loader import inspect_env, load_settings_from_env, parse_env_file


def _write_env(path: Path, *, omit_camera_4: bool = False) -> Path:
    camera_4 = "" if omit_camera_4 else """
CAMERA_4_ID=opposite_side
CAMERA_4_ROLE=opposite_side
CAMERA_4_RTSP_URL=rtsp://user:secret@192.0.2.4/stream1
CAMERA_4_USERNAME=user
CAMERA_4_PASSWORD=secret
CAMERA_4_ROTATION_DEGREES=0
"""
    path.write_text(
        f"""
# local development settings
APP_ENV=development
LOG_LEVEL=DEBUG
TAPPAS_WORKSPACE=/opt/hailo/tappas
TAPPAS_POSTPROC_PATH=/usr/lib/hailo/tappas/post_processes
HAILO_MODEL_DIR=models/hailo
HAILO_HEF_PATH=${{HAILO_MODEL_DIR}}/general/model.hef
HAILO_POSTPROCESS_SO=${{HAILO_MODEL_DIR}}/postprocess/post.so
HAILO_NETWORK_NAME=yolov5
HAILO_VEHICLE_DETECTION_HEF_PATH=${{HAILO_MODEL_DIR}}/vehicle_detection/vehicle.hef
HAILO_VEHICLE_DETECTION_CONFIG_PATH=${{HAILO_MODEL_DIR}}/vehicle_detection/configs/vehicle.json
HAILO_VEHICLE_DETECTION_POSTPROCESS_SO=${{HAILO_MODEL_DIR}}/postprocess/vehicle.so
HAILO_PERSON_PRESENCE_HEF_PATH=${{HAILO_MODEL_DIR}}/person_presence/person.hef
HAILO_PERSON_PRESENCE_CONFIG_PATH=${{HAILO_MODEL_DIR}}/person_presence/configs/person.json
HAILO_PERSON_PRESENCE_POSTPROCESS_SO=${{HAILO_MODEL_DIR}}/postprocess/person.so
HAILO_PERSON_PRESENCE_CROP_SO=${{HAILO_MODEL_DIR}}/postprocess/cropping_algorithms/crop.so
FAST_ALPR_DETECTOR_MODEL=detector-test-model
FAST_ALPR_OCR_MODEL=ocr-test-model
CAMERA_1_ID=ceiling
CAMERA_1_ROLE=ceiling
CAMERA_1_RTSP_URL=rtsp://user:secret@192.0.2.1/stream1
CAMERA_1_USERNAME=user
CAMERA_1_PASSWORD=secret
CAMERA_1_ROTATION_DEGREES=90
CAMERA_2_ID=front
CAMERA_2_ROLE=front
CAMERA_2_RTSP_URL=rtsp://user:secret@192.0.2.2/stream1
CAMERA_2_ROTATION_DEGREES=0
CAMERA_3_ID=rear_side
CAMERA_3_ROLE=rear_side
CAMERA_3_RTSP_URL=rtsp://user:secret@192.0.2.3/stream1
CAMERA_3_ROTATION_DEGREES=0
{camera_4}
CALIBRATION_PATH=data/calibration/site.json
PLC_ENDPOINT=tcp://127.0.0.1:502
UI_FULLSCREEN=false
UI_CAMERA_RESOLUTION=1024x576
""".strip(),
        encoding="utf-8",
    )
    return path


def test_parse_env_file_supports_comments_export_and_quotes(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nexport APP_ENV='development'\nLOG_LEVEL=\"INFO\"\n", encoding="utf-8")

    assert parse_env_file(env_path) == {"APP_ENV": "development", "LOG_LEVEL": "INFO"}


def test_load_settings_from_env_builds_settings(tmp_path: Path):
    settings = load_settings_from_env(_write_env(tmp_path / ".env"))

    assert settings.log_level == "DEBUG"
    assert settings.ui_fullscreen is False
    assert settings.ui_camera_resolution.width == 1024
    assert settings.ui_camera_resolution.height == 576
    assert settings.hailo_model_dir == Path("models/hailo")
    assert settings.tappas_postproc_path == Path("/usr/lib/hailo/tappas/post_processes")
    assert settings.hailo_hef_path == Path("models/hailo/general/model.hef")
    assert settings.hailo_postprocess_so == Path("models/hailo/postprocess/post.so")
    assert settings.hailo_vehicle_detection_hef_path == Path("models/hailo/vehicle_detection/vehicle.hef")
    assert settings.hailo_person_presence_hef_path == Path("models/hailo/person_presence/person.hef")
    assert settings.fast_alpr_detector_model == "detector-test-model"
    assert settings.fast_alpr_ocr_model == "ocr-test-model"
    assert [camera.id for camera in settings.cameras] == ["ceiling", "front", "rear_side", "opposite_side"]
    assert [camera.rotation_degrees for camera in settings.cameras] == [90, 0, 0, 0]


def test_load_settings_from_env_expands_home_and_config_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    env_path = _write_env(tmp_path / ".env")
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        .replace("TAPPAS_WORKSPACE=/opt/hailo/tappas", "TAPPAS_WORKSPACE=~/hailotappas/tappas")
        .replace("HAILO_MODEL_DIR=models/hailo", "HAILO_MODEL_DIR=~/tower-models")
        .replace("HAILO_HEF_PATH=${HAILO_MODEL_DIR}/general/model.hef", "HAILO_HEF_PATH=${HAILO_MODEL_DIR}/model.hef")
        .replace("HAILO_POSTPROCESS_SO=${HAILO_MODEL_DIR}/postprocess/post.so", "HAILO_POSTPROCESS_SO=$HAILO_MODEL_DIR/post.so"),
        encoding="utf-8",
    )

    settings = load_settings_from_env(env_path)

    assert settings.tappas_workspace == home / "hailotappas" / "tappas"
    assert settings.hailo_model_dir == home / "tower-models"
    assert settings.hailo_hef_path == home / "tower-models" / "model.hef"
    assert settings.hailo_postprocess_so == home / "tower-models" / "post.so"
    assert settings.hailo_vehicle_detection_hef_path == home / "tower-models" / "vehicle_detection" / "vehicle.hef"
    assert settings.hailo_person_presence_hef_path == home / "tower-models" / "person_presence" / "person.hef"


def test_inspect_env_allows_partial_camera_configuration(tmp_path: Path):
    result = inspect_env(_write_env(tmp_path / ".env", omit_camera_4=True))

    assert result.env_exists is True
    assert result.settings_loadable is False
    assert [camera.id for camera in result.configured_cameras] == ["ceiling", "front", "rear_side"]
    assert result.cameras[3].missing_fields == ("CAMERA_4_ID", "CAMERA_4_ROLE", "CAMERA_4_RTSP_URL")
    assert result.cameras[0].redacted_rtsp_url == "rtsp://***:***@192.0.2.1/stream1"


def test_invalid_bool_raises(tmp_path: Path):
    env_path = _write_env(tmp_path / ".env")
    env_path.write_text(env_path.read_text(encoding="utf-8").replace("UI_FULLSCREEN=false", "UI_FULLSCREEN=maybe"), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid boolean"):
        load_settings_from_env(env_path)


def test_invalid_ui_camera_resolution_raises(tmp_path: Path):
    env_path = _write_env(tmp_path / ".env")
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace("UI_CAMERA_RESOLUTION=1024x576", "UI_CAMERA_RESOLUTION=wide"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="WIDTHxHEIGHT"):
        load_settings_from_env(env_path)


def test_invalid_camera_rotation_raises(tmp_path: Path):
    env_path = _write_env(tmp_path / ".env")
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace("CAMERA_1_ROTATION_DEGREES=90", "CAMERA_1_ROTATION_DEGREES=45"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Camera rotation"):
        load_settings_from_env(env_path)
