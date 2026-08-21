import subprocess
import json
import sys
import types
from pathlib import Path

from towersightai.cli.fast_alpr_lpr import run_fast_alpr_lpr
from towersightai.config.settings import Settings
from towersightai.inference.callback import run_lpr_ocr_with_config
from towersightai.inference.purpose_tasks import (
    PURPOSE_LPR_IMAGE,
    PURPOSE_PERSON_PRESENCE,
    PURPOSE_VEHICLE_DETECTION,
    PurposeInferenceProcess,
    PurposeInferenceRunner,
    _archive_runtime_file,
    _read_last_text_line,
    build_purpose_process,
    parse_plate_ocr_json,
)


def _settings(tmp_path: Path) -> Settings:
    model_dir = tmp_path / "models" / "hailo"
    return Settings(
        tappas_workspace=tmp_path / "tappas",
        hailo_model_dir=model_dir,
        hailo_hef_path=tmp_path / "default.hef",
        hailo_postprocess_so=tmp_path / "post.so",
        hailo_vehicle_detection_hef_path=model_dir / "vehicle_detection/yolov5m_vehicles.hef",
        hailo_vehicle_detection_config_path=model_dir / "vehicle_detection/configs/yolov5_vehicle_detection.json",
        hailo_vehicle_detection_postprocess_so=model_dir / "postprocess/libyolo_hailortpp_post.so",
        hailo_person_presence_hef_path=model_dir / "person_presence/yolov5s_personface_reid.hef",
        hailo_person_presence_config_path=model_dir / "person_presence/configs/yolov5_personface.json",
        hailo_person_presence_postprocess_so=model_dir / "postprocess/libyolo_post.so",
        hailo_person_presence_crop_so=model_dir / "postprocess/cropping_algorithms/libwhole_buffer.so",
        fast_alpr_detector_model="detector-test-model",
        fast_alpr_ocr_model="ocr-test-model",
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://a", "rotation_degrees": 90},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_vehicle_detection_process_uses_hailo_apps_adapter_and_diagnostics(tmp_path: Path):
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
    assert settings.hailo_vehicle_detection_hef_path.as_posix() in command
    assert settings.hailo_vehicle_detection_postprocess_so.as_posix() in command
    assert "towersightai.cli.hailo_apps_detection" in command
    assert "--allowed-label car" in command
    assert "--diagnostic-path" in command
    assert process.diagnostic_path == tmp_path / "events/vehicle_detection/vehicle.heartbeat.jsonl"
    assert process.max_consecutive_restarts == 3
    assert "hailopython" not in command


def test_lpr_image_process_uses_fast_alpr_batch_runner(tmp_path: Path):
    settings = _settings(tmp_path)
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "plate.png").write_bytes(b"png")
    (image_dir / "plate.jpg").write_bytes(b"jpg")

    process = build_purpose_process(
        PURPOSE_LPR_IMAGE,
        settings,
        image_dir=image_dir,
        event_dir=tmp_path / "events",
    )
    command = " ".join(process.command)

    assert process.task_id == PURPOSE_LPR_IMAGE
    assert process.command[:3] == (sys.executable, "-m", "towersightai.cli.fast_alpr_lpr")
    assert "--image-dir" in process.command
    assert process.command[process.command.index("--detector-model") + 1] == settings.fast_alpr_detector_model
    assert process.command[process.command.index("--ocr-model") + 1] == settings.fast_alpr_ocr_model
    assert "gst-launch" not in command
    assert "yolov5m_vehicles.hef" not in command
    assert "tiny_yolov4_license_plates.hef" not in command
    assert "lprnet.hef" not in command
    assert "hailopython" not in command
    assert "liblpr_ocrsink.so" not in command
    manifest = json.loads((tmp_path / "events/lpr_image/lpr_manifest.json").read_text(encoding="utf-8"))
    assert manifest["detector_model"] == "detector-test-model"
    assert manifest["ocr_model"] == "ocr-test-model"
    assert [image["source_image"] for image in manifest["images"]] == [
        str(image_dir / "plate.jpg"),
        str(image_dir / "plate.png"),
    ]


