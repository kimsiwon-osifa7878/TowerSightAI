"""Printable checkerboard generation for camera intrinsics calibration.

The board is described by its number of squares (columns × rows) and the square edge in
millimetres. OpenCV detects the *inner* corners, so a 10 × 7 board yields a 9 × 6 corner pattern.
Three outputs are produced from the same spec so the operator can print whichever is convenient:

- PDF with exact millimetre geometry (preferred: print at 100 % / "actual size"),
- SVG with millimetre units (prints true to size from a browser),
- PNG at 300 DPI (pixel count matches the paper size exactly).

The generated files are documentation for a physical object; nothing here touches safety state.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path


PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    # width × height in portrait orientation
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "LETTER": (215.9, 279.4),
}
MM_PER_INCH = 25.4
PDF_POINTS_PER_MM = 72.0 / MM_PER_INCH


@dataclass(frozen=True)
class CheckerboardSpec:
    """Physical checkerboard: ``columns`` × ``rows`` squares of ``square_mm`` each."""

    columns: int = 10
    rows: int = 7
    square_mm: float = 25.0

    def __post_init__(self) -> None:
        if self.columns < 3 or self.rows < 3:
            raise ValueError("Checkerboard needs at least 3 x 3 squares.")
        if self.columns == self.rows:
            # An asymmetric board has an unambiguous orientation, which OpenCV needs for a
            # stable corner ordering across views.
            raise ValueError("Checkerboard columns and rows must differ.")
        if self.square_mm <= 0:
            raise ValueError("Checkerboard square size must be positive.")

    @property
    def inner_corners(self) -> tuple[int, int]:
        """OpenCV ``patternSize`` (corners across, corners down)."""
        return (self.columns - 1, self.rows - 1)

    @property
    def board_width_mm(self) -> float:
        return self.columns * self.square_mm

    @property
    def board_height_mm(self) -> float:
        return self.rows * self.square_mm

    @property
    def label(self) -> str:
        return f"{self.columns}x{self.rows} squares, {self.square_mm:g} mm"

    def file_stem(self, paper: str) -> str:
        return f"checkerboard-{self.columns}x{self.rows}-{self.square_mm:g}mm-{paper.lower()}"


def fits_paper(spec: CheckerboardSpec, paper: str, *, margin_mm: float = 10.0) -> bool:
    width, height = paper_size_mm(paper, landscape=True)
    return (
        spec.board_width_mm + 2 * margin_mm <= width
        and spec.board_height_mm + 2 * margin_mm <= height
    )


def paper_size_mm(paper: str, *, landscape: bool) -> tuple[float, float]:
    try:
        width, height = PAPER_SIZES_MM[paper.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported paper size {paper!r}; choose one of {sorted(PAPER_SIZES_MM)}.") from exc
    return (height, width) if landscape else (width, height)


def _board_origin_mm(spec: CheckerboardSpec, paper: str) -> tuple[float, float, float, float]:
    """Return (page_w, page_h, x0, y0) with the board centred on a landscape page."""
    page_w, page_h = paper_size_mm(paper, landscape=True)
    x0 = (page_w - spec.board_width_mm) / 2
    y0 = (page_h - spec.board_height_mm) / 2
    return page_w, page_h, x0, y0


def _caption(spec: CheckerboardSpec) -> str:
    cols, rows = spec.inner_corners
    return (
        f"TowerSightAI checkerboard {spec.columns}x{spec.rows} @ {spec.square_mm:g} mm "
        f"(inner corners {cols}x{rows}) - print at 100% / actual size, no scaling"
    )


def write_checkerboard_svg(spec: CheckerboardSpec, paper: str, path: Path) -> Path:
    page_w, page_h, x0, y0 = _board_origin_mm(spec, paper)
    rects = []
    for row in range(spec.rows):
        for col in range(spec.columns):
            if (row + col) % 2 == 0:
                x = x0 + col * spec.square_mm
                y = y0 + row * spec.square_mm
                rects.append(
                    f'<rect x="{x:.3f}" y="{y:.3f}" width="{spec.square_mm:.3f}" '
                    f'height="{spec.square_mm:.3f}" fill="#000"/>'
                )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{page_w}mm" height="{page_h}mm" '
        f'viewBox="0 0 {page_w} {page_h}">\n'
        f'<rect width="{page_w}" height="{page_h}" fill="#fff"/>\n'
        + "\n".join(rects)
        + f'\n<text x="{x0:.3f}" y="{page_h - 4:.3f}" font-family="sans-serif" font-size="3" fill="#000">'
        f"{_caption(spec)}</text>\n</svg>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return path


def write_checkerboard_pdf(spec: CheckerboardSpec, paper: str, path: Path) -> Path:
    """Write a minimal single-page PDF with exact millimetre geometry (no external library)."""
    page_w, page_h, x0, y0 = _board_origin_mm(spec, paper)
    pt = PDF_POINTS_PER_MM
    ops = ["0 g"]
    for row in range(spec.rows):
        for col in range(spec.columns):
            if (row + col) % 2 == 0:
                x = (x0 + col * spec.square_mm) * pt
                # PDF origin is bottom-left; flip rows so the SVG/PNG/PDF look identical.
                y = (page_h - y0 - (row + 1) * spec.square_mm) * pt
                ops.append(f"{x:.3f} {y:.3f} {spec.square_mm * pt:.3f} {spec.square_mm * pt:.3f} re f")
    caption = _caption(spec).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops.append(f"BT /F1 8 Tf {x0 * pt:.3f} {(4.0) * pt:.3f} Td ({caption}) Tj ET")
    content = "\n".join(ops).encode("ascii")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w * pt:.3f} {page_h * pt:.3f}] "
            f"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ).encode("ascii"),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def render_checkerboard_png(spec: CheckerboardSpec, paper: str, path: Path, *, dpi: int = 300) -> Path:
    """Write a PNG whose pixel size equals the landscape page at ``dpi`` (pure Python, no cv2)."""
    page_w, page_h, x0, y0 = _board_origin_mm(spec, paper)
    px_per_mm = dpi / MM_PER_INCH
    width = int(round(page_w * px_per_mm))
    height = int(round(page_h * px_per_mm))
    square_px = spec.square_mm * px_per_mm
    x0_px = x0 * px_per_mm
    y0_px = y0 * px_per_mm

    rows_bytes = bytearray()
    for y in range(height):
        row = bytearray(b"\x00")  # PNG filter type 0
        board_row = (y - y0_px) / square_px
        in_rows = 0 <= board_row < spec.rows
        row_index = int(board_row) if in_rows else -1
        line = bytearray(b"\xff" * width)
        if in_rows:
            for col in range(spec.columns):
                if (row_index + col) % 2 == 0:
                    start = int(round(x0_px + col * square_px))
                    end = int(round(x0_px + (col + 1) * square_px))
                    line[start:end] = b"\x00" * (end - start)
        row += line
        rows_bytes += row

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return len(payload).to_bytes(4, "big") + kind + payload + crc.to_bytes(4, "big")

    pixels_per_metre = int(round(dpi / MM_PER_INCH * 1000))
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])  # 8-bit grayscale
    phys = pixels_per_metre.to_bytes(4, "big") * 2 + b"\x01"
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"pHYs", phys)
        + chunk(b"IDAT", zlib.compress(bytes(rows_bytes), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def generate_checkerboard_files(
    spec: CheckerboardSpec,
    paper: str,
    out_dir: Path,
    *,
    dpi: int = 300,
) -> tuple[Path, Path, Path]:
    """Write PDF, SVG, and PNG for ``spec`` on ``paper`` (landscape) into ``out_dir``."""
    if not fits_paper(spec, paper):
        raise ValueError(
            f"{spec.label} ({spec.board_width_mm:g}x{spec.board_height_mm:g} mm) does not fit on {paper} "
            "with a 10 mm margin."
        )
    stem = spec.file_stem(paper)
    pdf = write_checkerboard_pdf(spec, paper, out_dir / f"{stem}.pdf")
    svg = write_checkerboard_svg(spec, paper, out_dir / f"{stem}.svg")
    png = render_checkerboard_png(spec, paper, out_dir / f"{stem}.png", dpi=dpi)
    return pdf, svg, png
