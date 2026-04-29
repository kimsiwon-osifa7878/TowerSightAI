from pathlib import Path

from towersightai.camera.pipeline import redact_rtsp
from towersightai.config.settings import Settings
from towersightai.inference.pipeline import build_multistream_hailo_pipeline


def test_redact_rtsp_password():
    assert redact_rtsp("rtsp://user:secret@192.168.0.1/stream") == "rtsp://***:***@192.168.0.1/stream"


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
