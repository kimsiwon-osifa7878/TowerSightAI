from __future__ import annotations

import argparse
import json
from pathlib import Path

from towersightai.config.env_loader import load_settings_from_env
from towersightai.storage.raw_data import RawDataManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload completed TowerSightAI raw-data days to Synology NAS.")
    parser.add_argument("--env", default=".env", help="Path to the deployment .env file.")
    parser.add_argument(
        "--include-current-day",
        action="store_true",
        help="Also upload today's current snapshot. Run after stopping writers for a stable test archive.",
    )
    args = parser.parse_args()
    settings = load_settings_from_env(Path(args.env))
    if not settings.raw_storage.enabled:
        print(json.dumps({"ok": False, "reason": "RAW_DATA_ENABLED is false"}))
        return 2
    manager = RawDataManager(settings.raw_storage, (camera.id for camera in settings.active_cameras))
    result = manager.sync_completed_days(include_current_day=args.include_current_day)
    print(
        json.dumps(
            {
                "ok": not result.errors,
                "uploaded_days": result.uploaded_days,
                "retained_days": result.retained_days,
                "deleted_days": result.deleted_days,
                "errors": result.errors,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
