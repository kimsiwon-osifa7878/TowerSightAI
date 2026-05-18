from towersightai.camera.preview import build_display_preview_pipeline, build_health_check_pipeline, configured_camera_command
from towersightai.config.env_loader import CameraInspection


def test_display_preview_pipeline_uses_autovideosink():
    pipeline = build_display_preview_pipeline("rtsp://camera/stream", latency_ms=50)

    assert "rtspsrc location=rtsp://camera/stream latency=50 protocols=tcp" in pipeline
    assert "video/x-raw,format=RGB,width=1280,height=720" in pipeline
    assert "fpsdisplaysink" in pipeline
    assert "autovideosink" in pipeline


def test_display_preview_pipeline_accepts_configured_resolution():
    pipeline = build_display_preview_pipeline("rtsp://camera/stream", resolution="1024x576")

    assert "video/x-raw,format=RGB,width=1024,height=576" in pipeline


def test_health_check_pipeline_reads_one_frame_to_fakesink():
    pipeline = build_health_check_pipeline("rtsp://camera/stream")

    assert "video/x-raw,format=RGB,width=1280,height=720" in pipeline
    assert "fakesink" in pipeline
    assert "num-buffers=1" in pipeline


def test_configured_camera_command_rejects_incomplete_camera():
    camera = CameraInspection(1, "front", "front", None, False, ("CAMERA_1_RTSP_URL",), None)

    try:
        configured_camera_command(camera, preview=True)
    except ValueError as exc:
        assert "not fully configured" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
