from pathlib import Path

from towersightai.camera.pipeline import build_preview_pipeline, redact_rtsp
from towersightai.config.settings import Settings
from towersightai.inference.pipeline import build_multistream_hailo_pipeline


def test_redact_rtsp_password():
    assert redact_rtsp("rtsp://user:secret@192.168.0.1/stream") == "rtsp://***:***@192.168.0.1/stream"


def test_preview_pipeline_uses_default_display_resolution():
    s = Settings(
        tappas_workspace=Path("/opt/hailo/tappas"),
        hailo_hef_path=Path("/opt/hailo/model.hef"),
        hailo_postprocess_so=Path("/opt/hailo/post.so"),
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=Path("data/calibration/site.json"),
        plc_endpoint="tcp://127.0.0.1:502",
    )

    pipeline = build_preview_pipeline(s.camera_1)

    assert "protocols=tcp drop-on-latency=true" in pipeline
    assert "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream" in pipeline
    assert "video/x-raw,width=1280,height=720" in pipeline
    assert "video/x-raw,format=RGB" in pipeline
    assert "appsink sync=false drop=true max-buffers=1" in pipeline


def test_preview_pipeline_rotates_when_ui_rotation_is_set():
    s = Settings(
        tappas_workspace=Path("/opt/hailo/tappas"),
        hailo_hef_path=Path("/opt/hailo/model.hef"),
        hailo_postprocess_so=Path("/opt/hailo/post.so"),
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=Path("data/calibration/site.json"),
        plc_endpoint="tcp://127.0.0.1:502",
    )

    default_pipeline = build_preview_pipeline(s.camera_2)
    rotated_pipeline = build_preview_pipeline(s.camera_2, rotation_degrees=90)

    assert "videoflip" not in default_pipeline
    assert "video/x-raw,width=1280,height=720" in default_pipeline
    assert "decodebin ! queue" in rotated_pipeline
    assert "videoflip method=counterclockwise ! videoscale" in rotated_pipeline
    assert "video/x-raw,width=720,height=1280" in rotated_pipeline


def test_preview_pipeline_uses_camera_config_default_rotation():
    s = Settings(
        tappas_workspace=Path("/opt/hailo/tappas"),
        hailo_hef_path=Path("/opt/hailo/model.hef"),
        hailo_postprocess_so=Path("/opt/hailo/post.so"),
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b", "rotation_degrees": 90},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=Path("data/calibration/site.json"),
        plc_endpoint="tcp://127.0.0.1:502",
    )

    pipeline = build_preview_pipeline(s.camera_2)

    assert "videoflip method=counterclockwise ! videoscale" in pipeline
    assert "video/x-raw,width=720,height=1280" in pipeline


def test_multistream_pipeline_includes_hailo_elements(tmp_path: Path):
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}")
    s = Settings(
        app_env="production",
        tappas_workspace=tmp_path,
        hailo_hef_path=tmp_path / "m.hef",
        hailo_postprocess_so=tmp_path / "pp.so",
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=calibration,
        plc_endpoint="tcp://127.0.0.1:502",
    )
    pipeline = build_multistream_hailo_pipeline(s)
    for elem in ["hailoroundrobin", "hailonet", "hailofilter", "hailopython", "hailostreamrouter"]:
        assert elem in pipeline