def test_fast_alpr_runner_writes_per_image_results(tmp_path: Path, monkeypatch):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "a.png").write_bytes(b"a")
    (image_dir / "b.jpg").write_bytes(b"b")
    event_path = tmp_path / "events/lpr.jsonl"
    log_path = tmp_path / "events/lpr.gst.log"
    manifest_path = tmp_path / "events/lpr_manifest.json"

    class BoundingBox:
        x1 = 1
        y1 = 2
        x2 = 3
        y2 = 4

    class Detection:
        label = "license_plate"
        confidence = 0.88
        bounding_box = BoundingBox()

    class Ocr:
        text = "12가3456"
        confidence = [0.9, 0.95]
        region = None
        region_confidence = None

    class Result:
        detection = Detection()
        ocr = Ocr()

    init_kwargs = {}

    class ALPR:
        def __init__(self, **kwargs):
            init_kwargs.update(kwargs)

        def predict(self, _path):
            return [Result()]

    monkeypatch.setitem(sys.modules, "fast_alpr", types.SimpleNamespace(ALPR=ALPR))

    assert run_fast_alpr_lpr(
        image_dir=image_dir,
        event_path=event_path,
        log_path=log_path,
        manifest_path=manifest_path,
        detector_model="detector-test-model",
        ocr_model="ocr-test-model",
    ) == 0

    lines = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    plate_events = [line for line in lines if line["type"] == "plate_ocr"]
    attempts = [line for line in lines if line["type"] == "plate_ocr_attempt"]

    assert len(plate_events) == 2
    assert len(attempts) == 2
    assert all(attempt["status"] == "recognized" for attempt in attempts)
    assert all(attempt["elapsed_ms"] >= 0 for attempt in attempts)
    assert attempts[0]["best_plate"]["plate_number"] == "12가3456"
    assert init_kwargs == {
        "detector_model": "detector-test-model",
        "ocr_model": "ocr-test-model",
        "ocr_device": "cpu",
    }
    assert "recognized-images=2/2" in log_path.read_text(encoding="utf-8")


def test_lpr_ocr_callback_writes_plate_event(tmp_path: Path, monkeypatch):
    event_path = tmp_path / "lpr.jsonl"

    class Classification:
        def get_classification_type(self):
            return "ocr"

        def get_label(self):
            return "12가3456"

        def get_confidence(self):
            return 0.94

    class PlateDetection:
        classifications = (Classification(),)
        detections = ()

    class VehicleDetection:
        classifications = ()
        detections = (PlateDetection(),)

    class Roi:
        classifications = ()
        detections = (VehicleDetection(),)

    class VideoFrame:
        roi = Roi()

    monkeypatch.setattr("towersightai.inference.callback._flow_ok", lambda: "OK")

    assert (
        run_lpr_ocr_with_config(
            VideoFrame(),
            event_path=str(event_path),
            min_confidence=0.9,
            source_image="tmp/car_number-test/plate.png",
            frame_index=0,
        )
        == "OK"
    )
    lines = event_path.read_text(encoding="utf-8").splitlines()
    event = parse_plate_ocr_json(lines[0])
    attempt = json.loads(lines[1])

    assert event is not None
    assert event.plate_number == "12가3456"
    assert event.confidence == 0.94
    assert attempt["type"] == "plate_ocr_attempt"
    assert attempt["source_image"] == "tmp/car_number-test/plate.png"
    assert attempt["frame_index"] == 0
    assert attempt["status"] == "recognized"
    assert attempt["plates"][0]["plate_number"] == "12가3456"


def test_lpr_ocr_callback_writes_no_result_attempt(tmp_path: Path, monkeypatch):
    event_path = tmp_path / "lpr.jsonl"

    class Roi:
        classifications = ()
        detections = ()

    class VideoFrame:
        roi = Roi()

    monkeypatch.setattr("towersightai.inference.callback._flow_ok", lambda: "OK")

    assert (
        run_lpr_ocr_with_config(
            VideoFrame(),
            event_path=str(event_path),
            source_image="tmp/car_number-test/no-result.png",
            frame_index=7,
        )
        == "OK"
    )
    attempt = json.loads(event_path.read_text(encoding="utf-8"))

    assert attempt["type"] == "plate_ocr_attempt"
    assert attempt["source_image"] == "tmp/car_number-test/no-result.png"
    assert attempt["frame_index"] == 7
    assert attempt["status"] == "no_result"
    assert attempt["plates"] == []


