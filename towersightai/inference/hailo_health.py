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
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HAILO_PCI_VENDOR = "0x1e60"
DEFAULT_DEVICE_NODE = Path("/dev/hailo0")
DEFAULT_PCI_ROOT = Path("/sys/bus/pci/devices")
DEFAULT_MODULE_ROOT = Path("/sys/module")

# temp probe returns (state, value): ("ok", "47.2") / ("error", "<메시지>") / ("skip", "")
TempProbe = Callable[[], tuple[str, str]]

_LOGGER = logging.getLogger("towersightai.hailo.health")


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

    @property
    def pill_text(self) -> str:
        """Compact text for the always-visible telemetry pill."""
        if self.status == "error":
            return "HAILO 오류"
        if self.status == "degraded":
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
) -> HailoHealthSnapshot:
    """Collect one health snapshot. Never raises; failures become the snapshot."""
    pcie_address, device_dir = find_hailo_pci_device(pci_root)
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
