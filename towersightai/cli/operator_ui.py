from __future__ import annotations

import argparse
from pathlib import Path

from towersightai.config.env_loader import load_settings_from_env
from towersightai.state_machine.core import ParkingState
from towersightai.ui.model import (
    AlignmentResult,
    PlcConnectionState,
    build_operator_display,
)
from towersightai.ui.pyqt_app import launch_operator_ui


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the TowerSightAI PyQt6 operator console.")
    parser.add_argument("--env", default=".env", help="Path to the deployment .env file.")
    parser.add_argument("--windowed", action="store_true", help="Run in a normal window instead of fullscreen.")
    args = parser.parse_args()

    settings = load_settings_from_env(Path(args.env))
    model = build_operator_display(
        state=ParkingState.IDLE,
        cameras=settings.cameras,
        alignment=AlignmentResult.UNKNOWN,
        plc_state=PlcConnectionState.UNKNOWN,
        fullscreen=settings.ui_fullscreen and not args.windowed,
    )
    return launch_operator_ui(model, settings=settings)


if __name__ == "__main__":
    raise SystemExit(main())
