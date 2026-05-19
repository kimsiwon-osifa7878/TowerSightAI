from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from towersightai.camera.preview import CameraHealthResult
from towersightai.config.settings import Settings
from towersightai.diagnostics import DiagnosticStatus, DiagnosticsService
from towersightai.plc import FakePLCAdapter, SimulatorPLCAdapter


def _settings(tmp_path: Path) -> Settings:
    hef = tmp_path / "model.hef"
    so = tmp_path / "post.so"
    hef.write_text("hef", encoding="utf-8")
    so.write_text("so", encoding="utf-8")
    return Settings(
        tappas_workspace=tmp_path,
        hailo_hef_path=hef,
        hailo_postprocess_so=so,
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://user:secret@192.0.2.10/stream1"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://user:secret@192.0.2.11/stream1"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://user:secret@192.0.2.12/stream1"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://user:secret@192.0.2.13/stream1"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def _env_file(tmp_path: Path, settings: Settings) -> Path:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "APP_ENV=development",
                f"TAPPAS_WORKSPACE={settings.tappas_workspace}",
                f"HAILO_HEF_PATH={settings.hailo_hef_path}",
                f"HAILO_POSTPROCESS_SO={settings.hailo_postprocess_so}",
                "HAILO_NETWORK_NAME=yolov5",
                "CAMERA_1_ID=ceiling",
                "CAMERA_1_ROLE=ceiling",
                "CAMERA_1_RTSP_URL=rtsp://user:secret@192.0.2.10/stream1",
                "CAMERA_2_ID=front",
                "CAMERA_2_ROLE=front",
                "CAMERA_2_RTSP_URL=rtsp://user:secret@192.0.2.11/stream1",
                "CAMERA_3_ID=rear_side",
                "CAMERA_3_ROLE=rear_side",
                "CAMERA_3_RTSP_URL=rtsp://user:secret@192.0.2.12/stream1",
                "CAMERA_4_ID=opposite_side",
                "CAMERA_4_ROLE=opposite_side",
                "CAMERA_4_RTSP_URL=rtsp://user:secret@192.0.2.13/stream1",
                f"CALIBRATION_PATH={settings.calibration_path}",
                "PLC_ENDPOINT=tcp://127.0.0.1:502",
            ]
        ),
        encoding="utf-8",
    )
    return env


def test_diagnostics_settings_redacts_camera_passwords(tmp_path: Path):
    settings = _settings(tmp_path)
    service = DiagnosticsService(settings, env_path=_env_file(tmp_path, settings), artifacts_dir=tmp_path / "diag")

    result = service.check_settings()

    assert result.status is DiagnosticStatus.PASS
    assert result.safe_to_operate is False
    assert "secret" not in result.detail
    assert "***:***" in result.detail


def test_hailo_image_smoke_missing_sample_is_fail(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    service = DiagnosticsService(settings, env_path=_env_file(tmp_path, settings), artifacts_dir=tmp_path / "diag")
    monkeypatch.chdir(tmp_path)

    result = service.run_hailo_image_smoke(timeout_seconds=1)

    assert result.status is DiagnosticStatus.FAIL
    assert "missing sample image" in result.summary
    assert result.safe_to_operate is False


def test_camera_diagnostic_wraps_health_result(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    service = DiagnosticsService(settings, env_path=_env_file(tmp_path, settings), artifacts_dir=tmp_path / "diag")

    def fake_health(camera, **kwargs):
        return CameraHealthResult(camera.id, camera.role, True, True, None, datetime.now(timezone.utc))

    monkeypatch.setattr("towersightai.diagnostics.run_camera_health_check", fake_health)

    result = service.check_camera(1)

    assert result.status is DiagnosticStatus.PASS
    assert "프레임 수신 성공" in result.summary


def test_plc_adapters_record_events():
    fake = FakePLCAdapter()
    fake.send("safety_status_ng", {"reason": "test"})

    simulator = SimulatorPLCAdapter()
    simulator.send("vehicle_parked", {"plate": "sanitized"})

    assert fake.events[0].name == "safety_status_ng"
    assert simulator.event_names == ("vehicle_parked",)
