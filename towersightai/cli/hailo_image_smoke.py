from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from towersightai.config.env_loader import load_settings_from_env
from towersightai.inference.hailo_check import check_hailo_installation
from towersightai.inference.image_smoke import (
    DEFAULT_OUTPUT_IMAGE,
    image_smoke_command,
    redacted_command,
    run_image_hailo_smoke,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    settings = load_settings_from_env(args.env)
    event_path = args.event_path
    output_image = None if args.no_output_image else args.output_image

    command = image_smoke_command(
        settings,
        image_path=args.image,
        output_image_path=output_image,
        display=args.display,
        show_fps=args.show_fps,
        min_confidence=args.min_confidence,
        gst_launch=args.gst_launch,
    )
    print("[HAILO IMAGE SMOKE]")
    print(f"IMAGE: {args.image}")
    print(f"EVENTS: {event_path}")
    print(f"OUTPUT_IMAGE: {output_image if output_image is not None else 'disabled'}")
    print(f"GST_COMMAND: {redacted_command(command)}")
    print("Runtime OK signal: BLOCKED")
    print("Reason: this command is hardware validation only and never authorizes PLC OK.")

    if args.check_installation:
        print("\n[HAILO INSTALLATION]")
        install = check_hailo_installation(settings, timeout_seconds=args.timeout)
        for item in install.items:
            print(f"{'OK' if item.ok else 'NG':<3} {item.name:<22} {item.detail}")
        if not install.ok and not args.run:
            return 2

    if not args.run:
        print("\nDRY_RUN: pass --run with RUN_HARDWARE_TESTS=1 on the Ubuntu/Hailo target to execute.")
        return 0

    result = run_image_hailo_smoke(
        settings,
        image_path=args.image,
        event_path=event_path,
        output_image_path=output_image,
        camera_id=args.camera_id,
        display=args.display,
        show_fps=args.show_fps,
        min_confidence=args.min_confidence,
        timeout_seconds=args.timeout,
        gst_launch=args.gst_launch,
        require_opt_in=not args.allow_without_opt_in,
    )
    print("\n[RESULT]")
    print(f"PIPELINE: {'OK' if result.ok else 'NG'}")
    if result.reason:
        print(f"REASON: {result.reason}")
    if result.stdout.strip():
        print("STDOUT:")
        print(result.stdout.strip())
    if result.stderr.strip():
        print("STDERR:")
        print(result.stderr.strip())

    events = _read_jsonl(event_path)
    print(f"DETECTIONS: {len(events)}")
    for event in events[: args.max_events]:
        bbox = event.get("bbox", {})
        print(
            f"- {event.get('camera_id')} {event.get('label')} {event.get('confidence'):.3f} "
            f"bbox=({bbox.get('x')}, {bbox.get('y')}, {bbox.get('w')}, {bbox.get('h')})"
        )
    if events:
        return 0
    print("SAFETY: no confident detections were produced; keep NG/wait until investigated.")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Hailo detection on one sanitized sample image.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to the deployment .env file.")
    parser.add_argument("--image", type=Path, required=True, help="Sample image path to run through Hailo.")
    parser.add_argument("--camera-id", default="sample_image", help="Camera/event ID to attach to sample detections.")
    parser.add_argument("--event-path", type=Path, default=Path("artifacts/hailo/sample-detections.jsonl"))
    parser.add_argument("--output-image", type=Path, default=DEFAULT_OUTPUT_IMAGE, help="Annotated PNG written by hailooverlay.")
    parser.add_argument("--no-output-image", action="store_true", help="Use fakesink instead of writing an annotated image.")
    parser.add_argument("--display", action="store_true", help="Display the overlaid frame instead of writing a PNG.")
    parser.add_argument("--show-fps", action="store_true", help="Show fpsdisplaysink text in display mode.")
    parser.add_argument("--min-confidence", type=float, default=0.3, help="Minimum confidence for normalized events.")
    parser.add_argument("--timeout", type=int, default=30, help="Pipeline timeout in seconds.")
    parser.add_argument("--gst-launch", default="gst-launch-1.0", help="GStreamer launcher executable.")
    parser.add_argument("--check-installation", action="store_true", help="Run Hailo installation checks before smoke execution.")
    parser.add_argument("--run", action="store_true", help="Actually run gst-launch. Otherwise only print the command.")
    parser.add_argument(
        "--allow-without-opt-in",
        action="store_true",
        help="Allow execution without RUN_HARDWARE_TESTS=1. Intended only for manual target debugging.",
    )
    parser.add_argument("--max-events", type=int, default=20, help="Maximum detections to print from the JSONL file.")
    return parser


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


if __name__ == "__main__":
    sys.exit(main())
