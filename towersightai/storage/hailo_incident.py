"""Hailo failure evidence → NAS, so a hang can be diagnosed without site access.

When the health monitor turns ``degraded``/``error`` this module collects the same read-only
evidence that field triage needs (PCIe link speed + AER counters, driver/device node, kernel
messages, device holders, a *snapshot copy* of the runtime log tail and the newest inference child
log) and uploads it to ``<SYNOLOGY_NAS_FOLDER>/hailo-incidents/<host>-<UTC stamp>/``.

Hard rules:

- **Read-only.** Nothing here removes, rescans, reloads, kills, or restarts anything. Every
  subprocess has a timeout so a wedged device cannot block the health thread.
- **Diagnostic only.** An incident report never changes safety state, calibration state, the
  process engine, or PLC output, and it can never make ``can_show_final_ok`` true.
- **Never raises.** Collection and upload failures are reported in the result and the log.
- Credentials are redacted before anything is written or uploaded.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from towersightai.config.settings import RawStorageConfig
from towersightai.inference.hailo_health import (
    DEFAULT_DEVICE_NODE,
    DEFAULT_MODULE_ROOT,
    DEFAULT_PCI_ROOT,
    find_hailo_pci_device,
    find_stale_children,
)
from towersightai.runtime_logging import redact_sensitive_text
from towersightai.storage.file_transfer import NasFileTransferResult, upload_files_to_nas

INCIDENT_ROOT = "hailo-incidents"
DEFAULT_WORK_DIR = Path("artifacts/runtime/hailo-incidents")
DEFAULT_RUNTIME_LOG = Path("artifacts/runtime/towersightai.log")
DEFAULT_PURPOSE_AI_DIR = Path("artifacts/runtime/purpose-ai")
LOG_TAIL_BYTES = 512 * 1024
CHILD_LOG_TAIL_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 15.0
_LOGGER = logging.getLogger("towersightai.hailo.incident")

# Read-only commands only. Anything that could change device or driver state is forbidden here.
_KERNEL_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("dmesg", "-T"),
    ("journalctl", "-k", "--no-pager", "-n", "2000"),
)
_KERNEL_KEYWORDS = ("hailo", "pcieport", "aer", "02:00")

CommandRunner = Callable[[Sequence[str]], str]


def _run_command(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout after {COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(command)}]"
    except OSError as exc:
        return f"[unavailable: {' '.join(command)}: {exc}]"
    return result.stdout or result.stderr or ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"[unreadable: {exc}]"


def _tail_bytes(path: Path, max_bytes: int) -> str:
    """Copy the tail of a file that another thread is still writing (snapshot, never a live read).

    The operator's manual upload of the live log failed remote SHA-256 verification because the
    file grew during the transfer; the bundle always uploads a frozen copy instead.
    """
    try:
        with path.open("rb") as fp:
            fp.seek(0, 2)
            size = fp.tell()
            fp.seek(max(0, size - max_bytes))
            return fp.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"[unreadable: {exc}]"


def incident_name(*, host: str | None = None, at: datetime | None = None) -> str:
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return f"{host or socket.gethostname()}-{stamp}"


def remote_incident_dir(config: RawStorageConfig, name: str) -> str:
    import posixpath

    return posixpath.join(config.nas_folder.rstrip("/"), INCIDENT_ROOT, name)


def _pcie_report(pci_root: Path) -> str:
    address, device_dir = find_hailo_pci_device(pci_root)
    if device_dir is None:
        return f"hailo pci device: not found under {pci_root}"
    try:
        parent_dir = device_dir.resolve().parent
    except OSError:
        parent_dir = device_dir
    lines = [f"hailo pci device: {address}", f"upstream port: {parent_dir.name}", ""]
    for label, directory in (("endpoint", device_dir), ("upstream port", parent_dir)):
        lines.append(f"== {label} ({directory.name})")
        for name in (
            "current_link_speed",
            "current_link_width",
            "max_link_speed",
            "max_link_width",
            "aer_dev_correctable",
            "aer_dev_nonfatal",
            "aer_dev_fatal",
        ):
            target = directory / name
            if not target.exists():
                continue
            value = _read_text(target).replace("\n", " ")
            lines.append(f"{name}: {value}")
        lines.append("")
    return "\n".join(lines)


def _driver_report(device_node: Path, module_root: Path) -> str:
    module_dir = module_root / "hailo_pci"
    holders = find_stale_children()
    lines = [
        f"hailo_pci loaded: {'yes' if module_dir.is_dir() else 'no'}",
        f"hailo_pci version: {_read_text(module_dir / 'version') if module_dir.is_dir() else 'n/a'}",
        f"{device_node}: {'present' if device_node.exists() else 'missing'}",
        "",
        "== device holders / orphaned children",
    ]
    lines.extend(holder.describe() for holder in holders)
    if not holders:
        lines.append("none")
    return "\n".join(lines)


def _kernel_report(runner: CommandRunner) -> str:
    for command in _KERNEL_COMMANDS:
        output = runner(command)
        if not output or output.startswith("["):
            continue
        matched = [
            line
            for line in output.splitlines()
            if any(keyword in line.lower() for keyword in _KERNEL_KEYWORDS)
        ]
        if matched:
            return f"$ {' '.join(command)}\n" + "\n".join(matched[-120:])
    return "[no kernel messages available: dmesg needs privileges and journalctl found nothing]"


def _child_log_report(purpose_ai_dir: Path) -> str:
    logs = sorted(purpose_ai_dir.glob("*/*.gst.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not logs:
        return "[no inference child log found]"
    newest = logs[0]
    tail = _tail_bytes(newest, CHILD_LOG_TAIL_BYTES)
    kept = [line for line in tail.splitlines() if "PIPELINE_DIAGNOSTIC" not in line]
    return f"== {newest}\n" + "\n".join(kept[-200:])


def build_incident_bundle(
    out_dir: Path,
    snapshot: Any,
    *,
    reason: str = "status_change",
    runtime_log: Path = DEFAULT_RUNTIME_LOG,
    purpose_ai_dir: Path = DEFAULT_PURPOSE_AI_DIR,
    device_node: Path = DEFAULT_DEVICE_NODE,
    pci_root: Path = DEFAULT_PCI_ROOT,
    module_root: Path = DEFAULT_MODULE_ROOT,
    runner: CommandRunner = _run_command,
    at: datetime | None = None,
) -> tuple[Path, ...]:
    """Write the read-only evidence files into ``out_dir``. Never raises."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    collected_at = at or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "kind": "hailo_incident",
        "reason": reason,
        "collected_at": collected_at.isoformat(),
        "source_host": socket.gethostname(),
        "health": snapshot_to_dict(snapshot),
        # Evidence for a human, never an authorization for anything.
        "safe_to_operate": False,
    }
    sections: list[tuple[str, str]] = [
        ("00-summary.json", json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)),
    ]
    for name, builder in (
        ("01-pcie.txt", lambda: _pcie_report(pci_root)),
        ("02-driver.txt", lambda: _driver_report(device_node, module_root)),
        ("03-kernel.txt", lambda: _kernel_report(runner)),
        ("04-identify.txt", lambda: runner(("hailortcli", "fw-control", "identify"))),
        ("05-app-log.txt", lambda: _tail_bytes(Path(runtime_log), LOG_TAIL_BYTES)),
        ("06-child-log.txt", lambda: _child_log_report(Path(purpose_ai_dir))),
    ):
        try:
            sections.append((name, builder()))
        except Exception as exc:  # noqa: BLE001 - one unreadable source must not lose the bundle.
            _LOGGER.exception("hailo-incident-section-failed name=%s", name)
            sections.append((name, f"[collection failed: {type(exc).__name__}: {exc}]"))

    written: list[Path] = []
    for name, text in sections:
        path = out_dir / name
        try:
            path.write_text(redact_sensitive_text(text) + "\n", encoding="utf-8")
        except OSError:
            _LOGGER.exception("hailo-incident-write-failed name=%s", name)
            continue
        written.append(path)
    return tuple(written)


def snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    """Flatten a HailoHealthSnapshot for JSON (accepts any object with the same attributes)."""
    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    checked_at = getattr(snapshot, "checked_at", None)
    holders = getattr(snapshot, "device_holders", ()) or ()
    return {
        "status": getattr(snapshot, "status", ""),
        "summary": getattr(snapshot, "summary", ""),
        "checked_at": checked_at.isoformat() if hasattr(checked_at, "isoformat") else None,
        "pcie_address": getattr(snapshot, "pcie_address", ""),
        "pcie_parent": getattr(snapshot, "pcie_parent", ""),
        "driver_loaded": getattr(snapshot, "driver_loaded", None),
        "driver_version": getattr(snapshot, "driver_version", ""),
        "device_node_exists": getattr(snapshot, "device_node_exists", None),
        "rxerr_count": getattr(snapshot, "rxerr_count", None),
        "rxerr_delta": getattr(snapshot, "rxerr_delta", 0),
        "chip_temperature_c": getattr(snapshot, "chip_temperature_c", None),
        "detail": redact_sensitive_text(getattr(snapshot, "detail", "") or ""),
        "device_holders": [holder.describe() for holder in holders],
        "safety_effect": "raw_only",
    }


@dataclass(frozen=True)
class IncidentReport:
    reported: bool
    reason: str = ""
    local_dir: Path | None = None
    remote_dir: str = ""
    file_count: int = 0
    error: str = ""
    # A report is evidence for a human; it never authorizes parking-machine operation.
    safe_to_operate: bool = False

    def summary(self) -> str:
        if not self.reported:
            return f"Hailo 진단 자료 업로드 안 함 ({self.reason})"
        if self.error:
            return f"Hailo 진단 자료 NAS 업로드 실패: {self.error}"
        return f"Hailo 진단 자료 NAS 업로드 완료: {self.remote_dir} ({self.file_count}개 파일)"


