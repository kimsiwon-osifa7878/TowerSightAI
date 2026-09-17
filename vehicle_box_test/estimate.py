"""차량 실루엣 → 접지선 → 월드 좌표 → 직육면체/앞뒤·바퀴 위치 추정.

가설: **Hailo bbox 없이, 고정 카메라 + 지면 실치수만으로 차량의 바닥 사각형(앞·뒤 끝,
좌·우 바퀴선)과 직육면체를 뽑을 수 있는가?**

방법 (INTENT.md §4 확정안의 1단계 축소판)
1. 카메라별 중앙값 배경과의 차분으로 실루엣을 만든다 (Hailo 없음, CPU만).
2. 주차기 영역(팔레트 사각형을 투영한 다각형 + 차체 높이만큼 위로 확장) 밖은 버린다.
   문이 열리면 바깥 차량·행인이 보이기 때문이다.
3. 실루엣의 **아래쪽 경계 = 타이어 접지선**으로 보고 지면 호모그래피로 월드(mm)에 올린다.
   바닥에서 뜬 점(범퍼·지붕)은 지면 변환이 틀리므로 절대 쓰지 않는다.
4. 접지점들에서 팔레트 축에 정렬된 사각형을 강건하게(백분위수) 뽑는다.
5. 자세 복원이 믿을 만한 카메라에서는 실루엣 최상단으로 높이까지 추정해 직육면체를 만든다.

판정이 안 되면 **왜 안 되는지를 한국어 사유로 남긴다** — 이미지에 그대로 새겨서 사람이
이미지만 보고 판단할 수 있게 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from vehicle_box_test.geometry import CameraPose, GroundCalibration, GroundModel

#: 승인도 설계기준(L5205 × W2000 × H1550/1850)에 여유를 둔 타당성 범위.
PLAUSIBLE_LENGTH_MM = (2500.0, 6500.0)
PLAUSIBLE_WIDTH_MM = (1200.0, 2600.0)
PLAUSIBLE_HEIGHT_MM = (900.0, 2400.0)

MIN_SILHOUETTE_RATIO = 0.012  # ROI 대비 실루엣 최소 비율
#: 실루엣 면적 / 실루엣 외접 사각형 면적. 차량은 꽉 찬 덩어리다. 바닥 매트
#: 무늬나 조명 변화를 잡으면 가늘고 흩어진 윤곽이 되어 이 값이 크게 떨어진다 —
#: 차가 없는 프레임에서 3D 상자가 그려지던 오검출을 여기서 막는다.
MIN_SILHOUETTE_EXTENT = 0.25
MIN_CONTACT_SAMPLES = 25
#: 접지선을 월드로 올릴 때 팔레트 밖으로 크게 벗어난 점은 버린다(문 밖 바닥은 평면이 다르다).
FOOTPRINT_MARGIN_MM = 900.0
#: 자세의 지면 잔차가 이보다 크면 3D 상자를 그리지 않는다 (mm).
#: 체커보드 측정 전에는 자세와 호모그래피가 서로 어긋나 상자를 아예 못 그렸다. 측정 후에는
#: 호모그래피를 자세에서 만들기 때문에 둘이 정의상 일치하고, 남는 문제는 지면 기준점의
#: 정확도뿐이라 mm 단위로 판단한다.
POSE_RESIDUAL_LIMIT_MM = 600.0


@dataclass
class VehicleEstimate:
    camera_id: str
    source: str
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # 월드 측정값 (mm)
    x_front: float | None = None
    x_rear: float | None = None
    y_left: float | None = None
    y_right: float | None = None
    height_mm: float | None = None

    # 영상 산출물
    silhouette_ratio: float = 0.0
    silhouette_extent: float = 0.0
    contact_samples: int = 0
    truncated_front: bool = False
    truncated_rear: bool = False
    pose_used: bool = False
    pose_error: float = 0.0
    annotated_path: str = ""

    @property
    def length_mm(self) -> float | None:
        if self.x_front is None or self.x_rear is None:
            return None
        return self.x_front - self.x_rear

    @property
    def width_mm(self) -> float | None:
        if self.y_left is None or self.y_right is None:
            return None
        return self.y_left - self.y_right

    @property
    def center(self) -> tuple[float, float] | None:
        if None in (self.x_front, self.x_rear, self.y_left, self.y_right):
            return None
        return ((self.x_front + self.x_rear) / 2.0, (self.y_left + self.y_right) / 2.0)

    def footprint(self) -> list[tuple[float, float]] | None:
        if None in (self.x_front, self.x_rear, self.y_left, self.y_right):
            return None
        return [
            (self.x_rear, self.y_right),
            (self.x_front, self.y_right),
            (self.x_front, self.y_left),
            (self.x_rear, self.y_left),
        ]

    def to_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        data["length_mm"] = self.length_mm
        data["width_mm"] = self.width_mm
        return data


# ------------------------------------------------------------------ 관심 영역


def machine_roi(calib: GroundCalibration, ground: GroundModel, shape, pose: CameraPose | None):
    """주차기 안쪽만 남기는 다각형(정규화 좌표).

    차체는 바닥보다 위로 솟으므로 팔레트 사각형만 쓰면 지붕이 잘린다. 자세가 있으면
    실제 구획 높이만큼의 입체를 투영하고, 없으면 영상에서 위쪽으로 넉넉히 늘린다.
    """
    import numpy as np

    margin = 150.0
    hx = ground.pallet_length_mm / 2.0 + margin
    hy = ground.pallet_width_mm / 2.0 + margin
    base = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]

    points = list(calib.world_to_image(base))
    if pose is not None:
        top = ground.turntable_radius * 0  # 자리표시 (아래에서 높이 사용)
        _ = top
        points += pose.project([(x, y, 2100.0) for x, y in base])
    else:
        ys = [p[1] for p in points]
        lift = (max(ys) - min(ys)) * 0.9
        points += [(u, v - lift) for u, v in points]

    hull = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
    import cv2

    hull = cv2.convexHull(hull).reshape(-1, 2)
    return [(float(u), float(v)) for u, v in hull]


def roi_mask(shape, polygon: Sequence[tuple[float, float]]):
    import cv2
    import numpy as np

    height, width = shape[:2]
    mask = np.zeros((height, width), np.uint8)
    pts = np.array([[int(u * width), int(v * height)] for u, v in polygon], np.int32)
    cv2.fillPoly(mask, [pts], 255)
    return mask


# --------------------------------------------------------------------- 실루엣


def silhouette(image, background, roi):
    """배경차분 실루엣. 밝기 차이를 보정해 조명이 다른 날도 비교 가능하게 한다."""
    import cv2
    import numpy as np

    if image.shape != background.shape:
        background = cv2.resize(background, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_AREA)

    # 흰 차 + 회색 바닥은 명도만 보면 거의 같다. Lab 색공간에서 채널별 조명 보정 후
    # 색 거리를 쓰면 흰 차체도 바닥과 구분된다.
    lab_image = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab_base = cv2.cvtColor(background, cv2.COLOR_BGR2LAB).astype(np.float32)
    for channel in range(3):
        shift = float(
            np.median(lab_image[:, :, channel][roi > 0]) - np.median(lab_base[:, :, channel][roi > 0])
        )
        lab_base[:, :, channel] += shift
    diff = np.linalg.norm(lab_image - lab_base, axis=2)
    diff = cv2.GaussianBlur(diff.astype(np.float32), (7, 7), 0)
    diff[roi == 0] = 0

    values = diff[roi > 0]
    # Otsu로 자동 분할하되, 바닥 잡음 수준(중앙값의 2배) 아래로는 내려가지 않게 한다.
    scaled = np.clip(diff / max(float(values.max()), 1.0) * 255.0, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(scaled[roi > 0], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = max(otsu / 255.0 * float(values.max()), 2.0 * float(np.median(values)), 9.0)
    mask = (diff >= threshold).astype(np.uint8) * 255

    kernel = np.ones((7, 7), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return mask * 0, 0.0
    largest = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    blob = ((labels == largest).astype(np.uint8)) * 255
    ratio = float(stats[largest, cv2.CC_STAT_AREA]) / max(float(np.count_nonzero(roi)), 1.0)
    box = float(stats[largest, cv2.CC_STAT_WIDTH] * stats[largest, cv2.CC_STAT_HEIGHT])
    extent = float(stats[largest, cv2.CC_STAT_AREA]) / max(box, 1.0)
    return blob, ratio, extent


def contact_points(blob, roi) -> list[tuple[float, float]]:
    """실루엣의 아래쪽 경계 = 접지선 후보 (정규화 좌표).

    ROI 아래 경계에 붙은 열은 버린다 — 바닥이 아니라 화면이 잘린 곳이다.
    """
    import numpy as np

    height, width = blob.shape[:2]
    points: list[tuple[float, float]] = []
    for x in range(0, width, max(width // 240, 1)):
        column = np.nonzero(blob[:, x])[0]
        if column.size == 0:
            continue
        bottom = int(column.max())
        roi_column = np.nonzero(roi[:, x])[0]
        if roi_column.size and bottom >= int(roi_column.max()) - 2:
            continue  # ROI 하단에 붙음 = 잘린 실루엣
        if bottom >= height - 2:
            continue
        points.append((x / width, bottom / height))
    return points


# --------------------------------------------------------------------- 추정


def estimate_vehicle(
    image,
    background,
    calib: GroundCalibration,
    ground: GroundModel,
    pose: CameraPose | None,
    *,
    camera_id: str,
    source: str,
) -> tuple[VehicleEstimate, object, list[tuple[float, float]]]:
    import numpy as np

    result = VehicleEstimate(camera_id=camera_id, source=source)
    polygon = machine_roi(calib, ground, image.shape, pose)
    roi = roi_mask(image.shape, polygon)
    blob, ratio, extent = silhouette(image, background, roi)
    result.silhouette_ratio = round(ratio, 4)
    result.silhouette_extent = round(extent, 3)

    if ratio < MIN_SILHOUETTE_RATIO:
        result.reasons.append(
            f"주차기 영역에서 배경과 다른 덩어리를 찾지 못했습니다 (면적 {ratio*100:.1f}%)."
            " 차량이 없거나, 배경과 색이 비슷하거나, 조명이 배경 이미지와 크게 다릅니다."
        )
        return result, blob, polygon

    if extent < MIN_SILHOUETTE_EXTENT:
        result.reasons.append(
            f"찾은 덩어리가 가늘고 흩어져 있습니다 "
            f"(채움 {extent * 100:.0f}%, 차량이면 {MIN_SILHOUETTE_EXTENT * 100:.0f}% 이상)."
            " 차량이 아니라 바닥 매트 무늬·조명 변화·그림자를 잡았을 가능성이 큽니다."
        )
        return result, blob, polygon

    raw = contact_points(blob, roi)
    if len(raw) < MIN_CONTACT_SAMPLES:
        result.reasons.append(
            f"접지선 표본이 {len(raw)}개뿐입니다(최소 {MIN_CONTACT_SAMPLES}개)."
            " 차량 아래쪽이 화면 밖이거나 다른 물체에 가려 바닥과 닿는 선이 보이지 않습니다."
        )
        return result, blob, polygon

    world = calib.image_to_world(raw)
    hx = ground.pallet_length_mm / 2.0 + FOOTPRINT_MARGIN_MM
    hy = ground.pallet_width_mm / 2.0 + FOOTPRINT_MARGIN_MM
    kept = [(x, y) for x, y in world if -hx <= x <= hx and -hy <= y <= hy]
    result.contact_samples = len(kept)
    if len(kept) < MIN_CONTACT_SAMPLES:
        result.reasons.append(
            f"접지선을 지면으로 변환했더니 팔레트 밖으로 벗어난 점이 대부분입니다"
            f" (유효 {len(kept)}/{len(raw)}개). 문 밖 차량·행인을 잡았거나 지면 교정이 어긋났습니다."
        )
        return result, blob, polygon

    xs = np.array([p[0] for p in kept])
    ys = np.array([p[1] for p in kept])
    centre_x, centre_y = float(np.median(xs)), float(np.median(ys))
    if abs(centre_x) > ground.pallet_length_mm / 2.0 or abs(centre_y) > ground.pallet_width_mm / 2.0:
        result.reasons.append(
            f"실루엣의 접지 중심이 팔레트 밖입니다 (x {centre_x:+.0f}, y {centre_y:+.0f} mm)."
            " 주차기 안의 차량이 아니라 문 밖 차량·행인을 잡았을 가능성이 큽니다."
        )
        return result, blob, polygon
    result.x_front = float(np.percentile(xs, 97))
    result.x_rear = float(np.percentile(xs, 3))
    result.y_left = float(np.percentile(ys, 97))
    result.y_right = float(np.percentile(ys, 3))

    # 화면 좌우 끝에 실루엣이 닿으면 그 방향 끝은 신뢰할 수 없다.
    columns = np.nonzero(blob.any(axis=0))[0]
    width = blob.shape[1]
    if columns.size:
        if columns.min() <= 2 or columns.max() >= width - 3:
            near_front = abs(result.x_front) > abs(result.x_rear)
            result.truncated_front = near_front
            result.truncated_rear = not near_front
            result.notes.append("차량 실루엣이 화면 가장자리에 닿아 한쪽 끝은 잘렸을 수 있습니다.")

    length = result.length_mm or 0.0
    width_mm = result.width_mm or 0.0
    if not PLAUSIBLE_LENGTH_MM[0] <= length <= PLAUSIBLE_LENGTH_MM[1]:
        result.reasons.append(
            f"길이 추정값 {length:.0f} mm이 타당 범위"
            f"({PLAUSIBLE_LENGTH_MM[0]:.0f}~{PLAUSIBLE_LENGTH_MM[1]:.0f} mm)를 벗어났습니다."
            " 차량 일부만 보이거나 접지선에 그림자·다른 물체가 섞였습니다."
        )
    if not PLAUSIBLE_WIDTH_MM[0] <= width_mm <= PLAUSIBLE_WIDTH_MM[1]:
        result.reasons.append(
            f"폭 추정값 {width_mm:.0f} mm이 타당 범위"
            f"({PLAUSIBLE_WIDTH_MM[0]:.0f}~{PLAUSIBLE_WIDTH_MM[1]:.0f} mm)를 벗어났습니다."
        )

    # 높이 — 자세 복원이 호모그래피와 잘 맞을 때만
    if pose is not None and pose.reprojection_error <= POSE_RESIDUAL_LIMIT_MM:
        centre = result.center
        if centre is not None:
            # 위에서 내려다보는 카메라에서 실루엣의 최상단(지붕 윤곽)은 **먼 쪽 지붕 모서리**다.
            # 중앙선이나 가까운 쪽으로 잡으면 높이가 과대평가된다.
            camera_y = float(pose.center[1])
            far_y = max((result.y_left, result.y_right), key=lambda y: abs(y - camera_y))
            centre = (centre[0], far_y)
            column_x = int(np.clip(_image_column_for(calib, centre, blob.shape), 0, blob.shape[1] - 1))
            column = np.nonzero(blob[:, column_x])[0]
            if column.size:
                top = (column_x / blob.shape[1], float(column.min()) / blob.shape[0])
                height = pose.height_of(centre, top)
                if height is not None and PLAUSIBLE_HEIGHT_MM[0] <= height <= PLAUSIBLE_HEIGHT_MM[1]:
                    result.height_mm = float(height)
                    result.pose_used = True
                elif height is not None:
                    result.notes.append(f"높이 추정값 {height:.0f} mm이 타당 범위를 벗어나 표시하지 않았습니다.")

    if not result.reasons:
        result.ok = True
    return result, blob, polygon


def _image_column_for(calib: GroundCalibration, centre: tuple[float, float], shape) -> int:
    point = calib.world_to_image([centre])[0]
    return int(round(point[0] * shape[1]))


def pose_agreement(calib: GroundCalibration, pose: CameraPose | None, ground: GroundModel) -> float:
    """자세 투영과 호모그래피가 지면에서 얼마나 어긋나는지 (화면 비율 최대값)."""
    if pose is None:
        return math.inf
    hx = ground.pallet_length_mm / 2.0
    hy = ground.pallet_width_mm / 2.0
    samples = [(x, y) for x in (-hx, 0.0, hx) for y in (-hy, 0.0, hy)]
    worst = 0.0
    for point in samples:
        a = calib.world_to_image([point])[0]
        b = pose.project([(point[0], point[1], 0.0)])[0]
        if max(abs(a[0]), abs(a[1])) > 3:
            continue
        worst = max(worst, math.dist(a, b))
    return worst
