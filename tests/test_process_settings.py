import json
from pathlib import Path

import pytest

from towersightai.process.settings_store import (
    OperatorRuntimeSettings,
    PlateZoneSettings,
    VehicleTriggerSettings,
    WheelGuideSettings,
    load_operator_settings,
    save_operator_settings,
    settings_from_payload,
)


def test_missing_file_returns_defaults(tmp_path: Path):
    settings = load_operator_settings(tmp_path / "absent.json")
    assert settings == OperatorRuntimeSettings()
    assert settings.vehicle_trigger.min_confidence == 0.6
    assert settings.vehicle_trigger.consecutive_frames == 5
    assert settings.timers.exit_clear_seconds == 10.0
    assert settings.timers.machine_operation_seconds == 60.0
    assert settings.nas_upload_mode == "scheduled"


def test_corrupt_json_returns_defaults(tmp_path: Path, caplog):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="towersightai.process.settings"):
        settings = load_operator_settings(path)
    assert settings == OperatorRuntimeSettings()
    assert any("defaults applied" in record.message for record in caplog.records)


def test_invalid_values_return_defaults_not_crash(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"vehicle_trigger": {"min_confidence": 5.0}}), encoding="utf-8")
    assert load_operator_settings(path) == OperatorRuntimeSettings()


def test_partial_payload_fills_defaults_and_ignores_unknown_keys():
    settings = settings_from_payload(
        {
            "vehicle_trigger": {"min_confidence": 0.7, "mystery": 1},
            "unknown_section": {"a": 1},
            "nas_upload_mode": "immediate",
        }
    )
    assert settings.vehicle_trigger.min_confidence == 0.7
    assert settings.vehicle_trigger.consecutive_frames == 5
    assert settings.person_debounce.idle_frames == 2
    assert settings.nas_upload_mode == "immediate"


def test_round_trip_save_and_load(tmp_path: Path):
    path = tmp_path / "nested" / "settings.json"
    original = OperatorRuntimeSettings(
        vehicle_trigger=VehicleTriggerSettings(min_confidence=0.75, consecutive_frames=7, release_seconds=4.0),
        plate_zone=PlateZoneSettings(line_y_norm=0.4, min_reads_for_vote=5, read_interval_seconds=0.5),
        wheel_guides=WheelGuideSettings(left_x_norm=0.2, right_x_norm=0.8, top_left_x_norm=0.45, top_right_x_norm=0.55, top_y_norm=0.4, stop_y_norm=0.9),
        nas_upload_mode="immediate",
    )
    save_operator_settings(original, path)
    assert load_operator_settings(path) == original
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    # atomic write leaves no temp files behind
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


@pytest.mark.parametrize(
    "section, kwargs",
    [
        ("vehicle", {"min_confidence": 0.0}),
        ("vehicle", {"consecutive_frames": 0}),
        ("plate", {"line_y_norm": 1.5}),
        ("plate", {"max_reads": 1, "min_reads_for_vote": 3}),
        ("guides", {"left_x_norm": 0.7, "right_x_norm": 0.6}),
        # trapezoid must narrow toward the top: top corners inside the bottom corners
        ("guides", {"top_left_x_norm": 0.1}),   # top-left wider than bottom-left (0.30)
        ("guides", {"top_right_x_norm": 0.9}),  # top-right wider than bottom-right (0.70)
        ("guides", {"top_y_norm": 0.9}),        # top edge below the stop line (0.80)
    ],
)
def test_out_of_range_values_raise(section: str, kwargs: dict):
    cls = {"vehicle": VehicleTriggerSettings, "plate": PlateZoneSettings, "guides": WheelGuideSettings}[section]
    with pytest.raises(ValueError):
        cls(**kwargs)


def test_wheel_guides_default_is_a_narrowing_trapezoid():
    guides = WheelGuideSettings()
    assert guides.left_x_norm <= guides.top_left_x_norm
    assert guides.top_left_x_norm < guides.top_right_x_norm
    assert guides.top_right_x_norm <= guides.right_x_norm
    assert guides.top_y_norm < guides.stop_y_norm


def test_unknown_upload_mode_rejected():
    with pytest.raises(ValueError):
        OperatorRuntimeSettings(nas_upload_mode="sometimes")


def test_audio_enabled_defaults_true_and_round_trips(tmp_path: Path):
    assert OperatorRuntimeSettings().audio_enabled is True
    off = settings_from_payload({"audio_enabled": False})
    assert off.audio_enabled is False
    path = tmp_path / "s.json"
    save_operator_settings(off, path)
    assert load_operator_settings(path).audio_enabled is False
