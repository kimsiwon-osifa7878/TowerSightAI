"""Hailo device recovery: the PCIe re-enumerate that used to be typed over SSH.

Covers the shell helper's guards (dry-run only, no device state is touched by the tests) and the
Python wrapper that the operator console calls. Recovery is maintenance: nothing here may report
success as any kind of safety authorization.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from towersightai.inference.hailo_recovery import (
    DEFAULT_RECOVER_BIN,
    RecoveryResult,
    recover_hailo_device,
    recovery_command,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "hailo_recover.sh"


def _fake_helper(tmp_path: Path, body: str) -> tuple[str, ...]:
    path = tmp_path / "fake-recover"
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return (str(path),)


# ---- shell helper --------------------------------------------------------------------------


def test_helper_dry_run_reports_the_steps_without_changing_anything(tmp_path: Path):
    """--dry-run must print the plan and touch neither the driver nor the PCI device."""
    pci_root = tmp_path / "pci"
    device = pci_root / "devices" / "0000:02:00.0"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x1e60\n", encoding="ascii")

    result = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PCI_ROOT": str(pci_root), "DEVICE_NODE": str(tmp_path / "none")},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEVICE=0000:02:00.0" in result.stdout
    assert "RESULT=ok" in result.stdout
    for step in ("modprobe-remove", "pci-remove", "pci-rescan", "modprobe-load"):
        assert f"STEP={step}" in result.stdout
    # Every state-changing command must have been announced, never executed.
    assert "DRYRUN=modprobe -r hailo_pci" in result.stdout
    assert not (device / "remove").exists()


def test_helper_fails_clearly_when_no_hailo_device_is_present(tmp_path: Path):
    empty = tmp_path / "pci"
    (empty / "devices").mkdir(parents=True)

    result = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PCI_ROOT": str(empty), "DEVICE_NODE": str(tmp_path / "none")},
    )

    assert result.returncode == 1
    assert "RESULT=failed" in result.stdout
    assert "Hailo 장치를 찾지 못했습니다" in result.stdout


def test_helper_refuses_to_run_for_real_without_root():
    """The real path must never proceed unprivileged; only --dry-run is allowed."""
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "root 권한이 필요합니다" in result.stdout


def test_helper_and_installer_are_executable_shell():
    for name in ("hailo_recover.sh", "install_hailo_recover.sh"):
        path = ROOT / "tools" / name
        assert path.is_file() and subprocess.run(["bash", "-n", str(path)]).returncode == 0, name


# ---- python wrapper ------------------------------------------------------------------------


def test_recovery_command_is_a_single_passwordless_sudo_target(monkeypatch):
    monkeypatch.delenv("TOWERSIGHTAI_RECOVER_BIN", raising=False)
    assert recovery_command() == ("sudo", "-n", DEFAULT_RECOVER_BIN)
    monkeypatch.setenv("TOWERSIGHTAI_RECOVER_BIN", "/opt/custom-recover")
    assert recovery_command() == ("sudo", "-n", "/opt/custom-recover")


def test_successful_recovery_is_parsed_and_never_authorizes_operation(tmp_path: Path):
    command = _fake_helper(
        tmp_path,
        'echo "DEVICE=0000:02:00.0"; echo "STEP=modprobe-remove"; echo "STEP=identify"; '
        'echo "RESULT=ok"; echo "DETAIL=Device Architecture: HAILO8"; exit 0\n',
    )

    result = recover_hailo_device(command=command)

    assert isinstance(result, RecoveryResult)
    assert result.ok is True
    assert result.safe_to_operate is False  # restoring the chip is not a safety approval
    assert result.device == "0000:02:00.0"
    assert result.steps == ("modprobe-remove", "identify")
    assert "HAILO8" in result.detail
    assert result.summary().startswith("Hailo 장치 복구 성공")


def test_helper_failure_is_reported_with_its_own_reason(tmp_path: Path):
    command = _fake_helper(
        tmp_path, 'echo "RESULT=failed"; echo "DETAIL=hailo_pci 모듈을 내리지 못했습니다"; exit 1\n'
    )

    result = recover_hailo_device(command=command)

    assert result.ok is False
    assert "모듈을 내리지 못했습니다" in result.detail
    assert result.summary().startswith("Hailo 장치 복구 실패")


def test_missing_sudo_rule_explains_how_to_install_it(tmp_path: Path):
    command = _fake_helper(
        tmp_path, 'echo "sudo: a password is required" >&2; exit 1\n'
    )

    result = recover_hailo_device(command=command)

    assert result.ok is False
    assert "install_hailo_recover.sh" in result.detail


def test_timeout_and_missing_binary_are_reported_not_raised(tmp_path: Path):
    slow = _fake_helper(tmp_path, "sleep 5\n")
    timed_out = recover_hailo_device(command=slow, timeout_seconds=0.3)
    assert timed_out.ok is False and "끝나지 않았습니다" in timed_out.detail

    missing = recover_hailo_device(command=("/nonexistent/towersightai-recover",))
    assert missing.ok is False and missing.detail

    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    crashed = recover_hailo_device(command=("x",), runner=boom)
    assert crashed.ok is False and "OSError" in crashed.detail


# ---- automatic recovery policy --------------------------------------------------------------


def _policy(**overrides):
    from towersightai.inference.hailo_recovery import HailoAutoRecoveryPolicy

    values = {"enabled": True, "after_seconds": 120.0, "max_attempts": 3, "window_seconds": 3600.0}
    values.update(overrides)
    return HailoAutoRecoveryPolicy(**values)


def test_auto_recovery_waits_for_two_minutes_of_continuous_error():
    policy = _policy()
    assert policy.observe("ok", 0).should_recover is False
    assert policy.observe("error", 10).reason == "waiting"
    assert policy.observe("error", 100).should_recover is False  # 90 s of error: not yet
    decision = policy.observe("error", 131)  # 121 s
    assert decision.should_recover is True
    assert decision.reason == "error_persisted"
    assert decision.attempt == 1
    assert decision.error_seconds >= 120


def test_a_single_healthy_sample_clears_the_pending_error():
    policy = _policy()
    policy.observe("error", 0)
    policy.observe("error", 100)
    assert policy.observe("ok", 110).should_recover is False
    assert policy.pending_error_since is None
    # The clock restarts: 121 s after the *new* first error, not the old one.
    assert policy.observe("error", 120).should_recover is False
    assert policy.observe("error", 230).should_recover is False
    assert policy.observe("error", 245).should_recover is True


def test_degraded_status_alone_never_triggers_recovery():
    policy = _policy()
    for now in range(0, 600, 60):
        assert policy.observe("degraded", now).should_recover is False


def test_attempts_are_spaced_and_rate_limited_while_the_device_stays_down():
    """A permanently dead device must not produce a retry storm, but must not be abandoned either."""
    policy = _policy(max_attempts=3, window_seconds=3600.0)
    fired: list[float] = []
    now = 0.0
    while now < 10800:  # three hours of uninterrupted error, sampled every 30 s
        if policy.observe("error", now).should_recover:
            fired.append(now)
        now += 30

    assert fired[:3] == [120, 240, 360]  # first hour: capped at max_attempts
    # Attempts never come back-to-back: each one restarts the two-minute clock.
    assert all(b - a >= 120 for a, b in zip(fired, fired[1:]))
    # And never more than max_attempts inside any one-hour window.
    for start in fired:
        assert len([t for t in fired if start <= t < start + 3600]) <= 3
    # The budget ages out, so the device keeps getting chances at roughly 3/hour.
    assert 6 <= len(fired) <= 9, fired


def test_auto_recovery_can_be_switched_off_entirely():
    policy = _policy(enabled=False)
    assert policy.observe("error", 0).reason == "disabled"
    assert policy.observe("error", 9999).should_recover is False

    budgetless = _policy(max_attempts=0)
    budgetless.observe("error", 0)
    assert budgetless.observe("error", 200).reason == "attempts_exhausted"
