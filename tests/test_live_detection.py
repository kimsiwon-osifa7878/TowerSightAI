from __future__ import annotations

from pathlib import Path

from towersightai.config.settings import Settings
from towersightai.inference.live_detection import (
    DetectionFileTail,
    build_live_detection_pipeline,
    build_live_multistream_detection_pipeline,
    live_detection_process,
    live_multistream_detection_process,
    parse_detection_json,
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
        camera_1={"id": "ceiling", "role": "ceiling", "rtsp_url": "rtsp://user:secret@192.0.2.10/stream1"},
        camera_2={"id": "front", "role": "front", "rtsp_url": "rtsp://user:secret@192.0.2.11/stream1"},
        camera_3={"id": "rear_side", "role": "rear_side", "rtsp_url": "rtsp://user:secret@192.0.2.12/stream1"},
        camera_4={"id": "opposite_side", "role": "opposite_side", "rtsp_url": "rtsp://user:secret@192.0.2.13/stream1"},
        calibration_path=tmp_path / "calibration.json",
        plc_endpoint="tcp://127.0.0.1:502",
    )


def test_live_detection_pipeline_uses_rtsp_hailo_and_callback(tmp_path: Path):
    settings = _settings(tmp_path)

    pipeline = build_live_detection_pipeline(settings, settings.camera_2, min_confidence=0.42)

    assert "rtspsrc location=rtsp://user:secret@192.0.2.11/stream1 latency=100 protocols=tcp" in pipeline
    assert "videoscale add-borders=true n-threads=2" in pipeline
    assert f"hailonet hef-path={settings.hailo_hef_path} batch-size=1 nms-score-threshold=0.42" in pipeline
    assert f"hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so}" in pipeline
    assert "hailopython module=towersightai/inference/callback.py" in pipeline
    assert "fakesink sync=false" in pipeline


def test_live_detection_pipeline_default_threshold_is_low_for_operator_overlay(tmp_path: Path):
    settings = _settings(tmp_path)

    pipeline = build_live_detection_pipeline(settings, settings.camera_1)

    assert "nms-score-threshold=0.1" in pipeline


def test_live_detection_pipeline_uses_ui_rotation_before_hailo(tmp_path: Path):
    settings = _settings(tmp_path)

    default_pipeline = build_live_detection_pipeline(settings, settings.camera_1)
    rotated_pipeline = build_live_detection_pipeline(settings, settings.camera_1, rotation_degrees=90)

    assert "videoflip" not in default_pipeline
    assert "decodebin ! queue" in rotated_pipeline
    assert "videoflip method=counterclockwise ! videoscale add-borders=true" in rotated_pipeline


def test_live_detection_pipeline_uses_camera_config_default_rotation(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.camera_1 = settings.camera_1.__class__(
        id=settings.camera_1.id,
        role=settings.camera_1.role,
        rtsp_url=settings.camera_1.rtsp_url,
        username=settings.camera_1.username,
        password=settings.camera_1.password,
        rotation_degrees=90,
    )

    pipeline = build_live_detection_pipeline(settings, settings.camera_1)

    assert "videoflip method=counterclockwise ! videoscale add-borders=true" in pipeline


def test_live_detection_process_sets_camera_event_sink_and_tappas_env(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    tappas_bin = tmp_path / "hailo_tappas_venv" / "bin"
    tappas_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    process = live_detection_process(settings, settings.camera_1, event_dir=tmp_path / "events")

    assert process.event_path == tmp_path / "events" / "ceiling.jsonl"
    assert process.env["TOWERSIGHTAI_HAILO_CAMERA_ID"] == "ceiling"
    assert process.env["TOWERSIGHTAI_HAILO_EVENT_PATH"] == str(process.event_path)
    assert process.env["VIRTUAL_ENV"] == str(tmp_path / "hailo_tappas_venv")
    assert process.env["PATH"].startswith(str(tappas_bin))


def test_live_multistream_detection_pipeline_routes_each_camera_to_callback(tmp_path: Path):
    settings = _settings(tmp_path)
    callbacks = {
        "ceiling": tmp_path / "callback_ceiling.py",
        "front": tmp_path / "callback_front.py",
    }

    pipeline = build_live_multistream_detection_pipeline(
        settings,
        (settings.camera_1, settings.camera_2),
        callback_modules=callbacks,
        min_confidence=0.42,
        camera_rotations={"ceiling": 90},
    )

    assert "hailoroundrobin mode=0 name=fun" in pipeline
    assert "hailostreamrouter name=sid src_0::input-streams=\"<sink_0>\" src_1::input-streams=\"<sink_1>\"" in pipeline
    assert "fun.sink_0 sid.src_0" in pipeline
    assert "fun.sink_1 sid.src_1" in pipeline
    assert f"hailopython module={callbacks['ceiling']} qos=false" in pipeline
    assert f"hailopython module={callbacks['front']} qos=false" in pipeline
    assert "rtspsrc location=rtsp://user:secret@192.0.2.10/stream1 name=source_0" in pipeline
    assert "rtspsrc location=rtsp://user:secret@192.0.2.11/stream1 name=source_1" in pipeline
    assert "hailo_preprocess_q_0 leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! videoflip method=counterclockwise ! videoscale" in pipeline
    assert "hailo_preprocess_q_1 leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! videoflip" not in pipeline
    assert f"hailonet hef-path={settings.hailo_hef_path} batch-size=1 nms-score-threshold=0.42" in pipeline


def test_live_multistream_detection_process_writes_camera_specific_callbacks(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    tappas_bin = tmp_path / "hailo_tappas_venv" / "bin"
    tappas_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    process = live_multistream_detection_process(
        settings,
        (settings.camera_1, settings.camera_2),
        event_dir=tmp_path / "events",
    )

    assert process.event_path == tmp_path / "events" / "multistream.jsonl"
    assert process.env["TOWERSIGHTAI_HAILO_EVENT_PATH"] == str(process.event_path)
    assert process.env["VIRTUAL_ENV"] == str(tmp_path / "hailo_tappas_venv")
    ceiling_callback = tmp_path / "events" / "callback_ceiling.py"
    front_callback = tmp_path / "events" / "callback_front.py"
    assert "camera_id='ceiling'" in ceiling_callback.read_text(encoding="utf-8")
    assert "camera_id='front'" in front_callback.read_text(encoding="utf-8")
    assert f"event_path='{process.event_path}'" in ceiling_callback.read_text(encoding="utf-8")
    assert f"hailopython module={ceiling_callback}" in " ".join(process.command)
    assert f"hailopython module={front_callback}" in " ".join(process.command)


def test_parse_detection_json_and_tail_reads_only_new_events(tmp_path: Path):
    path = tmp_path / "detections.jsonl"
    path.write_text(
        '{"bbox":{"x":0.1,"y":0.2,"w":0.3,"h":0.4},"camera_id":"front","confidence":0.91,'
        '"label":"car","timestamp":"2026-05-19T05:56:42+00:00","source":"hailo"}\n',
        encoding="utf-8",
    )
    tail = DetectionFileTail(path)

    first = tail.read_new_events()
    second = tail.read_new_events()

    assert len(first) == 1
    assert first[0].camera_id == "front"
    assert first[0].label == "car"
    assert second == ()
    assert parse_detection_json("not-json") is None
