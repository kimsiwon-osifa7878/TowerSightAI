from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from towersightai.config.env_loader import load_settings_from_env
from towersightai.runtime_logging import configure_runtime_logging
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import (
    AlignmentResult,
    PlcConnectionState,
    build_operator_display,
)
from towersightai.ui.pyqt_app import launch_operator_ui


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    settings = load_settings_from_env(Path(args.env))
    effective_log_level = os.environ.get("LOG_LEVEL", settings.log_level)
    log_path = configure_runtime_logging(effective_log_level)
    logging.getLogger(__name__).info(
        "application-start env=%s cwd=%s python=%s log=%s",
        Path(args.env).resolve(strict=False),
        Path.cwd(),
        sys.executable,
        log_path.resolve(strict=False),
    )
    os.environ["TOWERSIGHTAI_LOG_LEVEL"] = effective_log_level
    model = build_operator_display(
        state=ParkingState.IDLE,
        cameras=settings.active_cameras,
        alignment=AlignmentResult.UNKNOWN,
        plc_state=PlcConnectionState.UNKNOWN,
        fullscreen=_resolve_fullscreen(
            configured=settings.ui_fullscreen,
            force_fullscreen=args.fullscreen,
            force_windowed=args.windowed,
        ),
        birdview_available=settings.birdview_enabled,
    )
    return launch_operator_ui(model, settings=settings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the TowerSightAI PyQt6 operator console.")
    parser.add_argument("--env", default=".env", help="Path to the deployment .env file.")
    display_group = parser.add_mutually_exclusive_group()
    display_group.add_argument("--fullscreen", action="store_true", help="Force fullscreen mode.")
    display_group.add_argument("--windowed", action="store_true", help="Force normal window mode.")
    return parser


def _resolve_fullscreen(*, configured: bool, force_fullscreen: bool, force_windowed: bool) -> bool:
    if force_fullscreen:
        return True
    if force_windowed:
        return False
    return configured


if __name__ == "__main__":
    raise SystemExit(main())
