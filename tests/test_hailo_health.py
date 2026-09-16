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
        holder_scan=lambda: (),
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
        holder_scan=lambda: (),
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
        holder_scan=lambda: (),
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
        holder_scan=lambda: (),
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
        holder_scan=lambda: (),
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
        holder_scan=lambda: (),
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


# ---- device holders (orphaned inference children) --------------------------------------------


def _make_proc(tmp_path: Path, processes: dict[int, tuple[str, int, float, bool]], *, uptime: float = 1000.0) -> Path:
    """Fake /proc: pid -> (comm, ppid, starttime_ticks, holds_hailo)."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text(f"{uptime} {uptime}\n", encoding="ascii")
    for pid, (comm, ppid, starttime, holds) in processes.items():
        pdir = proc / str(pid)
        (pdir / "fd").mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(int(starttime))] + ["0"] * 5
        (pdir / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n", encoding="ascii")
        (pdir / "fd" / "0").symlink_to("/dev/null")
        if holds:
            (pdir / "fd" / "7").symlink_to("/dev/hailo0")
    return proc


def test_find_holders_distinguishes_own_children_from_orphans(tmp_path: Path):
    from towersightai.inference.hailo_health import find_hailo_device_holders

    proc = _make_proc(
        tmp_path,
        {
            1: ("systemd", 0, 0, False),
            100: ("operator_ui", 1, 500 * 100, False),  # us
            200: ("Hailo Multisource App", 100, 900 * 100, True),  # our child
            300: ("Hailo Multisource App", 1, 100 * 100, True),  # orphan re-parented to init
            400: ("bash", 1, 10, False),
        },
    )
    holders = find_hailo_device_holders(proc_root=proc, self_pid=100, clock_ticks=100)
    assert [(h.pid, h.is_descendant) for h in holders] == [(200, True), (300, False)]
    orphan = holders[1]
    assert orphan.name == "Hailo Multisource App"
    assert orphan.ppid == 1
    assert orphan.elapsed_seconds == 900.0
    assert "15분" in orphan.elapsed_text
    assert "고아" in orphan.describe()
    assert "이 앱의 자식" in holders[0].describe()


def test_holder_elapsed_text_covers_days_and_hours():
    from towersightai.inference.hailo_health import HailoDeviceHolder

    assert HailoDeviceHolder(1, "x", 1, 7 * 86400 + 2 * 3600 + 60, False).elapsed_text == "7일 2시간"
    assert HailoDeviceHolder(1, "x", 1, 3 * 3600 + 5 * 60, False).elapsed_text == "3시간 5분"


def test_foreign_holder_degrades_health_and_enables_the_operator_hint(tmp_path: Path):
    from towersightai.inference.hailo_health import HailoDeviceHolder

    pci_root, _ = _make_sysfs(tmp_path)
    module_root = _make_module(tmp_path)
    node = tmp_path / "hailo0"
    node.touch()
    orphan = HailoDeviceHolder(440555, "Hailo Multisource App", 853, 7 * 86400, False)
    own = HailoDeviceHolder(500, "Hailo Multisource App", 100, 30, True)

    snapshot = collect_hailo_health(
        device_node=node, pci_root=pci_root, module_root=module_root,
        temp_probe=lambda: ("ok", "47.5"), holder_scan=lambda: (own, orphan),
    )
    assert snapshot.status == "degraded"
    assert "점유" in snapshot.summary
    assert snapshot.pill_text == "HAILO 점유됨"
    assert snapshot.foreign_holders == (orphan,)
    assert any("440555" in line and "7일" in line for line in snapshot.detail_lines())

    only_own = collect_hailo_health(
        device_node=node, pci_root=pci_root, module_root=module_root,
        temp_probe=lambda: ("ok", "47.5"), holder_scan=lambda: (own,),
    )
    assert only_own.status == "ok"

    failing_scan = collect_hailo_health(
        device_node=node, pci_root=pci_root, module_root=module_root,
        temp_probe=lambda: ("ok", "47.5"), holder_scan=lambda: (_ for _ in ()).throw(OSError("boom")),
    )
    assert failing_scan.status == "ok"
    assert failing_scan.device_holders == ()


def test_describe_device_conflict_names_the_orphan_or_stays_silent():
    from towersightai.inference.hailo_health import HailoDeviceHolder, describe_device_conflict

    orphan = HailoDeviceHolder(440555, "Hailo Multisource App", 853, 7 * 86400, False)
    own = HailoDeviceHolder(500, "Hailo Multisource App", 100, 30, True)
    busy = "[HailoRT] [error] CHECK_SUCCESS failed with status=HAILO_OUT_OF_PHYSICAL_DEVICES(74)"
    assert describe_device_conflict("Caught SIGSEGV", holders=(orphan,)) == ""
    text = describe_device_conflict(busy, holders=(orphan,))
    assert "440555" in text and "점유" in text and "고아 프로세스 종료" in text
    assert "다른 추론 자식" in describe_device_conflict(busy, holders=(own,))
    assert "찾지 못했습니다" in describe_device_conflict(busy, holders=())


def test_terminate_foreign_holders_sends_term_then_kill_and_skips_own_children():
    import signal

    from towersightai.inference.hailo_health import HailoDeviceHolder, terminate_foreign_holders

    calls: list[tuple[int, int]] = []
    still_alive = {300}

    def kill(pid: int, sig: int) -> None:
        if pid == 999:
            raise ProcessLookupError
        calls.append((pid, sig))

    handled = terminate_foreign_holders(
        (
            HailoDeviceHolder(200, "child", 100, 5, True),
            HailoDeviceHolder(300, "orphan", 1, 500, False),
            HailoDeviceHolder(400, "orphan2", 1, 500, False),
            HailoDeviceHolder(999, "gone", 1, 500, False),
        ),
        kill=kill,
        sleep=lambda _s: None,
        alive=lambda pid: pid in still_alive,
    )
    assert handled == (300, 400)
    assert (200, signal.SIGTERM) not in calls
    assert calls == [(300, signal.SIGTERM), (400, signal.SIGTERM), (300, signal.SIGKILL)]


def test_find_orphaned_children_catches_recorder_and_inference_orphans(tmp_path: Path):
    from towersightai.inference.hailo_health import find_orphaned_children, find_stale_children

    proc = _make_proc(
        tmp_path,
        {
            1: ("systemd", 0, 0, False),
            100: ("operator_ui", 1, 500 * 100, False),  # us
            200: ("python3", 100, 900 * 100, False),  # our recorder child
            300: ("python3", 1, 100 * 100, False),  # orphan recorder
            310: ("Hailo Multisource App", 1, 200 * 100, True),  # orphan inference (also holds device)
            400: ("bash", 1, 10, False),
        },
    )
    for pid, cmd in (
        (200, "/usr/bin/python3 /home/x/TowerSightAI/towersightai/cli/event_video_recorder.py"),
        (300, "/usr/bin/python3 /home/x/TowerSightAI/towersightai/cli/event_video_recorder.py"),
        (310, "python -m towersightai.cli.hailo_apps_detection --hef a.hef"),
        (400, "bash"),
        (100, "python -m towersightai.cli.operator_ui"),
    ):
        (proc / str(pid) / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")

    orphans = find_orphaned_children(proc_root=proc, self_pid=100, clock_ticks=100)
    assert [o.pid for o in orphans] == [300, 310]
    assert all(o.is_descendant is False for o in orphans)

    stale = find_stale_children(proc_root=proc, self_pid=100, clock_ticks=100)
    assert [(h.pid, h.is_descendant) for h in stale] == [(300, False), (310, False)]
