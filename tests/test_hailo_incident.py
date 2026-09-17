from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from towersightai.config.settings import RawStorageConfig
from towersightai.inference.hailo_health import HailoDeviceHolder, HailoHealthSnapshot
from towersightai.storage.file_transfer import NasFileTransferResult, remote_file_transfer_dir
from towersightai.storage.hailo_incident import (
    INCIDENT_ROOT,
    HailoIncidentReporter,
    build_incident_bundle,
    incident_name,
    remote_incident_dir,
    snapshot_to_dict,
)

AT = datetime(2026, 9, 17, 5, 6, 7, tzinfo=timezone.utc)


def _config(tmp_path: Path, **overrides) -> RawStorageConfig:
    values = {
        "enabled": True,
        "local_dir": tmp_path / "raw",
        "nas_host": "nas.example.test",
        "nas_username": "uploader",
        "nas_password": "secret",
        "nas_folder": "/home/site",
        "known_hosts_path": tmp_path / "known_hosts",
    }
    values.update(overrides)
    return RawStorageConfig(**values)


def _snapshot(status: str = "error", **overrides) -> HailoHealthSnapshot:
    values = {
        "status": status,
        "summary": "장치가 제어 요청에 응답하지 않습니다",
        "checked_at": AT,
        "pcie_address": "0000:02:00.0",
        "pcie_parent": "0000:00:1d.0",
        "driver_loaded": True,
        "driver_version": "4.23.0",
        "device_node_exists": True,
        "rxerr_count": 0,
        "chip_temperature_c": None,
        "detail": "HAILO_DRIVER_OPERATION_FAILED(36) rtsp://user:pw@10.0.0.9/stream1",
    }
    values.update(overrides)
    return HailoHealthSnapshot(**values)


def _fake_sysfs(tmp_path: Path) -> tuple[Path, Path]:
    real_root = tmp_path / "sys" / "devices" / "pci0000:00"
    port = real_root / "0000:00:1d.0"
    device = port / "0000:02:00.0"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x1e60\n", encoding="ascii")
    (device / "current_link_speed").write_text("2.5 GT/s PCIe\n", encoding="ascii")
    (device / "current_link_width").write_text("2\n", encoding="ascii")
    (device / "max_link_speed").write_text("8.0 GT/s PCIe\n", encoding="ascii")
    (device / "aer_dev_correctable").write_text("RxErr 17\nBadTLP 0\n", encoding="ascii")
    (port / "aer_dev_correctable").write_text("RxErr 42\nBadTLP 0\n", encoding="ascii")
    pci_root = tmp_path / "bus"
    pci_root.mkdir()
    (pci_root / "0000:02:00.0").symlink_to(device)
    return pci_root, device


# ---- bundle contents ---------------------------------------------------------------------------


def test_bundle_captures_link_driver_kernel_and_log_evidence(tmp_path: Path):
    pci_root, _device = _fake_sysfs(tmp_path)
    module_root = tmp_path / "modules"
    (module_root / "hailo_pci").mkdir(parents=True)
    (module_root / "hailo_pci" / "version").write_text("4.23.0\n", encoding="ascii")
    node = tmp_path / "hailo0"
    node.touch()
    runtime_log = tmp_path / "towersightai.log"
    runtime_log.write_text(
        "\n".join(f"line {index} rtsp://user:pw@10.0.0.9/stream1" for index in range(50)),
        encoding="utf-8",
    )
    child_dir = tmp_path / "purpose-ai" / "process_monitoring"
    child_dir.mkdir(parents=True)
    (child_dir / "process_monitoring.gst.log").write_text(
        "PIPELINE_DIAGNOSTIC_PROBE stage=hailonet_input\n[HailoRT] [error] status=HAILO_DRIVER_OPERATION_FAILED(36)\n",
        encoding="utf-8",
    )
    commands: list[tuple[str, ...]] = []

    def runner(command):
        commands.append(tuple(command))
        if command[0] == "dmesg":
            return "hailo 0000:02:00.0: Device disconnected while opening device\nunrelated line\n"
        return "identify output"

    files = build_incident_bundle(
        tmp_path / "bundle",
        _snapshot(),
        reason="status_change",
        runtime_log=runtime_log,
        purpose_ai_dir=tmp_path / "purpose-ai",
        device_node=node,
        pci_root=pci_root,
        module_root=module_root,
        runner=runner,
        at=AT,
    )

    names = [path.name for path in files]
    assert names == [
        "00-summary.json",
        "01-pcie.txt",
        "02-driver.txt",
        "03-kernel.txt",
        "04-identify.txt",
        "05-app-log.txt",
        "06-child-log.txt",
    ]
    summary = json.loads((tmp_path / "bundle" / "00-summary.json").read_text(encoding="utf-8"))
    assert summary["kind"] == "hailo_incident"
    assert summary["reason"] == "status_change"
    assert summary["safe_to_operate"] is False
    assert summary["health"]["status"] == "error"
    assert summary["health"]["rxerr_count"] == 0

    pcie = (tmp_path / "bundle" / "01-pcie.txt").read_text(encoding="utf-8")
    assert "current_link_speed: 2.5 GT/s PCIe" in pcie  # the decisive Gen1-downgrade field
    assert "RxErr 17" in pcie and "RxErr 42" in pcie  # endpoint and upstream port
    driver = (tmp_path / "bundle" / "02-driver.txt").read_text(encoding="utf-8")
    assert "hailo_pci version: 4.23.0" in driver and "present" in driver
    kernel = (tmp_path / "bundle" / "03-kernel.txt").read_text(encoding="utf-8")
    assert "Device disconnected while opening device" in kernel
    assert "unrelated line" not in kernel
    child = (tmp_path / "bundle" / "06-child-log.txt").read_text(encoding="utf-8")
    assert "HAILO_DRIVER_OPERATION_FAILED(36)" in child
    assert "PIPELINE_DIAGNOSTIC" not in child
    assert ("hailortcli", "fw-control", "identify") in commands

    # No collected file may leak RTSP credentials.
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "user:pw" not in text
    assert "***:***@" in (tmp_path / "bundle" / "05-app-log.txt").read_text(encoding="utf-8")