class HailoIncidentReporter:
    """Collect and upload one evidence bundle when Hailo health goes bad.

    Throttled: one bundle per transition into a bad status, and at most one per
    ``config.hailo_incident_min_interval_seconds`` while it stays bad, so a device that is down
    for hours cannot flood the NAS.
    """

    BAD_STATUSES = frozenset({"error", "degraded"})

    def __init__(
        self,
        config: RawStorageConfig,
        *,
        work_dir: Path = DEFAULT_WORK_DIR,
        runtime_log: Path = DEFAULT_RUNTIME_LOG,
        purpose_ai_dir: Path = DEFAULT_PURPOSE_AI_DIR,
        uploader: Callable[..., NasFileTransferResult] | None = None,
        builder: Callable[..., tuple[Path, ...]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.work_dir = Path(work_dir)
        self.runtime_log = Path(runtime_log)
        self.purpose_ai_dir = Path(purpose_ai_dir)
        self._uploader = uploader or upload_files_to_nas
        self._builder = builder or build_incident_bundle
        self._clock = clock
        self._now = now
        self._last_report_at: float | None = None
        self._last_status = "ok"

    @property
    def enabled(self) -> bool:
        """Upload only when the NAS archive itself is configured and verified in use.

        ``config.enabled`` (RAW_DATA_ENABLED) is the site's own statement that this NAS path is
        real; without it there is nothing to upload to, and an unconfigured or test host must
        never cause a blocking SFTP attempt from the health thread.
        """
        return bool(
            self.config.enabled
            and self.config.hailo_incident_upload_enabled
            and self.config.nas_host
        )

    def observe(self, snapshot: Any) -> IncidentReport:
        """Feed one health snapshot; upload a bundle when it newly goes bad (or after the interval)."""
        status = str(getattr(snapshot, "status", "") or "")
        previous, self._last_status = self._last_status, status
        if status not in self.BAD_STATUSES:
            return IncidentReport(False, reason="status_ok")
        if not self.enabled:
            return IncidentReport(False, reason="upload_disabled")
        changed = previous not in self.BAD_STATUSES
        now = self._clock()
        if not changed:
            if (
                self._last_report_at is not None
                and now - self._last_report_at < self.config.hailo_incident_min_interval_seconds
            ):
                return IncidentReport(False, reason="throttled")
        self._last_report_at = now
        return self._report(snapshot, reason="status_change" if changed else "still_failing")

    def _report(self, snapshot: Any, *, reason: str) -> IncidentReport:
        name = incident_name(at=self._now())
        local_dir = self.work_dir / name
        try:
            files = self._builder(
                local_dir,
                snapshot,
                reason=reason,
                runtime_log=self.runtime_log,
                purpose_ai_dir=self.purpose_ai_dir,
                at=self._now(),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break the health monitor.
            _LOGGER.exception("hailo-incident-build-failed")
            return IncidentReport(True, reason=reason, local_dir=local_dir, error=f"{type(exc).__name__}: {exc}")
        if not files:
            return IncidentReport(True, reason=reason, local_dir=local_dir, error="수집된 파일이 없습니다")

        remote_dir = remote_incident_dir(self.config, name)
        try:
            result = self._uploader(self.config, files, remote_subdir=f"{INCIDENT_ROOT}/{name}")
        except Exception as exc:  # noqa: BLE001 - upload boundary reports instead of raising.
            _LOGGER.exception("hailo-incident-upload-failed")
            return IncidentReport(
                True, reason=reason, local_dir=local_dir, remote_dir=remote_dir,
                file_count=len(files), error=f"{type(exc).__name__}: {exc}",
            )
        report = IncidentReport(
            True,
            reason=reason,
            local_dir=local_dir,
            remote_dir=result.remote_dir or remote_dir,
            file_count=len(result.artifacts) if result.ok else len(files),
            error="" if result.ok else result.error,
        )
        _LOGGER.log(
            logging.INFO if result.ok else logging.ERROR,
            "hailo-incident reason=%s ok=%s remote_dir=%s files=%s local=%s error=%s",
            reason, result.ok, report.remote_dir, report.file_count, local_dir, report.error,
        )
        return report
