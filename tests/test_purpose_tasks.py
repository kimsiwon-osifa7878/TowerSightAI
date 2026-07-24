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
    build_purpose_process,
    parse_plate_ocr_json,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        tappas_workspace=tmp_path / "tappas",
        hailo_model_dir=tmp_path / "models" / "hailo",
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
    assert (settings.hailo_model_dir / "vehicle_detection/yolov5m_vehicles.hef").as_posix() in command
    assert (settings.hailo_model_dir / "postprocess/libyolo_hailortpp_post.so").as_posix() in command
    assert "function-name=yolov5m_vehicles" in command
    assert "configs/yolov5_vehicle_detection.json" in command
    assert "hailopython" in command


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
    assert "gst-launch" not in command
    assert "yolov5m_vehicles.hef" not in command
    assert "tiny_yolov4_license_plates.hef" not in command
    assert "lprnet.hef" not in command
    assert "hailopython" not in command
    assert "liblpr_ocrsink.so" not in command
    manifest = json.loads((tmp_path / "events/lpr_image/lpr_manifest.json").read_text(encoding="utf-8"))
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

    class ALPR:
        def __init__(self, **_kwargs):
            pass

        def predict(self, _path):
            return [Result()]

    monkeypatch.setitem(sys.modules, "fast_alpr", types.SimpleNamespace(ALPR=ALPR))

    assert run_fast_alpr_lpr(
        image_dir=image_dir,
        event_path=event_path,
        log_path=log_path,
        manifest_path=manifest_path,
    ) == 0

    lines = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    plate_events = [line for line in lines if line["type"] == "plate_ocr"]
    attempts = [line for line in lines if line["type"] == "plate_ocr_attempt"]

    assert len(plate_events) == 2
    assert len(attempts) == 2
    assert all(attempt["status"] == "recognized" for attempt in attempts)
    assert all(attempt["elapsed_ms"] >= 0 for attempt in attempts)
    assert attempts[0]["best_plate"]["plate_number"] == "12가3456"
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
    assert (settings.hailo_model_dir / "person_presence/yolov5s_personface_reid.hef").as_posix() in command
    assert (settings.hailo_model_dir / "postprocess/libyolo_post.so").as_posix() in command
    assert (settings.hailo_model_dir / "postprocess/cropping_algorithms/libwhole_buffer.so").as_posix() in command
    assert command.count("video/x-raw,format=RGB,width=640,height=640,pixel-aspect-ratio=1/1") == 3
    assert "force-writable=true" in command
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
