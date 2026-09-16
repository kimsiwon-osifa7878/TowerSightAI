import threading
import time

from towersightai.cli.hailo_apps_detection import parent_changed, start_parent_watchdog


def test_parent_changed_only_when_ppid_differs():
    assert parent_changed(100, lambda: 100) is False
    assert parent_changed(100, lambda: 1) is True


def test_watchdog_fires_once_when_the_parent_disappears():
    ppid = {"value": 4242}
    fired = threading.Event()
    calls = []

    def on_orphaned() -> None:
        calls.append(time.monotonic())
        fired.set()

    stop = start_parent_watchdog(on_orphaned, interval_seconds=0.02, getppid=lambda: ppid["value"])
    time.sleep(0.1)
    assert not fired.is_set()  # parent alive → nothing happens
    ppid["value"] = 1  # re-parented to init/systemd
    assert fired.wait(2.0)
    time.sleep(0.1)
    assert len(calls) == 1
    stop.set()


def test_watchdog_can_be_stopped_before_the_parent_dies():
    ppid = {"value": 7}
    calls = []
    stop = start_parent_watchdog(calls.append, interval_seconds=0.02, getppid=lambda: ppid["value"])
    stop.set()
    time.sleep(0.1)
    ppid["value"] = 1
    time.sleep(0.1)
    assert calls == []


def test_parent_death_signal_arms_on_linux():
    import sys

    from towersightai.cli.hailo_apps_detection import arm_parent_death_signal

    armed = arm_parent_death_signal()
    assert armed is (sys.platform == "linux")
    if armed:
        # Reset so the test runner is not killed when this test's parent thread exits.
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, 0, 0, 0, 0)


def test_arming_after_the_parent_already_died_exits_immediately():
    from towersightai.cli import hailo_apps_detection as child

    calls = []
    armed = child.arm_parent_death_signal(startup_ppid=4242, getppid=lambda: 853, on_orphaned=lambda: calls.append("exit"))
    if armed:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, 0, 0, 0, 0)
        assert calls == ["exit"]  # re-parented to systemd --user (853), not pid 1, still detected
    calls.clear()
    armed = child.arm_parent_death_signal(startup_ppid=4242, getppid=lambda: 4242, on_orphaned=lambda: calls.append("exit"))
    if armed:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, 0, 0, 0, 0)
        assert calls == []
    assert isinstance(child.STARTUP_PPID, int)
