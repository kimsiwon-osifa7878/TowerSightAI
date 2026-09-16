"""Generate a printable calibration checkerboard (PDF + SVG + PNG)."""

from __future__ import annotations

import argparse
from pathlib import Path

from towersightai.calibration.checkerboard import (
    PAPER_SIZES_MM,
    CheckerboardSpec,
    generate_checkerboard_files,
)


DEFAULT_OUT_DIR = Path("data/calibration/checkerboard")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write a printable checkerboard for camera calibration.")
    parser.add_argument("--columns", type=int, default=10, help="squares across (default 10)")
    parser.add_argument("--rows", type=int, default=7, help="squares down (default 7)")
    parser.add_argument("--square-mm", type=float, default=25.0, help="square edge in mm (default 25)")
    parser.add_argument("--paper", default="A4", choices=sorted(PAPER_SIZES_MM), help="paper size (landscape)")
    parser.add_argument("--dpi", type=int, default=300, help="PNG resolution (default 300)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = CheckerboardSpec(columns=args.columns, rows=args.rows, square_mm=args.square_mm)
    pdf, svg, png = generate_checkerboard_files(spec, args.paper, args.out_dir, dpi=args.dpi)
    cols, rows = spec.inner_corners
    print(f"checkerboard {spec.label} on {args.paper} landscape (inner corners {cols}x{rows})")
    for path in (pdf, svg, png):
        print(f"  {path}")
    print("Print at 100% / actual size. Verify one square with a ruler before use.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
