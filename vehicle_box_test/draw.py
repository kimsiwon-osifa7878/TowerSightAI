"""검증 이미지 위에 그리는 도구 — 한국어 주석을 이미지 안에 새긴다.

사람이 **이미지만 보고** 판단할 수 있어야 한다는 것이 이 랩의 요구사항이다. 그래서
- 성공한 경우: 직육면체, 앞·뒤 위치선, 바퀴 좌우선, 치수(mm)를 이미지에 새기고
- 실패한 경우: 왜 안 됐는지를 한국어로 이미지 한쪽에 적는다.

cv2.putText는 한글을 못 그리므로 Pillow + Noto Sans CJK KR로 그린다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Sequence

# Noto CJK는 Ubuntu 기본 설치. 없으면 첫 번째로 찾은 CJK 폰트를 쓴다.
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
)

# BGR
COLOR_BOX = (60, 220, 60)
COLOR_BOX_TOP = (120, 255, 180)
COLOR_RAIL = (0, 215, 255)
COLOR_CIRCLE = (255, 190, 80)
COLOR_PALLET = (220, 120, 255)
COLOR_FRONT = (60, 80, 255)
COLOR_REAR = (255, 160, 60)
COLOR_WHEEL = (255, 255, 90)
COLOR_FAIL = (60, 60, 255)
COLOR_TEXT_BG = (28, 28, 32)


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def put_text(
    image,
    text: str,
    origin: tuple[int, int],
    *,
    size: int = 22,
    color: tuple[int, int, int] = (255, 255, 255),
    background: tuple[int, int, int] | None = COLOR_TEXT_BG,
    padding: int = 6,
    anchor: str = "lt",
    alpha: int = 255,
):
    """한글 포함 여러 줄 텍스트를 그린다 (BGR 이미지를 그대로 수정해 반환).

    ``alpha`` < 255면 배경 판을 **반투명**하게 깐다. 불투명한 검은 판은 그 아래에 있는
    직육면체 선을 통째로 가려서, 정작 판정해야 할 상자가 안 보였다 (2026-09-18 지적).
    """
    import numpy as np
    from PIL import Image, ImageDraw

    pil = Image.fromarray(image[:, :, ::-1])
    drawer = ImageDraw.Draw(pil)
    font = _font(size)
    lines = text.split("\n")
    widths, heights = [], []
    for line in lines:
        box = drawer.textbbox((0, 0), line, font=font)
        widths.append(box[2] - box[0])
        heights.append(box[3] - box[1] + max(size // 4, 4))
    width, height = max(widths), sum(heights)

    x, y = origin
    if anchor[0] == "r":
        x -= width
    elif anchor[0] == "c":
        x -= width // 2
    if anchor[1] == "b":
        y -= height

    if background is not None:
        rect = [x - padding, y - padding, x + width + padding, y + height + padding]
        fill = (background[2], background[1], background[0])
        if alpha >= 255:
            drawer.rectangle(rect, fill=fill)
        else:
            # 반투명 판은 합성해야 한다. PIL의 rectangle은 알파를 무시하고 덮어쓴다.
            overlay = Image.new("RGBA", pil.size, (0, 0, 0, 0))
            ImageDraw.Draw(overlay).rectangle(rect, fill=(*fill, int(alpha)))
            pil = Image.alpha_composite(pil.convert("RGBA"), overlay).convert("RGB")
            drawer = ImageDraw.Draw(pil)
    cursor = y
    for line, line_height in zip(lines, heights):
        drawer.text((x, cursor), line, font=font, fill=(color[2], color[1], color[0]))
        cursor += line_height

    image[:, :] = np.asarray(pil)[:, :, ::-1]
    return image


def denorm(points: Iterable[tuple[float, float]], shape) -> list[tuple[int, int]]:
    height, width = shape[:2]
    return [(int(round(u * width)), int(round(v * height))) for u, v in points]


def polyline(image, points: Sequence[tuple[float, float]], color, thickness=2, closed=False):
    import cv2
    import numpy as np

    pts = np.array(denorm(points, image.shape), dtype=np.int32)
    if len(pts) >= 2:
        cv2.polylines(image, [pts], closed, color, thickness, cv2.LINE_AA)
    return image


def line(image, a: tuple[float, float], b: tuple[float, float], color, thickness=2):
    import cv2

    (ax, ay), (bx, by) = denorm([a, b], image.shape)
    cv2.line(image, (ax, ay), (bx, by), color, thickness, cv2.LINE_AA)
    return image


def dot(image, point: tuple[float, float], color, radius=5):
    import cv2

    (x, y) = denorm([point], image.shape)[0]
    cv2.circle(image, (x, y), radius, color, -1, cv2.LINE_AA)
    cv2.circle(image, (x, y), radius + 2, (20, 20, 20), 1, cv2.LINE_AA)
    return image


def pixel_ruler(image, step_norm: float = 0.05):
    """대응점을 손으로 읽기 위한 정규화 좌표 눈금자."""
    import cv2

    height, width = image.shape[:2]
    overlay = image.copy()
    steps = int(round(1.0 / step_norm))
    for i in range(steps + 1):
        value = i * step_norm
        x = int(round(value * width))
        y = int(round(value * height))
        major = i % 2 == 0
        color = (0, 255, 255) if major else (90, 90, 90)
        cv2.line(overlay, (x, 0), (x, height), color, 2 if major else 1)
        cv2.line(overlay, (0, y), (width, y), color, 2 if major else 1)
        if major:
            put_text(overlay, f"{value:.2f}", (x + 4, 4), size=max(width // 90, 14), background=(0, 0, 0))
            put_text(overlay, f"{value:.2f}", (4, y + 4), size=max(width // 90, 14), background=(0, 0, 0))
    return cv2.addWeighted(overlay, 0.75, image, 0.25, 0)


def draw_ground_model(image, calib, ground, *, tick_mm: float = 500.0):
    """레일·턴테이블 원판·팔레트 사각형을 투영해 그린다 (캘리브레이션 확인용)."""
    circle = calib.world_to_image(ground.turntable_circle())
    polyline(image, circle, COLOR_CIRCLE, 2, closed=True)

    left, right = ground.rail_lines()
    polyline(image, calib.world_to_image(left), COLOR_RAIL, 3)
    polyline(image, calib.world_to_image(right), COLOR_RAIL, 3)

    polyline(image, calib.world_to_image(ground.pallet_rect()), COLOR_PALLET, 2, closed=True)

    half = ground.pallet_length_mm / 2.0
    x = -half
    while x <= half + 1e-6:
        a, b = calib.world_to_image([(x, -ground.rail_half), (x, ground.rail_half)])
        line(image, a, b, COLOR_PALLET, 1)
        x += tick_mm

    for label, wx, wy in ground.rail_circle_intersections():
        point = calib.world_to_image([(wx, wy)])[0]
        dot(image, point, (255, 255, 255), 5)
        put_text(image, label, (int(point[0] * image.shape[1]) + 8, int(point[1] * image.shape[0]) - 10), size=18)
    return image


def draw_box(image, calib, corners: Sequence[tuple[float, float]], height_mm: float, *, color=COLOR_BOX):
    """바닥 네 모서리 + 높이로 직육면체를 그린다. 윗면은 바닥면을 평행 이동해 근사한다.

    엄밀히는 높이 방향 소실점이 필요하지만, 여기서는 지면 호모그래피만 있으므로
    '차량 높이만큼 카메라 광축 반대쪽으로 민' 근사를 쓰고 그 사실을 이미지에 적는다.
    """
    base = calib.world_to_image(corners)
    polyline(image, base, color, 3, closed=True)
    return base


def draw_box_3d(image, calib, corners: Sequence[tuple[float, float]], top_shift: tuple[float, float], color=COLOR_BOX):
    """바닥 사각형과 '윗면 = 바닥면 + 화면상 평행이동' 근사로 직육면체를 그린다."""
    base = calib.world_to_image(corners)
    top = [(u + top_shift[0], v + top_shift[1]) for u, v in base]
    polyline(image, base, color, 3, closed=True)
    polyline(image, top, COLOR_BOX_TOP, 2, closed=True)
    for a, b in zip(base, top):
        line(image, a, b, COLOR_BOX_TOP, 2)
    return base, top


#: 글자 판의 불투명도. 완전히 가리지 않으면서 글자는 읽힌다 — 이미지 위의 모든 판에 같이 쓴다.
PANEL_ALPHA = 165
#: 한 줄 최대 글자 수. 오른쪽 아래에 붙이므로 너무 길면 왼쪽 아래의 카메라 표시와 겹친다.
PANEL_WRAP = 62


def label_at(image, text: str, point: tuple[float, float], *, size=20, color=(255, 255, 255), anchor="lt"):
    height, width = image.shape[:2]
    return put_text(
        image,
        text,
        (int(point[0] * width), int(point[1] * height)),
        size=size,
        color=color,
        anchor=anchor,
        alpha=PANEL_ALPHA,
    )


def _wrap(lines: Sequence[str], limit: int = PANEL_WRAP) -> list[str]:
    import textwrap

    wrapped: list[str] = []
    for line in lines:
        pieces = textwrap.wrap(line, width=limit, break_long_words=False) or [""]
        wrapped.append(pieces[0])
        # 이어지는 줄은 들여써서 한 항목임을 보이게 한다.
        wrapped += [f"   {piece}" for piece in pieces[1:]]
    return wrapped


def failure_note(image, reasons: Sequence[str], *, title: str = "판정 불가"):
    """실패 사유를 이미지 **오른쪽 아래**에 한국어로 새긴다 (반투명 판)."""
    height, width = image.shape[:2]
    body = _wrap([f"· {reason}" for reason in reasons] or ["· 사유 미기록"])
    text = "\n".join([f"[{title}]", *body])
    size = max(width // 95, 13)
    put_text(
        image,
        text,
        (int(width * 0.99), int(height * 0.985)),
        size=size,
        color=(150, 190, 255),
        anchor="rb",
        alpha=PANEL_ALPHA,
    )
    import cv2

    cv2.rectangle(image, (2, 2), (width - 3, height - 3), COLOR_FAIL, 4)
    return image


def side_panel(image, lines: Sequence[str], *, title: str = "측정값", size: int | None = None):
    """성공한 경우의 수치 요약을 **오른쪽 아래**에 새긴다 (반투명 판).

    예전에는 오른쪽 위에 불투명한 판으로 깔았는데, 직육면체 윗면이 대개 화면 위쪽으로
    뻗기 때문에 판정해야 할 선을 정확히 그 판이 가렸다.
    """
    height, width = image.shape[:2]
    text = "\n".join([f"[{title}]", *_wrap(list(lines))])
    size = size or max(width // 95, 14)
    return put_text(
        image,
        text,
        (int(width * 0.99), int(height * 0.985)),
        size=size,
        anchor="rb",
        alpha=PANEL_ALPHA,
    )


def arrow_with_length(image, a: tuple[float, float], b: tuple[float, float], text: str, color, *, size=20):
    import cv2

    (ax, ay), (bx, by) = denorm([a, b], image.shape)
    cv2.arrowedLine(image, (ax, ay), (bx, by), color, 2, cv2.LINE_AA, tipLength=0.03)
    cv2.arrowedLine(image, (bx, by), (ax, ay), color, 2, cv2.LINE_AA, tipLength=0.03)
    mid = ((ax + bx) // 2, (ay + by) // 2)
    put_text(image, text, mid, size=size, color=(255, 255, 255), anchor="ct", alpha=PANEL_ALPHA)
    return image


def distance_norm(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.dist(a, b)
