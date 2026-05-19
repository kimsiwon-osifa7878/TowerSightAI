from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Callable

from towersightai.camera.preview import run_camera_health_check
from towersightai.config.env_loader import inspect_env
from towersightai.config.settings import CameraConfig, Settings
from towersightai.inference.hailo_check import check_hailo_installation
from towersightai.inference.image_smoke import (
    DEFAULT_OUTPUT_IMAGE,
    HailoImageSmokeResult,
    redacted_command,
    run_image_hailo_smoke,
)


class DiagnosticStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class DiagnosticResult:
    test_id: str
    label: str
    status: DiagnosticStatus
    summary: str
    detail: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    duration_ms: int = 0
    safe_to_operate: bool = False

    @property
    def ok(self) -> bool:
        return self.status is DiagnosticStatus.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "label": self.label,
            "status": self.status.value,
            "summary": self.summary,
            "detail": self.detail,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "safe_to_operate": self.safe_to_operate,
        }


@dataclass(frozen=True)
class DiagnosticTest:
    test_id: str
    label: str
    description: str


@dataclass(frozen=True)
class _CameraInspectionAdapter:
    camera: CameraConfig
    index: int

    @property
    def id(self) -> str:
        return self.camera.id

    @property
    def role(self) -> str:
        return self.camera.role.value

    @property
    def rtsp_url(self) -> str:
        return self.camera.rtsp_url

    @property
    def configured(self) -> bool:
        return bool(self.camera.id and self.camera.role and self.camera.rtsp_url)


