"""Operator-triggered Hailo device recovery (PCIe re-enumerate).

The site chip sometimes drops off the bus while the PCIe link itself stays healthy
(``HAILO_DRIVER_OPERATION_FAILED(36)`` / ``Device disconnected while opening device``). Reloading
the driver and re-enumerating the PCI device brings it back most of the time, which until now had
to be typed over SSH. ``tools/hailo_recover.sh`` does that as root; this module runs it through a
single narrow ``sudo`` entry and turns its ``RESULT=``/``DETAIL=`` lines into a result object.

Recovery is maintenance, not authorization: a successful run restores *inference capability* only.
It never touches the safety gate, the state machine, or PLC output, and the display stays NG until
monitoring proves itself again. The function never raises; every failure is reported.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

DEFAULT_RECOVER_BIN = "/usr/local/sbin/towersightai-hailo-recover"
DEFAULT_TIMEOUT_SECONDS = 90.0
_LOGGER = logging.getLogger("towersightai.hailo.recovery")


@dataclass(frozen=True)
class RecoveryResult:
    ok: bool
    detail: str
    steps: tuple[str, ...] = field(default_factory=tuple)
    device: str = ""
    returncode: int | None = None
    # Restoring the device proves nothing about parking-machine safety.
    safe_to_operate: bool = False

    def summary(self) -> str:
        if self.ok:
            return f"Hailo 장치 복구 성공: {self.detail}"
        return f"Hailo 장치 복구 실패: {self.detail}"


def recovery_command(binary: str | None = None) -> tuple[str, ...]:
    """The exact command the sudoers rule allows. ``-n`` so it can never wait for a password."""
    target = binary or os.environ.get("TOWERSIGHTAI_RECOVER_BIN") or DEFAULT_RECOVER_BIN
    return ("sudo", "-n", target)


def _parse(output: str) -> tuple[str, str, tuple[str, ...], str]:
    result = detail = device = ""
    steps: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("RESULT="):
            result = line[len("RESULT=") :]
        elif line.startswith("DETAIL="):
            detail = line[len("DETAIL=") :]
        elif line.startswith("STEP="):
            steps.append(line[len("STEP=") :])
        elif line.startswith("DEVICE="):
            device = line[len("DEVICE=") :]
    return result, detail, tuple(steps), device


def recover_hailo_device(
    *,
    command: tuple[str, ...] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner=subprocess.run,  # noqa: ANN001 - injected for tests.
) -> RecoveryResult:
    """Reload the Hailo driver and re-enumerate the device. Never raises."""
    argv = list(command or recovery_command())
    try:
        completed = runner(argv, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _LOGGER.error("hailo-recovery-timeout after %.0fs", timeout_seconds)
        return RecoveryResult(False, f"복구가 {timeout_seconds:.0f}초 안에 끝나지 않았습니다")
    except FileNotFoundError:
        return RecoveryResult(False, "sudo 를 찾을 수 없습니다")
    except OSError as exc:  # noqa: BLE001 - reported, never raised.
        return RecoveryResult(False, f"복구 실행 실패: {type(exc).__name__}: {exc}")

    output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    status, detail, steps, device = _parse(output)
    if not status:
        # The helper never ran (missing binary, or sudo refused without a password).
        stderr = (completed.stderr or "").strip().splitlines()
        hint = stderr[-1][:160] if stderr else f"종료코드 {completed.returncode}"
        if "password" in hint.lower() or "sudo:" in hint.lower():
            hint += " · tools/install_hailo_recover.sh 를 먼저 실행했는지 확인하세요"
        return RecoveryResult(False, hint, returncode=completed.returncode)

    ok = status == "ok" and completed.returncode == 0
    _LOGGER.log(
        logging.INFO if ok else logging.ERROR,
        "hailo-recovery ok=%s device=%s steps=%s detail=%s",
        ok, device, ",".join(steps), detail,
    )
    return RecoveryResult(ok, detail or status, steps=steps, device=device, returncode=completed.returncode)


@dataclass(frozen=True)
class AutoRecoveryDecision:
    should_recover: bool
    reason: str
    attempt: int = 0
    error_seconds: float = 0.0


class HailoAutoRecoveryPolicy:
    """Decide when a stuck Hailo device should be re-enumerated without an operator.

    Rules, chosen so a *hardware* problem is never hidden by an endless retry loop:

    * Only after the health monitor has reported ``error`` continuously for
      ``after_seconds`` (default 120 s). A single bad probe is not enough.
    * Each attempt restarts that clock, so two attempts are always ``after_seconds`` apart.
    * At most ``max_attempts`` inside ``window_seconds`` (default 3 per hour). When the budget is
      spent the device stays down and the operator has to look, which is the correct outcome for a
      failing M.2 contact or an unstable supply.
    * Any healthy sample clears the pending error; the attempt budget still ages out on its own.

    Pure and clock-injected. Deciding to recover is maintenance, never an authorization: the
    safety gate keeps its own state and final OK stays blocked throughout.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        after_seconds: float = 120.0,
        max_attempts: int = 3,
        window_seconds: float = 3600.0,
    ) -> None:
        self.enabled = enabled
        self.after_seconds = after_seconds
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._error_since: float | None = None
        self._attempts: list[float] = []

    @property
    def pending_error_since(self) -> float | None:
        return self._error_since

    def recent_attempts(self, now: float) -> int:
        return len([stamp for stamp in self._attempts if now - stamp < self.window_seconds])

    def observe(self, status: str, now: float) -> AutoRecoveryDecision:
        """Feed one health status; returns whether recovery should run right now."""
        if status != "error":
            self._error_since = None
            return AutoRecoveryDecision(False, "healthy" if status == "ok" else status)
        if not self.enabled:
            return AutoRecoveryDecision(False, "disabled")
        if self._error_since is None:
            self._error_since = now
        elapsed = now - self._error_since
        if elapsed < self.after_seconds:
            return AutoRecoveryDecision(False, "waiting", error_seconds=elapsed)
        used = self.recent_attempts(now)
        if used >= self.max_attempts:
            return AutoRecoveryDecision(False, "attempts_exhausted", attempt=used, error_seconds=elapsed)
        # Restart the error clock so the next attempt is another after_seconds away.
        self._error_since = now
        self._attempts.append(now)
        return AutoRecoveryDecision(True, "error_persisted", attempt=used + 1, error_seconds=elapsed)
