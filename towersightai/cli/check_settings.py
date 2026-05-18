from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable

from towersightai.camera.preview import launch_camera_previews, run_camera_health_check
from towersightai.config.env_loader import CameraInspection, inspect_env, load_settings_from_env
from towersightai.inference.hailo_check import check_hailo_installation


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    inspection = inspect_env(args.env)
    _print_config_report(inspection.env_path, inspection.env_exists, inspection.settings_loadable, inspection.settings_error)
    _print_camera_report(inspection.cameras)

    selected_cameras = _select_cameras(inspection.configured_cameras, args.camera)
    exit_code = 0

    if args.health_check_cameras:
        print("\n[CAMERA HEALTH]")
        for camera in selected_cameras:
            result = run_camera_health_check(camera, timeout_seconds=args.timeout, latency_ms=args.latency_ms)
            status = "OK" if result.healthy else "NG"
            detail = "frame received" if result.healthy else result.error
            print(f"{result.camera_id:<14} {result.role:<14} {status:<3} {detail}")
            if not result.healthy:
                exit_code = 2

    if args.preview_cameras:
        print("\n[CAMERA PREVIEW]")
        if not selected_cameras:
            print("No fully configured cameras are available for preview.")
            exit_code = 2
        elif args.dry_run:
            for camera in selected_cameras:
                print(f"{camera.id or camera.index}: {camera.redacted_rtsp_url}")
        else:
            print("Launching GStreamer preview windows. Press Ctrl+C to stop.")
            processes = launch_camera_previews(selected_cameras, latency_ms=args.latency_ms)
            try:
                while any(process.poll() is None for process in processes):
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("Stopping camera previews...")
                for process in processes:
                    if process.poll() is None:
                        process.terminate()

    if args.check_hailo:
        print("\n[HAILO]")
        try:
            settings = load_settings_from_env(args.env)
        except Exception as exc:  # noqa: BLE001 - CLI report should be explicit and safe.
            print(f"NG  settings not loadable for Hailo check: {exc}")
            exit_code = 2
        else:
            hailo_result = check_hailo_installation(settings, timeout_seconds=args.timeout)
            for item in hailo_result.items:
                print(f"{'OK' if item.ok else 'NG':<3} {item.name:<22} {item.detail}")
            if not hailo_result.ok:
                exit_code = 2

    print("\n[SAFETY]")
    print("Runtime OK signal: BLOCKED")
    print("Reason: this command is inspection-only and never authorizes PLC OK.")
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect TowerSightAI .env, cameras, and Hailo installation.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to the environment file to inspect.")
    parser.add_argument("--camera", action="append", help="Camera ID or role to inspect. May be passed multiple times.")
    parser.add_argument("--preview-cameras", action="store_true", help="Launch GStreamer preview windows for configured cameras.")
    parser.add_argument("--health-check-cameras", action="store_true", help="Receive one frame from each configured camera with GStreamer.")
    parser.add_argument("--check-hailo", action="store_true", help="Check HailoRT, Hailo GStreamer elements, HEF, and postprocess paths.")
    parser.add_argument("--dry-run", action="store_true", help="Print redacted targets without launching preview processes.")
    parser.add_argument("--timeout", type=int, default=10, help="Timeout in seconds for health and Hailo checks.")
    parser.add_argument("--latency-ms", type=int, default=100, help="RTSP latency for generated GStreamer pipelines.")
    return parser


def _print_config_report(env_path: Path, env_exists: bool, settings_loadable: bool, settings_error: str | None) -> None:
    print("[CONFIG]")
    print(f"ENV_PATH: {env_path}")
    print(f"ENV_EXISTS: {'OK' if env_exists else 'NG'}")
    print(f"FULL_SETTINGS: {'OK' if settings_loadable else 'NG'}")
    if settings_error:
        print(f"SETTINGS_ERROR: {settings_error}")


def _print_camera_report(cameras: Iterable[CameraInspection]) -> None:
    print("\n[CAMERAS]")
    for camera in cameras:
        label = camera.id or f"camera_{camera.index}"
        role = camera.role or "unknown"
        status = "CONFIGURED" if camera.configured else "MISSING"
        detail = camera.redacted_rtsp_url or ", ".join(camera.missing_fields)
        print(f"{label:<14} {role:<14} {status:<10} {detail}")


def _select_cameras(cameras: Iterable[CameraInspection], selectors: list[str] | None) -> list[CameraInspection]:
    camera_list = list(cameras)
    if not selectors:
        return camera_list
    selector_set = set(selectors)
    return [camera for camera in camera_list if camera.id in selector_set or camera.role in selector_set]


if __name__ == "__main__":
    sys.exit(main())
