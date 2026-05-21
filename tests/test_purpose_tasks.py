import subprocess
from pathlib import Path

from towersightai.config.settings import Settings
from towersightai.inference.purpose_tasks import (
    PURPOSE_LPR_IMAGE,
    PURPOSE_PERSON_PRESENCE,
    PURPOSE_VEHICLE_DETECTION,
    PurposeInferenceProcess,
    PurposeInferenceRunner,
    build_purpose_process,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        tappas_workspace=tmp_path / "tappas",
        hailo_hef_path=tmp_path / "default.hef",
        hailo_postprocess_so=tmp_path / "post.so",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a", "rotation_degrees": 90},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_vehicle_detection_process_uses_lpr_vehicle_model_and_callback(tmp_path: Path):
    settings = _settings(tmp_path)

    process = build_purpose_process(
        PURPOSE_VEHICLE_DETECTION,
        settings,
        cameras=tuple(settings.cameras),
        camera_rotations={"front": 0},
        event_dir=tmp_path / "events",
    )
    command = " ".join(process.command)

    assert process.task_id == PURPOSE_VEHICLE_DETECTION
    assert process.camera_ids == ("front",)
    assert "yolov5m_vehicles.hef" in command
    assert "function-name=yolov5m_vehicles" in command
    assert "configs/yolov5_vehicle_detection.json" in command
    assert "hailopython" in command


def test_lpr_image_process_uses_three_lpr_models(tmp_path: Path):
    settings = _settings(tmp_path)
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "plate.png").write_bytes(b"png")

    process = build_purpose_process(
        PURPOSE_LPR_IMAGE,
        settings,
        image_dir=image_dir,
        event_dir=tmp_path / "events",
    )
    command = " ".join(process.command)

    assert process.task_id == PURPOSE_LPR_IMAGE
    assert "yolov5m_vehicles.hef" in command
    assert "tiny_yolov4_license_plates.hef" in command
    assert "lprnet.hef" in command
    assert "function-name=tiny_yolov4_license_plates" in command
    assert "libocr_post.so" in command
    assert "liblpr_ocrsink.so" in command


def test_person_presence_process_uses_detector_without_reid_embedding(tmp_path: Path):
    settings = _settings(tmp_path)

    process = build_purpose_process(
        PURPOSE_PERSON_PRESENCE,
        settings,
        cameras=tuple(settings.cameras[:2]),
        camera_rotations={"ceiling": 90, "front": 0},
        event_dir=tmp_path / "events",
    )
    command = " ".join(process.command)

    assert process.task_id == PURPOSE_PERSON_PRESENCE
    assert process.camera_ids == ("ceiling", "front")
    assert "yolov5s_personface_reid.hef" in command
    assert "function-name=yolov5_personface_letterbox" in command
    assert "hailopython" in command
    assert "allowed_labels=('person', 'human')" in (tmp_path / "events/person_presence/person_presence_callback_ceiling.py").read_text(encoding="utf-8")
    assert "repvgg_a0_person_reid_2048.hef" not in command
    assert "hailogallery" not in command
    assert "libre_id" not in command


def test_purpose_runner_stops_spinning_gstreamer_on_fatal_hailo_log(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    process = PurposeInferenceProcess(
        task_id=PURPOSE_VEHICLE_DETECTION,
        label="차량 전용 검출",
        command=("gst-launch-1.0", "-q", "fakesrc", "!", "fakesink"),
        env={},
        log_path=tmp_path / "vehicle.gst.log",
        event_path=tmp_path / "vehicle.jsonl",
        model_paths=(tmp_path / "vehicle.hef",),
        camera_ids=("front",),
    )

    class SpinningProcess:
        pid = 12345

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):  # noqa: ANN001 - fake subprocess API.
            raise subprocess.TimeoutExpired("gst-launch-1.0", timeout)

    spinning = SpinningProcess()

    def fake_popen(*_args, **_kwargs):
        process.log_path.write_text("Caught SIGSEGV\nSpinning.\n", encoding="utf-8")
        return spinning

    monkeypatch.setattr("towersightai.inference.purpose_tasks.shutil.which", lambda _command: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr("towersightai.inference.purpose_tasks.subprocess.Popen", fake_popen)
    monkeypatch.setattr("towersightai.inference.purpose_tasks.os.killpg", lambda _pid, _sig: None)

    runner = PurposeInferenceRunner(process, on_events=lambda _events: None, on_error=errors.append)

    assert runner.run() is True
    assert errors
    assert "Caught SIGSEGV" in errors[0]
