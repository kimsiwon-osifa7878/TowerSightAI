"""노란 레일 가장자리 자동 검출.

주차기 팔레트의 두 타이어 트랙은 노란 안전 테두리로 감싸여 있어 HSV에서 또렷하게 분리된다.
승인도 J001 기준 팔레트 폭 2,200 / 레일 내폭 2,106 이므로, **가장 바깥 노란 띠의 바깥쪽
가장자리**를 팔레트 양 끝(y = ±1,100)으로 본다(내폭과의 차이는 47 mm로 무시 가능).

검출 결과는 '띠마다 중심선을 따라 찍은 표본점'이다. 어안 왜곡이 있어 한 직선으로 맞추면
가장자리에서 어긋나므로, 캘리브레이션 최적화에는 점 자체를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

YELLOW_LOWER = (18, 90, 110)
YELLOW_UPPER = (38, 255, 255)
MIN_STRIP_AREA = 3000


@dataclass
class YellowStrip:
    """노란 띠 하나. 점들은 정규화 좌표."""

    area: int
    centerline: list[tuple[float, float]] = field(default_factory=list)
    outer_edge: list[tuple[float, float]] = field(default_factory=list)
    inner_edge: list[tuple[float, float]] = field(default_factory=list)

    @property
    def centroid(self) -> tuple[float, float]:
        xs = [p[0] for p in self.centerline]
        ys = [p[1] for p in self.centerline]
        return (sum(xs) / len(xs), sum(ys) / len(ys)) if xs else (0.0, 0.0)


def yellow_mask(image):
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, YELLOW_LOWER, YELLOW_UPPER)
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return mask


def detect_strips(image, *, max_strips: int = 4, samples: int = 40) -> list[YellowStrip]:
    """큰 노란 띠들을 찾아 띠마다 중심선/양쪽 가장자리 표본점을 돌려준다.

    띠가 가로로 누웠는지 세로로 섰는지에 따라 표본 방향을 바꾼다(전면 카메라는 세로,
    측면 카메라는 가로에 가깝다).
    """
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    mask = yellow_mask(image)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    order = sorted(range(1, count), key=lambda i: -stats[i, cv2.CC_STAT_AREA])

    strips: list[YellowStrip] = []
    for index in order[: max_strips * 2]:
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < MIN_STRIP_AREA or len(strips) >= max_strips:
            continue
        component = labels == index
        ys, xs = np.nonzero(component)
        horizontal = (xs.max() - xs.min()) >= (ys.max() - ys.min())

        strip = YellowStrip(area=area)
        if horizontal:
            positions = np.linspace(xs.min(), xs.max(), samples).astype(int)
            for x in positions:
                column = np.nonzero(component[:, x])[0]
                if column.size == 0:
                    continue
                strip.centerline.append((x / width, float(column.mean()) / height))
                strip.inner_edge.append((x / width, float(column.min()) / height))
                strip.outer_edge.append((x / width, float(column.max()) / height))
        else:
            positions = np.linspace(ys.min(), ys.max(), samples).astype(int)
            for y in positions:
                row = np.nonzero(component[y, :])[0]
                if row.size == 0:
                    continue
                strip.centerline.append((float(row.mean()) / width, y / height))
                strip.inner_edge.append((float(row.min()) / width, y / height))
                strip.outer_edge.append((float(row.max()) / width, y / height))
        if len(strip.centerline) >= 5:
            strips.append(strip)
    return strips
