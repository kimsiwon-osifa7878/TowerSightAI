from pathlib import Path

from towersightai.inference.hailo_health import (
    HailoHealthSnapshot,
    collect_hailo_health,
    find_hailo_pci_device,
    read_rxerr_count,
)


def _make_sysfs(tmp_path: Path, *, vendor: str = "0x1e60", rxerr: int = 0, parent_rxerr: int = 0) -> tuple[Path, Path]:
    """Build a fake /sys/bus/pci/devices with one device under a root port."""
    real_root = tmp_path / "sys" / "devices" / "pci0000:00"
    port = real_root / "0000:00:1d.0"
    device = port / "0000:02:00.0"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n", encoding="ascii")
    (device / "aer_dev_correctable").write_text(f"RxErr {rxerr}\nBadTLP 0\n", encoding="ascii")
    (port / "aer_dev_correctable").write_text(f"RxErr {parent_rxerr}\nBadTLP 0\n", encoding="ascii")
    pci_root = tmp_path / "bus"
    pci_root.mkdir()
    (pci_root / "0000:02:00.0").symlink_to(device)
    return pci_root, device


def _make_module(tmp_path: Path, *, version: str = "4.23.0") -> Path:
    module_root = tmp_path / "module"
    (module_root / "hailo_pci").mkdir(parents=True)
    (module_root / "hailo_pci" / "version").write_text(version + "\n", encoding="ascii")
    return module_root


def test_healthy_device_reports_ok_with_temperature(tmp_path: Path):
    pci_root, _ = _make_sysfs(tmp_path)
    module_root = _make_module(tmp_path)
    node = tmp_path / "hailo0"
    node.touch()

    snapshot = collect_hailo_health(
        device_node=node,
        pci_root=pci_root,
        module_root=module_root,
        temp_probe=lambda: ("ok", "47.5"),
    )

    assert snapshot.status == "ok"
    assert snapshot.pcie_address == "0000:02:00.0"
    assert snapshot.pcie_parent == "0000:00:1d.0"
    assert snapshot.driver_version == "4.23.0"
    assert snapshot.chip_temperature_c == 47.5
    assert snapshot.pill_text == "HAILO 정상 48°C"


def test_missing_pcie_device_is_an_error(tmp_path: Path):
    pci_root, _ = _make_sysfs(tmp_path, vendor="0x10ec")  # not Hailo
    snapshot = collect_hailo_health(
        device_node=tmp_path / "hailo0",
        pci_root=pci_root,
        module_root=_make_module(tmp_path),
        temp_probe=lambda: ("skip", ""),
    )
    assert snapshot.status == "error"
    assert "PCIe" in snapshot.summary
    assert snapshot.pill_text == "HAILO 오류"


def test_unloaded_driver_is_an_error(tmp_path: Path):
    pci_root, _ = _make_sysfs(tmp_path)
    empty_module_root = tmp_path / "module"
    empty_module_root.mkdir()
    snapshot = collect_hailo_health(
        device_node=tmp_path / "hailo0",
        pci_root=pci_root,
        module_root=empty_module_root,
        temp_probe=lambda: ("skip", ""),
    )
    assert snapshot.status == "error"
    assert "드라이버" in snapshot.summary


def test_unresponsive_device_reports_the_driver_error_and_cold_boot_hint(tmp_path: Path):
    """The field incident: node exists, driver loaded, but the chip is hung."""
    pci_root, _ = _make_sysfs(tmp_path)
    node = tmp_path / "hailo0"
    node.touch()
    snapshot = collect_hailo_health(
        device_node=node,
        pci_root=pci_root,
        module_root=_make_module(tmp_path),
        temp_probe=lambda: ("error", "CHECK_SUCCESS failed with status=HAILO_DRIVER_OPERATION_FAILED(36)"),
    )
    assert snapshot.status == "error"
    assert "응답하지" in snapshot.summary
    assert "HAILO_DRIVER_OPERATION_FAILED" in snapshot.detail
    assert "콜드 부팅" in snapshot.detail


def test_growing_rxerr_degrades_with_delta(tmp_path: Path):
    pci_root, _ = _make_sysfs(tmp_path, rxerr=0, parent_rxerr=11)
    node = tmp_path / "hailo0"
    node.touch()
    snapshot = collect_hailo_health(
        device_node=node,
        pci_root=pci_root,
        module_root=_make_module(tmp_path),
        temp_probe=lambda: ("ok", "51.0"),
        previous_rxerr=8,
    )
    assert snapshot.rxerr_count == 11
    assert snapshot.rxerr_delta == 3
    assert snapshot.status == "degraded"
    assert "RxErr +3" in snapshot.summary
    assert snapshot.pill_text == "HAILO 링크오류 +3"
    assert any("M.2" in line or "Gen2" in line for line in snapshot.detail_lines())


def test_stable_rxerr_count_stays_ok(tmp_path: Path):
    pci_root, _ = _make_sysfs(tmp_path, parent_rxerr=11)
    node = tmp_path / "hailo0"
    node.touch()
    snapshot = collect_hailo_health(
        device_node=node,
        pci_root=pci_root,
        module_root=_make_module(tmp_path),
        temp_probe=lambda: ("ok", "51.0"),
        previous_rxerr=11,
    )
    assert snapshot.status == "ok"
    assert snapshot.rxerr_delta == 0


def test_rxerr_sums_device_and_parent_port(tmp_path: Path):
    pci_root, device = _make_sysfs(tmp_path, rxerr=2, parent_rxerr=9)
    assert read_rxerr_count(pci_root / "0000:02:00.0") == 11
    assert find_hailo_pci_device(pci_root)[0] == "0000:02:00.0"


def test_detail_lines_are_operator_readable():
    snapshot = HailoHealthSnapshot(
        status="error",
        summary="장치가 제어 요청에 응답하지 않습니다",
        pcie_address="0000:02:00.0",
        pcie_parent="0000:00:1d.0",
        driver_loaded=True,
        driver_version="4.23.0",
        device_node_exists=True,
        rxerr_count=11,
        detail="HAILO_DRIVER_OPERATION_FAILED(36) · 콜드 부팅(전원 완전 차단)이 필요할 수 있습니다.",
    )
    text = "\n".join(snapshot.detail_lines())
    assert "0000:02:00.0" in text
    assert "4.23.0" in text
    assert "11건" in text
    assert "콜드 부팅" in text
