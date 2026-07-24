from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RUNTIME_LOG = Path("artifacts/runtime/towersightai.log")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
_RTSP_CREDENTIALS = re.compile(r"(rtsp://)([^@\s/]+)@", re.IGNORECASE)


def configure_runtime_logging(
    log_level: str = "INFO",
    *,
    log_path: Path = DEFAULT_RUNTIME_LOG,
) -> Path:
    level = getattr(logging, log_level.strip().upper(), logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    if not any(getattr(handler, "_towersightai_console", False) for handler in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        console._towersightai_console = True  # type: ignore[attr-defined]
        root.addHandler(console)

    resolved_log_path = log_path.resolve()
    if not any(
        getattr(handler, "_towersightai_path", None) == resolved_log_path
        for handler in root.handlers
    ):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        file_handler._towersightai_path = resolved_log_path  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    return log_path


def redact_sensitive_text(value: Any) -> str:
    return _RTSP_CREDENTIALS.sub(r"\1***:***@", str(value))


def path_diagnostic(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    try:
        resolved = expanded.resolve(strict=False)
    except OSError:
        resolved = expanded.absolute()
    exists = expanded.exists()
    size: int | None = None
    if exists and expanded.is_file():
        try:
            size = expanded.stat().st_size
        except OSError:
            size = None
    return {
        "configured": str(path),
        "resolved": str(resolved),
        "exists": exists,
        "is_file": expanded.is_file(),
        "is_dir": expanded.is_dir(),
        "size_bytes": size,
    }


def missing_resource_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(path for path in paths if not path.expanduser().is_file())


def new_run_id(task_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-") or "ai"
    return f"{stamp}-{safe_task}"


def write_run_status(path: Path, **values: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **values,
    }
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
