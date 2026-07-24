from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from towersightai.config.env_loader import load_settings_from_env
from towersightai.config.settings import Settings
from towersightai.runtime_logging import path_diagnostic, redact_sensitive_text


RUNTIME_DIR = Path("artifacts/runtime")
LOG_TAIL_LINES = 160
LOG_TAIL_CHARS = 24_000


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect existing TowerSightAI AI logs without starting cameras, GStreamer, or inference."
    )
    parser.add_argument("--env", default=".env", help="Deployment .env used only through the typed settings loader.")
    parser.add_argument("--output", default="artifacts/runtime/ai-diagnostics.txt", help="Destination report path.")
    args = parser.parse_args()

    output_path = Path(args.output)
    report = collect_ai_diagnostics(env_path=Path(args.env), runtime_dir=RUNTIME_DIR)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"TowerSightAI AI diagnostics written: {output_path.resolve(strict=False)}")
    return 0


def collect_ai_diagnostics(*, env_path: Path, runtime_dir: Path) -> str:
    lines = [
        "TowerSightAI AI diagnostics",
        f"collected-at={datetime.now(timezone.utc).isoformat()}",
        "collection-mode=existing-files-only",
        "inference-started=false",
        "",
        "[SYSTEM]",
        f"platform={platform.platform()}",
        f"python={sys.version.replace(chr(10), ' ')}",
        f"python-executable={sys.executable}",
        f"cwd={Path.cwd()}",
        f"env-path={env_path.resolve(strict=False)}",
        f"runtime-dir={runtime_dir.resolve(strict=False)}",
    ]
    for executable in ("gst-launch-1.0", "gst-inspect-1.0", "hailortcli"):
        lines.append(f"executable[{executable}]={shutil.which(executable) or 'missing'}")

    settings: Settings | None = None
    try:
        settings = load_settings_from_env(env_path)
    except Exception as exc:  # noqa: BLE001 - collector must preserve partial evidence.
        lines.extend(("", "[SETTINGS]", f"status=error", f"error={redact_sensitive_text(exc)}"))
    else:
        lines.extend(_settings_report(settings))

    lines.extend(_run_status_report(runtime_dir))
    lines.extend(_event_report(runtime_dir))
    lines.extend(_log_report(runtime_dir))
    lines.extend(_fast_alpr_cache_report(settings))
    return "\n".join(redact_sensitive_text(line) for line in lines) + "\n"


def _settings_report(settings: Settings) -> list[str]:
    resources = (
        ("general-hef", settings.hailo_hef_path),
        ("general-postprocess", settings.hailo_postprocess_so),
        ("vehicle-hef", settings.hailo_vehicle_detection_hef_path),
        ("vehicle-config", settings.hailo_vehicle_detection_config_path),
        ("vehicle-postprocess", settings.hailo_vehicle_detection_postprocess_so),
        ("person-hef", settings.hailo_person_presence_hef_path),
        ("person-config", settings.hailo_person_presence_config_path),
        ("person-postprocess", settings.hailo_person_presence_postprocess_so),
        ("person-crop", settings.hailo_person_presence_crop_so),
    )
    tappas_venv = settings.tappas_workspace / "hailo_tappas_venv"
    lines = [
        "",
        "[SETTINGS]",
        "status=loaded",
        f"log-level={settings.log_level}",
        f"hailo-network-name={settings.hailo_network_name}",
        f"fast-alpr-detector-model={settings.fast_alpr_detector_model}",
        f"fast-alpr-ocr-model={settings.fast_alpr_ocr_model}",
        f"tappas-workspace={path_diagnostic(settings.tappas_workspace)}",
        f"tappas-venv={path_diagnostic(tappas_venv)}",
    ]
    for name, path in resources:
        lines.append(f"resource[{name}]={path_diagnostic(path)}")
    for camera in settings.cameras:
        lines.append(
            f"camera[{camera.id}]=role:{camera.role.value} host:{_rtsp_host(camera.rtsp_url)} "
            f"rotation:{camera.rotation_degrees}"
        )
    return lines


def _run_status_report(runtime_dir: Path) -> list[str]:
    lines = ["", "[LATEST RUN STATUS]"]
    status_paths = tuple(sorted(runtime_dir.rglob("*.run-status.json"))) if runtime_dir.exists() else ()
    status_paths += tuple(sorted(runtime_dir.rglob("run-status.json"))) if runtime_dir.exists() else ()
    status_paths = tuple(dict.fromkeys(status_paths))
    if not status_paths:
        lines.append("missing")
        return lines
    for path in status_paths:
        lines.append(f"--- {path.resolve(strict=False)}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"status-file-error={exc}")
            continue
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return lines


def _event_report(runtime_dir: Path) -> list[str]:
    lines = ["", "[EVENT FILES]"]
    event_paths = tuple(sorted(runtime_dir.rglob("*.jsonl"))) if runtime_dir.exists() else ()
    if not event_paths:
        lines.append("missing")
        return lines
    for path in event_paths:
        record_count = 0
        last_record = ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                for raw_line in fp:
                    if raw_line.strip():
                        record_count += 1
                        last_record = raw_line.strip()
        except OSError as exc:
            lines.append(f"{path.resolve(strict=False)} read-error={exc}")
            continue
        lines.append(
            f"{path.resolve(strict=False)} records={record_count} "
            f"last={last_record or 'empty'}"
        )
    return lines


def _log_report(runtime_dir: Path) -> list[str]:
    lines = ["", "[LOG TAILS]"]
    log_paths = tuple(sorted(runtime_dir.rglob("*.log"))) if runtime_dir.exists() else ()
    if not log_paths:
        lines.append("missing")
        return lines
    for path in log_paths:
        lines.append(f"--- {path.resolve(strict=False)}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            lines.append(f"read-error={exc}")
            continue
        tail = "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
        lines.append(tail[-LOG_TAIL_CHARS:] if tail else "empty")
    return lines


def _fast_alpr_cache_report(settings: Settings | None) -> list[str]:
    lines = ["", "[FASTALPR CACHE]"]
    cache_root = Path.home() / ".cache" / "open-image-models"
    lines.append(f"cache-root={path_diagnostic(cache_root)}")
    if not cache_root.is_dir():
        lines.append("missing")
        return lines
    model_names = {
        settings.fast_alpr_detector_model if settings else "yolo-v9-t-384-license-plate-end2end",
        settings.fast_alpr_ocr_model if settings else "cct-xs-v2-global-model",
    }
    matches = [
        path
        for path in cache_root.rglob("*")
        if any(
            _model_token(name) in _model_token(path.name)
            or _model_token(path.name) in _model_token(name)
            for name in model_names
        )
    ]
    if not matches:
        lines.append("matching-model-files=missing")
        return lines
    for path in sorted(matches)[:100]:
        lines.append(f"cache-entry={path_diagnostic(path)}")
    if len(matches) > 100:
        lines.append(f"cache-entry-truncated={len(matches) - 100}")
    return lines


def _rtsp_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "invalid"


def _model_token(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


if __name__ == "__main__":
    raise SystemExit(main())