def test_person_presence_process_uses_hailo_apps_detector_without_reid_embedding(tmp_path: Path):
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
    assert settings.hailo_person_presence_hef_path.as_posix() in command
    assert settings.hailo_person_presence_postprocess_so.as_posix() in command
    assert "towersightai.cli.hailo_apps_detection" in command
    assert "--allowed-label person" in command
    assert "hailopython" not in command
    assert "repvgg_a0_person_reid_2048.hef" not in command
    assert "hailogallery" not in command
    assert "libre_id" not in command
    assert "--diagnostic-path" in process.command
    assert process.diagnostic_path == tmp_path / "events/person_presence/person_presence.heartbeat.jsonl"
    assert process.max_consecutive_restarts == 3


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


def test_purpose_runner_stops_alive_process_on_pipeline_heartbeat_stall(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    diagnostic_path = tmp_path / "person_presence.heartbeat.jsonl"
    process = PurposeInferenceProcess(
        task_id=PURPOSE_PERSON_PRESENCE,
        label="사람 존재 감지",
        command=("gst-launch-1.0", "-q", "fakesrc", "!", "fakesink"),
        env={},
        log_path=tmp_path / "person_presence.gst.log",
        event_path=tmp_path / "person_presence.jsonl",
        model_paths=(),
        camera_ids=("front",),
        diagnostic_path=diagnostic_path,
    )

    class StalledProcess:
        pid = 54321
        returncode = None

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout=None):  # noqa: ANN001 - fake subprocess API.
            raise subprocess.TimeoutExpired("gst-launch-1.0", timeout)

    stalled = StalledProcess()

    def fake_popen(*_args, **_kwargs):
        diagnostic_path.write_text(
            json.dumps(
                {
                    "status": "stalled",
                    "stale_cameras": ["front"],
                    "stages": {
                        "rtsp_packet": {"buffers": 120, "age_seconds": 0.1},
                        "callback": {"buffers": 90, "age_seconds": 16.0},
                    },
                    "queue_levels": {"shared": {"inference_hailonet_q": 3}},
                    "cameras": {
                        "front": {
                            "ingress_buffers": 100,
                            "inference_buffers": 90,
                            "ingress_age_seconds": 0.1,
                            "inference_age_seconds": 16.0,
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return stalled

    monkeypatch.setattr("towersightai.inference.purpose_tasks.shutil.which", lambda _command: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr("towersightai.inference.purpose_tasks.subprocess.Popen", fake_popen)
    monkeypatch.setattr("towersightai.inference.purpose_tasks.os.killpg", lambda _pid, _sig: None)

    runner = PurposeInferenceRunner(process, on_events=lambda _events: None, on_error=errors.append)

    assert runner.run() is True
    assert errors
    assert "AI pipeline stalled" in errors[0]
    assert "stale-cameras=front" in errors[0]
    assert '"rtsp_packet"' in errors[0]
    assert '"inference_hailonet_q": 3' in errors[0]


def test_purpose_runner_stops_alive_process_when_heartbeat_never_starts(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    diagnostic_path = tmp_path / "person_presence.heartbeat.jsonl"
    process = PurposeInferenceProcess(
        task_id=PURPOSE_PERSON_PRESENCE,
        label="사람 존재 감지",
        command=("gst-launch-1.0", "-q", "fakesrc", "!", "fakesink"),
        env={},
        log_path=tmp_path / "person_presence.gst.log",
        event_path=tmp_path / "person_presence.jsonl",
        model_paths=(),
        camera_ids=("front",),
        diagnostic_path=diagnostic_path,
        diagnostic_startup_timeout_seconds=0.0,
    )

    class SilentProcess:
        pid = 54322
        returncode = None

        def poll(self):
            return None

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout=None):  # noqa: ANN001 - fake subprocess API.
            self.returncode = -15
            return self.returncode

    monkeypatch.setattr("towersightai.inference.purpose_tasks.shutil.which", lambda _command: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr("towersightai.inference.purpose_tasks.subprocess.Popen", lambda *_args, **_kwargs: SilentProcess())
    monkeypatch.setattr("towersightai.inference.purpose_tasks.os.killpg", lambda _pid, _sig: None)

    runner = PurposeInferenceRunner(process, on_events=lambda _events: None, on_error=errors.append, poll_seconds=0)

    assert runner.run() is True
    assert errors
    assert "heartbeat did not start" in errors[0]


def test_purpose_runner_replaces_stalled_process_and_confirms_recovery(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    statuses: list[str] = []
    diagnostic_path = tmp_path / "person_presence.heartbeat.jsonl"
    process = PurposeInferenceProcess(
        task_id=PURPOSE_PERSON_PRESENCE,
        label="사람 존재 감지",
        command=("gst-launch-1.0", "-q", "fakesrc", "!", "fakesink"),
        env={},
        log_path=tmp_path / "person_presence.gst.log",
        event_path=tmp_path / "person_presence.jsonl",
        model_paths=(),
        camera_ids=("front",),
        diagnostic_path=diagnostic_path,
        max_consecutive_restarts=1,
        restart_delay_seconds=0,
    )

    class FakeProcess:
        def __init__(self, pid: int, returncode: int | None) -> None:
            self.pid = pid
            self.returncode = returncode

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout=None):  # noqa: ANN001 - fake subprocess API.
            self.returncode = -15
            return self.returncode

    launches = 0

    def fake_popen(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        if launches == 1:
            payload = {
                "status": "stalled",
                "stale_cameras": ["front"],
                "cameras": {"front": {"inference_age_seconds": 16.0}},
            }
            child = FakeProcess(1001, None)
        else:
            payload = {
                "status": "running",
                "stale_cameras": [],
                "cameras": {"front": {"inference_age_seconds": 0.01}},
            }
            child = FakeProcess(1002, 0)
        diagnostic_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return child

    monkeypatch.setattr("towersightai.inference.purpose_tasks.shutil.which", lambda _command: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr("towersightai.inference.purpose_tasks.subprocess.Popen", fake_popen)
    monkeypatch.setattr("towersightai.inference.purpose_tasks.os.killpg", lambda _pid, _sig: None)

    runner = PurposeInferenceRunner(
        process,
        on_events=lambda _events: None,
        on_error=errors.append,
        on_status=statuses.append,
        poll_seconds=0,
    )

    assert runner.run() is True
    assert launches == 2
    assert errors == []
    assert statuses == ["AI 입력 스트림 복구 중 (1/1)", "AI 입력 스트림 복구 완료"]
    assert tuple(tmp_path.glob("person_presence.heartbeat.jsonl.*.restart-1.previous"))
    assert "PROCESS_RESTART attempt=1" in process.log_path.read_text(encoding="utf-8")


def test_runtime_log_archive_preserves_previous_run_and_redacts_credentials(tmp_path: Path):
    log_path = tmp_path / "person_presence.gst.log"
    log_path.write_text(
        'rtspsrc location="rtsp://operator:secret@camera.invalid/stream1"\n',
        encoding="utf-8",
    )

    archive = _archive_runtime_file(log_path, "next-run")

    assert archive == tmp_path / "person_presence.gst.log.next-run.previous"
    assert not log_path.exists()
    archived_text = archive.read_text(encoding="utf-8")
    assert "operator" not in archived_text
    assert "secret" not in archived_text
    assert "rtsp://***:***@camera.invalid/stream1" in archived_text


def test_read_last_text_line_does_not_require_loading_whole_history(tmp_path: Path):
    path = tmp_path / "heartbeat.jsonl"
    path.write_bytes((b"x" * 300_000) + b"\n{\"status\":\"stalled\"}\n")

    assert _read_last_text_line(path) == '{"status":"stalled"}'
