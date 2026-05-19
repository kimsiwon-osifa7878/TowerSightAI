from __future__ import annotations

from pathlib import Path
import subprocess

from towersightai.config.settings import Settings
from towersightai.inference.image_smoke import (
    HAILO_CALLBACK_MODULE,
    build_image_hailo_pipeline,
    image_smoke_command,
    run_image_hailo_smoke,
)


def _settings(tmp_path: Path) -> Settings:
    hef = tmp_path / "model.hef"
    so = tmp_path / "post.so"
    hef.write_text("hef", encoding="utf-8")
    so.write_text("so", encoding="utf-8")
    return Settings(
        tappas_workspace=tmp_path,
        hailo_hef_path=hef,
        hailo_postprocess_so=so,
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_image_hailo_pipeline_uses_sample_image_hailo_callback_and_png_output(tmp_path: Path):
    settings = _settings(tmp_path)
    image = tmp_path / "sample.jpg"
    output = tmp_path / "out.png"

    pipeline = build_image_hailo_pipeline(settings, image_path=image, output_image_path=output, min_confidence=0.42)

    assert f"filesrc location={image}" in pipeline
    assert "decodebin" in pipeline
    assert "imagefreeze num-buffers=3 is-live=true" in pipeline
    assert "video/x-raw,framerate=1/1" in pipeline
    assert "video/x-raw,format=RGB,width=640,height=640" in pipeline
    assert f"hailonet hef-path={settings.hailo_hef_path} batch-size=1 nms-score-threshold=0.42" in pipeline
    assert f"hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so}" in pipeline
    assert f"hailopython module={HAILO_CALLBACK_MODULE}" in pipeline
    assert "hailooverlay" in pipeline
    assert f"pngenc snapshot=true ! filesink location={output}" in pipeline


def test_image_hailo_command_is_skipped_without_hardware_opt_in(tmp_path: Path):
    settings = _settings(tmp_path)
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"fake")

    result = run_image_hailo_smoke(
        settings,
        image_path=image,
        event_path=tmp_path / "events.jsonl",
        output_image_path=tmp_path / "out.png",
        require_opt_in=True,
    )

    assert result.ok is False
    assert result.reason == "RUN_HARDWARE_TESTS=1 is required"
    assert result.command == tuple(image_smoke_command(settings, image_path=image, output_image_path=tmp_path / "out.png"))


def test_image_hailo_command_reports_timeout(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"fake")

    monkeypatch.setenv("RUN_HARDWARE_TESTS", "1")
    monkeypatch.setattr("towersightai.inference.image_smoke.shutil.which", lambda _: "/usr/bin/gst-launch-1.0")
    terminated = []

    class TimeoutProcess:
        pid = 12345
        returncode = None

        def communicate(self, timeout=None):
            if timeout == 7:
                raise subprocess.TimeoutExpired(cmd=("gst-launch-1.0",), timeout=timeout)
            self.returncode = -15
            return "", "Caught SIGSEGV"

        def kill(self):
            terminated.append("kill")

        def terminate(self):
            terminated.append("terminate")

    monkeypatch.setattr("towersightai.inference.image_smoke.subprocess.Popen", lambda *args, **kwargs: TimeoutProcess())
    monkeypatch.setattr("towersightai.inference.image_smoke.os.killpg", lambda pid, sig: terminated.append((pid, sig)))

    result = run_image_hailo_smoke(
        settings,
        image_path=image,
        event_path=tmp_path / "events.jsonl",
        output_image_path=tmp_path / "out.png",
        timeout_seconds=7,
    )

    assert result.ok is False
    assert result.reason == "gst-launch timed out after 7 seconds"
    assert terminated


def test_image_hailo_command_adds_tappas_venv_to_gst_environment(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    tappas_bin = tmp_path / "hailo_tappas_venv" / "bin"
    tappas_bin.mkdir(parents=True)
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"fake")
    captured_env = {}

    monkeypatch.setenv("RUN_HARDWARE_TESTS", "1")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr("towersightai.inference.image_smoke.shutil.which", lambda _: "/usr/bin/gst-launch-1.0")

    class CompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *, timeout_seconds, env):
        captured_env.update(env)
        event_path.write_text('{"label": "car"}\n', encoding="utf-8")
        return CompletedProcess()

    event_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("towersightai.inference.image_smoke._run_gst_command", fake_run)

    result = run_image_hailo_smoke(
        settings,
        image_path=image,
        event_path=event_path,
        output_image_path=tmp_path / "out.png",
    )

    assert result.ok is True
    assert captured_env["VIRTUAL_ENV"] == str(tmp_path / "hailo_tappas_venv")
    assert captured_env["PATH"].startswith(str(tappas_bin))
