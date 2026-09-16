"""Hailo-8 device health collection for the operator console.

Built from a real field incident: the chip hung while idle after a day of
correctable PCIe RxErr on its link, and the only visible symptom was every
inference dying instantly with ``HAILO_DRIVER_OPERATION_FAILED(36)``. This
module watches exactly those signals so the operator sees the failure — and
the trend that precedes it — before wondering why AI 추론 buttons fail.

Health output is diagnostic telemetry only. It never relaxes the safety gate:
an unhealthy device already blocks final OK through the inference path.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HAILO_PCI_VENDOR = "0x1e60"
DEFAULT_DEVICE_NODE = Path("/dev/hailo0")
DEFAULT_PCI_ROOT = Path("/sys/bus/pci/devices")
DEFAULT_MODULE_ROOT = Path("/sys/module")

# temp probe returns (state, value): ("ok", "47.2") / ("error", "<메시지>") / ("skip", "")
TempProbe = Callable[[], tuple[str, str]]

_LOGGER = logging.getLogger("towersightai.hailo.health")


@dataclass(frozen=True)
class HailoDeviceHolder:
    """A process that currently has ``/dev/hailo*`` open."""

    pid: int
    name: str
    ppid: int
    elapsed_seconds: float
    is_descendant: bool  # child of the current process (our own inference child)

    @property
    def elapsed_text(self) -> str:
        seconds = int(self.elapsed_seconds)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}일 {hours}시간"
        if hours:
            return f"{hours}시간 {minutes}분"
        return f"{minutes}분"

    def describe(self) -> str:
        owner = "이 앱의 자식" if self.is_descendant else "다른 프로세스(고아 가능성)"
        return f"PID {self.pid} {self.name!r} 실행 {self.elapsed_text}, {owner}"


def _read_proc_stat(proc_dir: Path) -> tuple[str, int, float] | None:
    """Return (comm, ppid, starttime_ticks) from /proc/<pid>/stat, or None."""
    try:
        raw = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        left, _, right = raw.rpartition(")")
        comm = left.split("(", 1)[1]
        fields = right.split()
        ppid = int(fields[1])
        starttime = float(fields[19])
    except (IndexError, ValueError):
        return None
    return comm, ppid, starttime


def _is_descendant(pid: int, ancestor: int, parents: dict[int, int]) -> bool:
    seen = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        parent = parents.get(current)
        if parent is None:
            return False
        if parent == ancestor:
            return True
        current = parent
    return False


def find_hailo_device_holders(
    *,
    proc_root: Path = Path("/proc"),
    device_prefix: str = "/dev/hailo",
    self_pid: int | None = None,
    now_uptime: float | None = None,
    clock_ticks: int | None = None,
) -> tuple[HailoDeviceHolder, ...]:
    """Scan /proc for processes holding the Hailo device node. Never raises."""
    self_pid = os.getpid() if self_pid is None else self_pid
    try:
        ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        ticks = 100
    if now_uptime is None:
        try:
            now_uptime = float((proc_root / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            now_uptime = None
    stats: dict[int, tuple[str, int, float]] = {}
    holders_pids: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _read_proc_stat(entry)
        if stat is None:
            continue
        stats[pid] = stat
        if pid == self_pid:
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith(device_prefix):
                holders_pids.append(pid)
                break
    parents = {pid: stat[1] for pid, stat in stats.items()}
    holders = []
    for pid in sorted(holders_pids):
        comm, ppid, starttime = stats[pid]
        elapsed = 0.0 if now_uptime is None else max(0.0, now_uptime - starttime / ticks)
        holders.append(
            HailoDeviceHolder(
                pid=pid,
                name=comm,
                ppid=ppid,
                elapsed_seconds=elapsed,
                is_descendant=_is_descendant(pid, self_pid, parents),
            )
        )
    return tuple(holders)


DEVICE_BUSY_MARKERS = ("HAILO_OUT_OF_PHYSICAL_DEVICES", "not enough free devices")
# Command-line fragments of every child this application spawns and that must never outlive it.
CHILD_CMDLINE_MARKERS = (
    "towersightai.cli.hailo_apps_detection",
    "towersightai/cli/event_video_recorder.py",
)
CHILD_COMM_MARKERS = ("Hailo Multisour",)


def find_orphaned_children(
    *,
    proc_root: Path = Path("/proc"),
    self_pid: int | None = None,
    now_uptime: float | None = None,
    clock_ticks: int | None = None,
) -> tuple[HailoDeviceHolder, ...]:
    """TowerSightAI child processes (inference, evidence recorder) that are not our descendants.

    They hold Hailo and/or camera RTSP sessions, so a stale one from a dead UI starves every later
    run (HAILO_OUT_OF_PHYSICAL_DEVICES, RTSP 400). Never raises.
    """
    self_pid = os.getpid() if self_pid is None else self_pid
    try:
        ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        ticks = 100
    if now_uptime is None:
        try:
            now_uptime = float((proc_root / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            now_uptime = None
    stats: dict[int, tuple[str, int, float]] = {}
    candidates: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _read_proc_stat(entry)
        if stat is None:
            continue
        stats[pid] = stat
        if pid == self_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        comm = stat[0]
        if any(marker in cmdline for marker in CHILD_CMDLINE_MARKERS) or any(
            comm.startswith(marker) for marker in CHILD_COMM_MARKERS
        ):
            candidates.append(pid)
    parents = {pid: stat[1] for pid, stat in stats.items()}
    orphans = []
    for pid in sorted(candidates):
        if _is_descendant(pid, self_pid, parents):
            continue
        comm, ppid, starttime = stats[pid]
        elapsed = 0.0 if now_uptime is None else max(0.0, now_uptime - starttime / ticks)
        orphans.append(HailoDeviceHolder(pid=pid, name=comm, ppid=ppid, elapsed_seconds=elapsed, is_descendant=False))
    return tuple(orphans)


def find_stale_children(**kwargs: Any) -> tuple[HailoDeviceHolder, ...]:
    """Device holders plus orphaned children, de-duplicated by pid (the health monitor's scan)."""
    seen: dict[int, HailoDeviceHolder] = {}
    for holder in find_hailo_device_holders(**kwargs):
        seen[holder.pid] = holder
    for orphan in find_orphaned_children(**{k: v for k, v in kwargs.items() if k != "device_prefix"}):
        seen.setdefault(orphan.pid, orphan)
    return tuple(seen[pid] for pid in sorted(seen))


def describe_device_conflict(log_text: str, holders: tuple[HailoDeviceHolder, ...] | None = None) -> str:
    """Operator-facing explanation when a child failed because the device was already taken."""
    if not any(marker in log_text for marker in DEVICE_BUSY_MARKERS):
        return ""
    holders = find_hailo_device_holders() if holders is None else holders
    foreign = [holder for holder in holders if not holder.is_descendant]
    if foreign:
        listed = "; ".join(holder.describe() for holder in foreign)
        return (
            f"Hailo 장치를 다른 프로세스가 점유 중입니다: {listed}. "
            "시스템 점검 페이지의 '고아 프로세스 종료'로 정리하거나 해당 프로세스를 종료하세요"
        )
    if holders:
        return "Hailo 장치를 이 앱의 다른 추론 자식이 아직 쥐고 있습니다. 이전 추론이 끝난 뒤 다시 시작하세요"
    return "Hailo 장치가 사용 중이라고 보고되었지만 점유 프로세스를 찾지 못했습니다 (장치 재초기화 필요 가능)"


def terminate_foreign_holders(
    holders: tuple[HailoDeviceHolder, ...],
    *,
    kill: Callable[[int, int], None] = os.kill,
    grace_seconds: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] | None = None,
) -> tuple[int, ...]:
    """SIGTERM (then SIGKILL) holders that are not this process's descendants. Returns pids handled."""

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    alive = alive or _alive
    handled: list[int] = []
    targets = [holder for holder in holders if not holder.is_descendant]
    for holder in targets:
        try:
            kill(holder.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            _LOGGER.warning("hailo-holder-terminate-denied pid=%s name=%s", holder.pid, holder.name)
            continue
        handled.append(holder.pid)
        _LOGGER.warning("hailo-holder-terminate pid=%s name=%s elapsed=%s", holder.pid, holder.name, holder.elapsed_text)
    if handled:
        sleep(grace_seconds)
        for pid in handled:
            if alive(pid):
                try:
                    kill(pid, signal.SIGKILL)
                    _LOGGER.warning("hailo-holder-kill pid=%s", pid)
                except (ProcessLookupError, PermissionError):
                    pass
    return tuple(handled)


@dataclass(frozen=True)
class HailoHealthSnapshot:
    status: str  # "ok" | "degraded" | "error"
    summary: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pcie_address: str = ""
    pcie_parent: str = ""
    driver_loaded: bool = False
    driver_version: str = ""
    device_node_exists: bool = False
    rxerr_count: int | None = None
    rxerr_delta: int = 0
    chip_temperature_c: float | None = None
    detail: str = ""
    device_holders: tuple[HailoDeviceHolder, ...] = ()

    @property
    def foreign_holders(self) -> tuple[HailoDeviceHolder, ...]:
        return tuple(holder for holder in self.device_holders if not holder.is_descendant)

    @property
    def pill_text(self) -> str:
        """Compact text for the always-visible telemetry pill."""
        if self.status == "error":
            return "HAILO 오류"
        if self.status == "degraded":
            if self.foreign_holders:
                return "HAILO 점유됨"
            extra = f" +{self.rxerr_delta}" if self.rxerr_delta else ""
            return f"HAILO 링크오류{extra}"
        if self.chip_temperature_c is not None:
            return f"HAILO 정상 {self.chip_temperature_c:.0f}°C"
        return "HAILO 정상"

    def detail_lines(self) -> tuple[str, ...]:
        rxerr = "확인 불가" if self.rxerr_count is None else f"{self.rxerr_count}건"
        if self.rxerr_delta:
            rxerr += f" (이번 감시 중 +{self.rxerr_delta})"
        temp = "확인 불가" if self.chip_temperature_c is None else f"{self.chip_temperature_c:.1f}°C"
        lines = [
            f"상태: {self.summary}",
            f"PCIe 장치: {self.pcie_address or '미검출'}"
            + (f" (포트 {self.pcie_parent})" if self.pcie_parent else ""),
            f"드라이버(hailo_pci): {'로드됨 ' + self.driver_version if self.driver_loaded else '미로드'}",
            f"/dev/hailo0: {'있음' if self.device_node_exists else '없음'}",
            f"PCIe 링크 오류(RxErr 누적): {rxerr}",
            f"칩 온도: {temp}",
        ]
        if self.device_holders:
            lines.append("추론/녹화 프로세스: " + "; ".join(holder.describe() for holder in self.device_holders))
        else:
            lines.append("추론/녹화 프로세스: 없음")
        if self.detail:
            lines.append(f"세부: {self.detail}")
        return tuple(lines)


def find_hailo_pci_device(pci_root: Path = DEFAULT_PCI_ROOT) -> tuple[str, Path | None]:
    """Return (pci address, sysfs dir) of the first Hailo device, or ("", None)."""
    try:
        entries = sorted(pci_root.iterdir())
    except OSError:
        return "", None
    for entry in entries:
        try:
            vendor = (entry / "vendor").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if vendor.lower() == HAILO_PCI_VENDOR:
            return entry.name, entry
    return "", None


def read_rxerr_count(device_dir: Path) -> int | None:
    """Sum RxErr from the device and its root port AER counters (whichever exist)."""
    total: int | None = None
    candidates = [device_dir / "aer_dev_correctable"]
    try:
        parent = device_dir.resolve().parent
        candidates.append(parent / "aer_dev_correctable")
    except OSError:
        pass
    for path in candidates:
        try:
            text = path.read_text(encoding="ascii")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "RxErr":
                total = (total or 0) + int(parts[1])
    return total


def make_subprocess_temp_probe(hailo_apps_python: Path, *, timeout_seconds: float = 12.0) -> TempProbe:
    """Query chip responsiveness + temperature through the Hailo Apps venv.

    A successful read proves the device answers control requests; the failure text
    (e.g. HAILO_DRIVER_OPERATION_FAILED) is exactly what the field needs to see.
    """

    # pyhailort treats a temporary Device as released before .control is used,
    # so the device must be held in a variable and released explicitly.
    code = (
        "from hailo_platform import Device\n"
        "device = Device()\n"
        "temperature = device.control.get_chip_temperature()\n"
        "print(f'{temperature.ts0_temperature:.1f}')\n"
        "device.release()\n"
    )

    def probe() -> tuple[str, str]:
        python = Path(hailo_apps_python).expanduser()
        if not python.is_file():
            return "skip", ""
        try:
            result = subprocess.run(
                [str(python), "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return "error", "장치 응답 시간 초과"
        except OSError as exc:
            return "error", f"온도 조회 실행 실패: {exc}"
        if result.returncode == 0:
            value = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            try:
                float(value)
            except ValueError:
                return "error", f"온도 응답 형식 오류: {value[:60]}"
            return "ok", value
        stderr = (result.stderr or result.stdout or "").strip()
        for line in stderr.splitlines():
            if "HAILO_" in line or "error" in line.lower():
                return "error", line.strip()[:160]
        return "error", (stderr.splitlines()[-1][:160] if stderr else f"exit {result.returncode}")

    return probe


def collect_hailo_health(
    *,
    device_node: Path = DEFAULT_DEVICE_NODE,
    pci_root: Path = DEFAULT_PCI_ROOT,
    module_root: Path = DEFAULT_MODULE_ROOT,
    temp_probe: TempProbe | None = None,
    previous_rxerr: int | None = None,
    holder_scan: Callable[[], tuple[HailoDeviceHolder, ...]] | None = find_stale_children,
) -> HailoHealthSnapshot:
    """Collect one health snapshot. Never raises; failures become the snapshot."""
    pcie_address, device_dir = find_hailo_pci_device(pci_root)
    holders: tuple[HailoDeviceHolder, ...] = ()
    if holder_scan is not None:
        try:
            holders = holder_scan()
        except Exception:  # noqa: BLE001 - a scan failure must not break health reporting.
            _LOGGER.exception("hailo-holder-scan-failed")
    foreign_holders = tuple(holder for holder in holders if not holder.is_descendant)
    pcie_parent = ""
    rxerr_count: int | None = None
    if device_dir is not None:
        try:
            pcie_parent = device_dir.resolve().parent.name
        except OSError:
            pcie_parent = ""
        rxerr_count = read_rxerr_count(device_dir)

    module_dir = module_root / "hailo_pci"
    driver_loaded = module_dir.is_dir()
    driver_version = ""
    if driver_loaded:
        try:
            driver_version = (module_dir / "version").read_text(encoding="ascii").strip()
        except OSError:
            driver_version = ""

    node_exists = device_node.exists()

    probe_state, probe_value = ("skip", "")
    if temp_probe is not None:
        probe_state, probe_value = temp_probe()
    chip_temp = float(probe_value) if probe_state == "ok" else None

    rxerr_delta = 0
    if rxerr_count is not None and previous_rxerr is not None and rxerr_count > previous_rxerr:
        rxerr_delta = rxerr_count - previous_rxerr

    if not pcie_address:
        status, summary = "error", "PCIe에서 Hailo 장치가 보이지 않습니다 (전원/장착 확인)"
        detail = ""
    elif not driver_loaded:
        status, summary = "error", "hailo_pci 드라이버가 로드되지 않았습니다"
        detail = "sudo modprobe hailo_pci 또는 dkms 상태를 확인하세요."
    elif not node_exists:
        status, summary = "error", "/dev/hailo0 장치 파일이 없습니다"
        detail = "드라이버 probe 실패 여부를 dmesg에서 확인하세요."
    elif probe_state == "error":
        status, summary = "error", "장치가 제어 요청에 응답하지 않습니다"
        detail = f"{probe_value} · 콜드 부팅(전원 완전 차단)이 필요할 수 있습니다."
    elif rxerr_delta:
        status = "degraded"
        summary = f"PCIe 링크 오류가 증가하고 있습니다 (RxErr +{rxerr_delta}, 누적 {rxerr_count})"
        detail = "M.2 장착 상태 점검 또는 PCIe 링크 속도 하향(Gen2)을 검토하세요."
    elif foreign_holders:
        status = "degraded"
        summary = (
            f"이 앱의 자식이 아닌 추론/녹화 프로세스가 {len(foreign_holders)}개 남아 있습니다 "
            "— Hailo 장치 또는 카메라 RTSP 세션을 점유해 추론이 시작되지 못합니다"
        )
        detail = "'고아 프로세스 종료' 버튼으로 정리하세요 (이 앱의 자식은 건드리지 않습니다)."
    else:
        status, summary = "ok", "정상"
        detail = "" if probe_state == "ok" else (
            "온도/응답 확인은 Hailo Apps Python이 있어야 수행됩니다." if probe_state == "skip" else ""
        )

    return HailoHealthSnapshot(
        status=status,
        summary=summary,
        pcie_address=pcie_address,
        pcie_parent=pcie_parent,
        driver_loaded=driver_loaded,
        driver_version=driver_version,
        device_node_exists=node_exists,
        rxerr_count=rxerr_count,
        rxerr_delta=rxerr_delta,
        chip_temperature_c=chip_temp,
        detail=detail,
        device_holders=holders,
    )


def log_hailo_health(snapshot: HailoHealthSnapshot, *, previous: HailoHealthSnapshot | None) -> None:
    """Write the snapshot to the runtime log. Status changes and RxErr growth are loud."""
    line = (
        f"hailo-health status={snapshot.status} summary={snapshot.summary} "
        f"pcie={snapshot.pcie_address or 'none'} driver={snapshot.driver_version or 'none'} "
        f"node={'yes' if snapshot.device_node_exists else 'no'} "
        f"rxerr={snapshot.rxerr_count if snapshot.rxerr_count is not None else 'na'} "
        f"temp={f'{snapshot.chip_temperature_c:.1f}C' if snapshot.chip_temperature_c is not None else 'na'}"
        + (f" detail={snapshot.detail}" if snapshot.detail else "")
    )
    changed = previous is None or previous.status != snapshot.status
    if snapshot.status == "error":
        (_LOGGER.error if changed else _LOGGER.warning)(line)
    elif snapshot.status == "degraded" or snapshot.rxerr_delta:
        _LOGGER.warning(line)
    elif changed:
        _LOGGER.info(line)
    else:
        _LOGGER.debug(line)