def test_bundle_is_written_even_when_every_source_is_missing(tmp_path: Path):
    files = build_incident_bundle(
        tmp_path / "bundle",
        _snapshot(),
        runtime_log=tmp_path / "missing.log",
        purpose_ai_dir=tmp_path / "missing",
        device_node=tmp_path / "no-node",
        pci_root=tmp_path / "no-pci",
        module_root=tmp_path / "no-modules",
        runner=lambda _command: "",
        at=AT,
    )
    assert len(files) == 7
    kernel = (tmp_path / "bundle" / "03-kernel.txt").read_text(encoding="utf-8")
    assert "no kernel messages available" in kernel
    assert "no inference child log" in (tmp_path / "bundle" / "06-child-log.txt").read_text(encoding="utf-8")


def test_app_log_is_a_frozen_tail_copy_not_a_live_read(tmp_path: Path):
    """The manual upload failed remote SHA-256 because the live log grew mid-transfer."""
    runtime_log = tmp_path / "towersightai.log"
    runtime_log.write_text("old line\n", encoding="utf-8")
    build_incident_bundle(
        tmp_path / "bundle", _snapshot(), runtime_log=runtime_log,
        purpose_ai_dir=tmp_path / "none", pci_root=tmp_path / "none",
        module_root=tmp_path / "none", device_node=tmp_path / "none",
        runner=lambda _c: "", at=AT,
    )
    with runtime_log.open("a", encoding="utf-8") as fp:
        fp.write("line written after collection\n")
    copied = (tmp_path / "bundle" / "05-app-log.txt").read_text(encoding="utf-8")
    assert "old line" in copied
    assert "after collection" not in copied


def test_snapshot_to_dict_redacts_and_lists_holders():
    holder = HailoDeviceHolder(440555, "Hailo Multisource App", 853, 7 * 86400, False)
    data = snapshot_to_dict(_snapshot(device_holders=(holder,)))
    assert data["status"] == "error"
    assert data["safety_effect"] == "raw_only"
    assert "user:pw" not in data["detail"] and "***:***@" in data["detail"]
    assert data["device_holders"] and "440555" in data["device_holders"][0]
    assert snapshot_to_dict(None) == {}


# ---- reporter: trigger, throttle, safety --------------------------------------------------------


class _Recorder:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[dict] = []
        self.ok = ok

    def __call__(self, config, files, *, remote_subdir="transfer", **kwargs):
        self.calls.append({"files": tuple(files), "remote_subdir": remote_subdir})
        remote = remote_file_transfer_dir(config, remote_subdir)
        if not self.ok:
            return NasFileTransferResult(ok=False, summary="실패", remote_dir=remote, error="OSError: timed out")
        return NasFileTransferResult(ok=True, summary="완료", remote_dir=remote)