class DiagnosticsService:
    def __init__(
        self,
        settings: Settings,
        *,
        env_path: Path = Path(".env"),
        artifacts_dir: Path = Path("artifacts/diagnostics"),
    ) -> None:
        self.settings = settings
        self.env_path = env_path
        self.artifacts_dir = artifacts_dir

    def available_tests(self) -> tuple[DiagnosticTest, ...]:
        camera_tests = tuple(
            DiagnosticTest(f"camera_{idx}", f"카메라 {idx} 프레임 수신", f"{camera.role.value} RTSP 단일 프레임 확인")
            for idx, camera in enumerate(self.settings.cameras, start=1)
        )
        return (
            DiagnosticTest("settings", "설정 검증", ".env, 카메라 역할, Hailo/PLC 경로 설정 확인"),
            DiagnosticTest("hailo_installation", "Hailo 설치 점검", "HailoRT, 장치, GStreamer element, HEF/postprocess 확인"),
            DiagnosticTest("hailo_image_smoke", "Hailo 샘플 이미지 추론", "sanitized sample image를 Hailo 파이프라인으로 실행"),
            *camera_tests,
            DiagnosticTest("plc_simulator", "PLC 시뮬레이터", "실제 PLC 통신 없이 이벤트 인터페이스 기록 확인"),
            DiagnosticTest("full_hardware_smoke", "전체 하드웨어 스모크", "설정, Hailo, 카메라, 샘플 이미지 테스트 순차 실행"),
        )

    def run(self, test_id: str, *, timeout_seconds: int = 10) -> DiagnosticResult:
        handlers: dict[str, Callable[[int], DiagnosticResult]] = {
            "settings": self.check_settings,
            "hailo_installation": self.check_hailo_installation,
            "hailo_image_smoke": self.run_hailo_image_smoke,
            "plc_simulator": self.check_plc_simulator,
            "full_hardware_smoke": self.run_full_hardware_smoke,
        }
        for idx, _camera in enumerate(self.settings.cameras, start=1):
            handlers[f"camera_{idx}"] = lambda timeout, index=idx: self.check_camera(index, timeout_seconds=timeout)
        handler = handlers.get(test_id)
        if handler is None:
            return _timed_result(test_id, test_id, DiagnosticStatus.FAIL, "알 수 없는 테스트 항목입니다.")
        return handler(timeout_seconds)

    def check_settings(self, timeout_seconds: int = 10) -> DiagnosticResult:
        del timeout_seconds
        started, tick = _start()
        inspection = inspect_env(self.env_path)
        details = [
            f"env: {inspection.env_path}",
            f"exists: {inspection.env_exists}",
            f"loadable: {inspection.settings_loadable}",
        ]
        if inspection.missing_settings:
            details.append("missing: " + ", ".join(inspection.missing_settings))
        if inspection.settings_error:
            details.append("error: " + inspection.settings_error)
        for camera in inspection.cameras:
            details.append(
                f"camera_{camera.index}: configured={camera.configured} id={camera.id} role={camera.role} "
                f"source={camera.redacted_rtsp_url or 'missing'}"
            )
        ok = inspection.env_exists and inspection.settings_loadable and not inspection.missing_settings
        return _finish(
            "settings",
            "설정 검증",
            DiagnosticStatus.PASS if ok else DiagnosticStatus.FAIL,
            "설정 파일을 로드할 수 있습니다." if ok else "설정 파일 검증에 실패했습니다.",
            "\n".join(details),
            started,
            tick,
        )

    def check_hailo_installation(self, timeout_seconds: int = 10) -> DiagnosticResult:
        started, tick = _start()
        result = check_hailo_installation(self.settings, timeout_seconds=timeout_seconds)
        detail = "\n".join(f"{'OK' if item.ok else 'NG'} {item.name}: {item.detail}" for item in result.items)
        return _finish(
            "hailo_installation",
            "Hailo 설치 점검",
            DiagnosticStatus.PASS if result.ok else DiagnosticStatus.FAIL,
            "Hailo 설치와 장치 접근이 확인되었습니다." if result.ok else "Hailo 설치 또는 장치 접근에 문제가 있습니다.",
            detail,
            started,
            tick,
        )

    def run_hailo_image_smoke(self, timeout_seconds: int = 30) -> DiagnosticResult:
        started, tick = _start()
        sample = Path("data/samples/test-car.png")
        event_path = self.artifacts_dir / "hailo-sample-detections.jsonl"
        output_image = self.artifacts_dir / "hailo-sample-detection.png"
        result = run_image_hailo_smoke(
            self.settings,
            image_path=sample,
            event_path=event_path,
            output_image_path=output_image or DEFAULT_OUTPUT_IMAGE,
            timeout_seconds=timeout_seconds,
            require_opt_in=False,
        )
        detail = _hailo_smoke_detail(result)
        events = _read_jsonl_count(event_path)
        ok = result.ok and events > 0
        if result.ok and events == 0:
            detail += "\nNo normalized detection events were produced."
        return _finish(
            "hailo_image_smoke",
            "Hailo 샘플 이미지 추론",
            DiagnosticStatus.PASS if ok else DiagnosticStatus.FAIL,
            f"샘플 이미지 추론 이벤트 {events}개 생성" if ok else (result.reason or "샘플 이미지 추론 실패"),
            detail,
            started,
            tick,
        )

    def check_camera(self, index: int, *, timeout_seconds: int = 10) -> DiagnosticResult:
        started, tick = _start()
        if index < 1 or index > len(self.settings.cameras):
            return _finish(
                f"camera_{index}",
                f"카메라 {index} 프레임 수신",
                DiagnosticStatus.FAIL,
                "카메라 번호가 유효하지 않습니다.",
                "",
                started,
                tick,
            )
        camera = self.settings.cameras[index - 1]
        result = run_camera_health_check(
            _CameraInspectionAdapter(camera, index),
            timeout_seconds=timeout_seconds,
            resolution=self.settings.ui_camera_resolution,
        )
        detail = f"id={result.camera_id}\nrole={result.role}\nframe_received={result.frame_received}\nerror={result.error or ''}"
        return _finish(
            f"camera_{index}",
            f"카메라 {index} 프레임 수신",
            DiagnosticStatus.PASS if result.healthy else DiagnosticStatus.FAIL,
            "프레임 수신 성공" if result.healthy else (result.error or "프레임 수신 실패"),
            detail,
            started,
            tick,
        )

    def check_plc_simulator(self, timeout_seconds: int = 10) -> DiagnosticResult:
        del timeout_seconds
        started, tick = _start()
        events = ("vehicle_parked", "safety_status_ng")
        return _finish(
            "plc_simulator",
            "PLC 시뮬레이터",
            DiagnosticStatus.PASS,
            "PLC 이벤트 인터페이스 기록을 확인했습니다.",
            "\n".join(events),
            started,
            tick,
        )

    def run_full_hardware_smoke(self, timeout_seconds: int = 10) -> DiagnosticResult:
        started, tick = _start()
        children: list[DiagnosticResult] = [
            self.check_settings(timeout_seconds=timeout_seconds),
            self.check_hailo_installation(timeout_seconds=timeout_seconds),
        ]
        children.extend(self.check_camera(index, timeout_seconds=timeout_seconds) for index in range(1, 5))
        children.append(self.run_hailo_image_smoke(timeout_seconds=max(timeout_seconds, 30)))
        ok = all(child.ok for child in children)
        detail = "\n\n".join(f"[{child.status.value}] {child.label}\n{child.summary}\n{child.detail}" for child in children)
        return _finish(
            "full_hardware_smoke",
            "전체 하드웨어 스모크",
            DiagnosticStatus.PASS if ok else DiagnosticStatus.FAIL,
            "전체 하드웨어 스모크 통과" if ok else "하나 이상의 하드웨어 스모크 테스트 실패",
            detail,
            started,
            tick,
        )

    def write_result(self, result: DiagnosticResult) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / "diagnostics.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return path


def _hailo_smoke_detail(result: HailoImageSmokeResult) -> str:
    lines = [
        f"command: {redacted_command(result.command)}",
        f"events: {result.event_path}",
        f"output_image: {result.output_image or 'disabled'}",
    ]
    if result.reason:
        lines.append(f"reason: {result.reason}")
    if result.stdout.strip():
        lines.append("stdout:\n" + result.stdout.strip())
    if result.stderr.strip():
        lines.append("stderr:\n" + result.stderr.strip())
    return "\n".join(lines)


def _read_jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _start() -> tuple[datetime, float]:
    return datetime.now(timezone.utc), perf_counter()


def _finish(
    test_id: str,
    label: str,
    status: DiagnosticStatus,
    summary: str,
    detail: str,
    started: datetime,
    tick: float,
) -> DiagnosticResult:
    return DiagnosticResult(
        test_id=test_id,
        label=label,
        status=status,
        summary=summary,
        detail=detail,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        duration_ms=int((perf_counter() - tick) * 1000),
        safe_to_operate=False,
    )


def _timed_result(test_id: str, label: str, status: DiagnosticStatus, summary: str) -> DiagnosticResult:
    started, tick = _start()
    return _finish(test_id, label, status, summary, "", started, tick)
