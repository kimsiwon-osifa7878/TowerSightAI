import importlib.util
import sys
from pathlib import Path


def _load_recorder():
    path = Path(__file__).resolve().parents[1] / "towersightai" / "cli" / "event_video_recorder.py"
    spec = importlib.util.spec_from_file_location("event_video_recorder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_recorder_parent_changed_and_death_signal():
    recorder = _load_recorder()
    assert recorder.parent_changed(10, lambda: 10) is False
    assert recorder.parent_changed(10, lambda: 1) is True
    armed = recorder.arm_parent_death_signal()
    assert armed is (sys.platform == "linux")
    if armed:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, 0, 0, 0, 0)  # disarm for the test runner
    assert recorder.STOP_GRACE_SECONDS > 0