def _reporter(tmp_path: Path, uploader, *, clock=None, **config_overrides) -> HailoIncidentReporter:
    ticks = clock if clock is not None else iter(range(0, 100000, 1))
    return HailoIncidentReporter(
        _config(tmp_path, **config_overrides),
        work_dir=tmp_path / "incidents",
        runtime_log=tmp_path / "towersightai.log",
        purpose_ai_dir=tmp_path / "purpose-ai",
        uploader=uploader,
        builder=lambda out_dir, *a, **kw: (_written(out_dir),),
        clock=lambda: next(ticks) if not callable(clock) else clock(),
        now=lambda: AT,
    )


def _written(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "00-summary.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_report_is_uploaded_once_per_transition_into_a_bad_status(tmp_path: Path):
    uploader = _Recorder()
    now = {"value": 0.0}
    reporter = _reporter(tmp_path, uploader, clock=lambda: now["value"])

    assert reporter.observe(_snapshot("ok")).reported is False
    report = reporter.observe(_snapshot("error"))
    assert report.reported is True and report.error == ""
    assert report.reason == "status_change"
    assert report.safe_to_operate is False
    assert f"{INCIDENT_ROOT}/" in uploader.calls[0]["remote_subdir"]
    assert report.remote_dir == remote_incident_dir(reporter.config, incident_name(at=AT))
    assert "Hailo 진단 자료 NAS 업로드 완료" in report.summary()

    # Still failing a minute later: throttled, no second upload.
    now["value"] = 60.0
    assert reporter.observe(_snapshot("error")).reason == "throttled"
    assert len(uploader.calls) == 1

    # Past the interval it reports again, marked as a continuing failure.
    now["value"] = 60.0 + reporter.config.hailo_incident_min_interval_seconds
    again = reporter.observe(_snapshot("error"))
    assert again.reported is True and again.reason == "still_failing"
    assert len(uploader.calls) == 2

    # Recovery, then a new failure uploads immediately regardless of the interval.
    now["value"] += 1
    assert reporter.observe(_snapshot("ok")).reported is False
    now["value"] += 1
    assert reporter.observe(_snapshot("degraded")).reason == "status_change"
    assert len(uploader.calls) == 3


def test_reporter_is_off_without_raw_storage_or_when_disabled(tmp_path: Path):
    uploader = _Recorder()
    # RAW_DATA_ENABLED=false: no verified NAS path, so the health thread must never dial out.
    off = HailoIncidentReporter(
        _config(tmp_path, enabled=False), work_dir=tmp_path / "a", uploader=uploader,
        builder=lambda out_dir, *a, **kw: (_written(out_dir),),
    )
    assert off.enabled is False
    assert off.observe(_snapshot("error")).reason == "upload_disabled"

    explicit = HailoIncidentReporter(
        _config(tmp_path, hailo_incident_upload_enabled=False), work_dir=tmp_path / "b",
        uploader=uploader, builder=lambda out_dir, *a, **kw: (_written(out_dir),),
    )
    assert explicit.observe(_snapshot("error")).reason == "upload_disabled"

    no_host = HailoIncidentReporter(
        _config(tmp_path, enabled=False, nas_host=""), work_dir=tmp_path / "c",
        uploader=uploader, builder=lambda out_dir, *a, **kw: (_written(out_dir),),
    )
    assert no_host.observe(_snapshot("error")).reason == "upload_disabled"
    assert uploader.calls == []


def test_upload_and_build_failures_are_reported_not_raised(tmp_path: Path):
    failing_upload = _Recorder(ok=False)
    reporter = _reporter(tmp_path, failing_upload)
    report = reporter.observe(_snapshot("error"))
    assert report.reported is True
    assert "timed out" in report.error
    assert "업로드 실패" in report.summary()
    assert report.local_dir is not None and report.local_dir.is_dir()  # evidence kept on disk

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    broken = HailoIncidentReporter(
        _config(tmp_path), work_dir=tmp_path / "broken", uploader=_Recorder(), builder=boom, now=lambda: AT
    )
    crashed = broken.observe(_snapshot("error"))
    assert crashed.reported is True and "OSError" in crashed.error

    empty = HailoIncidentReporter(
        _config(tmp_path), work_dir=tmp_path / "empty", uploader=_Recorder(),
        builder=lambda *a, **kw: (), now=lambda: AT,
    )
    assert "수집된 파일이 없습니다" in empty.observe(_snapshot("error")).error


def test_incident_remote_path_is_isolated_from_the_operator_transfer_folder(tmp_path: Path):
    config = _config(tmp_path)
    assert remote_incident_dir(config, "host-20260917-050607Z") == "/home/site/hailo-incidents/host-20260917-050607Z"
    assert remote_file_transfer_dir(config) == "/home/site/transfer"
    with pytest.raises(ValueError, match="invalid NAS subdirectory"):
        remote_file_transfer_dir(config, "../escape")
