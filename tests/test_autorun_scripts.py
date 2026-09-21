"""The boot-autostart helper scripts (install/uninstall/start/stop_autorun.sh).

They only manage a systemd *user* service that launches ``run.sh``; nothing here touches safety
state, the engine, or PLC output. The tests run the real scripts against a fake ``systemctl`` and
a temporary unit directory, so no service is installed on the machine running the suite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "towersightai-test.service"


@pytest.fixture
def harness(tmp_path: Path):
    """A fake systemctl that records its arguments, plus an isolated unit directory."""
    calls = tmp_path / "systemctl-calls.txt"
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'case "$*" in\n'
        '  *is-enabled*) echo enabled ;;\n'
        '  *is-active*) echo active ;;\n'
        'esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    unit_dir = tmp_path / "systemd-user"
    env = {
        **os.environ,
        "SYSTEMCTL": str(fake),
        "TOWERSIGHTAI_SYSTEMD_USER_DIR": str(unit_dir),
        "TOWERSIGHTAI_SERVICE_NAME": SERVICE,
    }

    def run(script: str):
        return subprocess.run(
            [str(ROOT / script)], capture_output=True, text=True, env=env, cwd=tmp_path
        )

    def recorded() -> list[str]:
        return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []

    return run, recorded, unit_dir / SERVICE


def test_install_writes_a_user_unit_that_launches_run_sh(harness):
    run, recorded, unit_path = harness

    result = run("install_autorun.sh")

    assert result.returncode == 0, result.stderr
    unit = unit_path.read_text(encoding="utf-8")
    # Launches the repo's own entry point with absolute paths (no reliance on the caller's cwd).
    assert f"ExecStart={ROOT}/run.sh" in unit
    assert f"WorkingDirectory={ROOT}" in unit
    # GUI app: bound to the desktop session, not started headless at boot.
    assert "After=graphical-session.target" in unit
    assert "PartOf=graphical-session.target" in unit
    assert "WantedBy=graphical-session.target" in unit
    # A monitoring app must not stay down; children are cleaned up with the unit.
    assert "Restart=always" in unit
    assert "TimeoutStopSec=30" in unit
    # StartLimitIntervalSec belongs to [Unit]; systemd silently ignores it in the service
    # section. Split on the section header itself, not on the word inside a comment.
    unit_section = unit.split("\n[Service]", 1)[0]
    assert "StartLimitIntervalSec=0" in unit_section

    commands = recorded()
    assert "--user daemon-reload" in commands
    assert f"--user enable {SERVICE}" in commands
    assert not any("start" in line and "daemon" not in line for line in commands)  # install ≠ start
    assert "등록 완료" in result.stdout


def test_start_and_stop_only_touch_the_registered_service(harness):
    run, recorded, _unit_path = harness
    run("install_autorun.sh")

    start = run("start_autorun.sh")
    assert start.returncode == 0, start.stderr
    assert f"--user start {SERVICE}" in recorded()
    assert "부팅 시 자동 실행: enabled" in start.stdout

    stop = run("stop_autorun.sh")
    assert stop.returncode == 0, stop.stderr
    assert f"--user stop {SERVICE}" in recorded()
    # Stopping must not unregister: the unit file stays.
    assert f"--user disable {SERVICE}" not in recorded()


def test_start_and_stop_refuse_when_nothing_is_registered(harness):
    run, recorded, unit_path = harness

    for script in ("start_autorun.sh", "stop_autorun.sh"):
        result = run(script)
        assert result.returncode == 3
        assert "등록되어 있지 않습니다" in result.stderr
        assert "install_autorun.sh" in result.stderr
    assert not unit_path.exists()
    assert not any("start" in line or "stop" in line for line in recorded())


def test_uninstall_disables_and_removes_the_unit(harness):
    run, recorded, unit_path = harness
    run("install_autorun.sh")
    assert unit_path.exists()

    result = run("uninstall_autorun.sh")

    assert result.returncode == 0, result.stderr
    assert not unit_path.exists()
    assert f"--user disable --now {SERVICE}" in recorded()
    assert recorded().count("--user daemon-reload") == 2  # install + uninstall
    assert "등록 해제 완료" in result.stdout

    # Running it again on a clean system is not an error.
    again = run("uninstall_autorun.sh")
    assert again.returncode == 0
    assert "등록되어 있지 않습니다" in again.stdout


def test_generated_unit_is_accepted_by_systemd(harness):
    """systemd-analyze catches misplaced keys that would otherwise be ignored in silence."""
    run, _recorded, unit_path = harness
    run("install_autorun.sh")
    verify = subprocess.run(
        ["systemd-analyze", "verify", str(unit_path)], capture_output=True, text=True
    )
    if verify.returncode != 0 and "command not found" in (verify.stderr or ""):
        pytest.skip("systemd-analyze unavailable")
    complaints = [
        line for line in (verify.stderr or "").splitlines() if unit_path.name in line
    ]
    assert complaints == [], complaints


def test_scripts_are_executable_and_live_in_the_repo_root():
    for name in (
        "install_autorun.sh",
        "uninstall_autorun.sh",
        "start_autorun.sh",
        "stop_autorun.sh",
        "autorun-common.sh",
    ):
        path = ROOT / name
        assert path.is_file(), name
        assert os.access(path, os.X_OK), name
        assert subprocess.run(["bash", "-n", str(path)]).returncode == 0, name
