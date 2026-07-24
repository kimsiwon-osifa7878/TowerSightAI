from pathlib import Path

from towersightai.config.settings import Settings
from towersightai.inference.hailo_check import check_hailo_installation


def _settings(tmp_path: Path) -> Settings:
    hef = tmp_path / "model.hef"
    so = tmp_path / "post.so"
    vehicle_hef = tmp_path / "vehicle.hef"
    vehicle_config = tmp_path / "vehicle.json"
    person_hef = tmp_path / "person.hef"
    person_config = tmp_path / "person.json"
    person_so = tmp_path / "person.so"
    crop_so = tmp_path / "crop.so"
    for path in (hef, so, vehicle_hef, vehicle_config, person_hef, person_config, person_so, crop_so):
        path.write_text("resource", encoding="utf-8")
    return Settings(
        tappas_workspace=tmp_path,
        hailo_hef_path=hef,
        hailo_postprocess_so=so,
        hailo_vehicle_detection_hef_path=vehicle_hef,
        hailo_vehicle_detection_config_path=vehicle_config,
        hailo_vehicle_detection_postprocess_so=so,
        hailo_person_presence_hef_path=person_hef,
        hailo_person_presence_config_path=person_config,
        hailo_person_presence_postprocess_so=person_so,
        hailo_person_presence_crop_so=crop_so,
        camera_1={"id": "front", "role": "front", "rtsp_url": "rtsp://a"},
        camera_2={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://b"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://c"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://d"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_hailo_check_reports_missing_tools(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = check_hailo_installation(_settings(tmp_path))

    assert result.ok is False
    assert result.items[0].name == "hailortcli"
    assert result.items[0].ok is False
    assert any(item.name == "HEF path" and item.ok for item in result.items)


def test_hailo_check_uses_subprocess_for_tools(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    calls = []

    def fake_run(command, check, capture_output, text, timeout):
        calls.append(command)
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = check_hailo_installation(_settings(tmp_path))

    assert result.ok is True
    assert ["hailortcli", "fw-control", "identify"] in calls
    assert ["gst-inspect-1.0", "hailonet"] in calls
