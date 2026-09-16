"""주차기 지면 기하: 월드 모델 + 지면 호모그래피 + 그리기 도우미.

좌표계 (INTENT.md §4 확정안과 동일)
    원점 = 팔레트(주차구획) 사각형 중심, x = 팔레트 길이 방향(차량 진입 방향),
    y = 폭 방향, z = 위. 단위 mm.

체커보드 내부 파라미터가 아직 없으므로 렌즈 왜곡은 보정하지 않는다. 대신 **바닥면 위의
실치수를 아는 물체**로 지면↔영상 호모그래피를 직접 맞춘다. 현장 영상에 항상 보이는 것:

* 노란 레일 두 줄 — 승인도 J001의 레일 내폭 2,106 mm
* 턴테이블 원판 — 승인도 J002의 Ø6,100 mm

호모그래피는 **정규화 영상 좌표(0~1)** 로 정의한다. 스냅샷(1920×1080, stream1)과 클립
프레임(640×360, stream2)의 화각이 같고 해상도만 다르기 때문에, 정규화해 두면 두 해상도에
그대로 쓸 수 있다.

주의: 호모그래피는 **z=0 평면(바닥)** 만 정확하다. 바닥에서 뜬 점(범퍼·지붕)은 바닥 좌표로
변환하면 틀린다 — 그래서 차량 위치는 **타이어 접지선**에서만 읽는다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from towersightai.config.settings import VehicleEnvelopeConfig


LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_CALIB_PATH = LAB_ROOT / "site_calibration.json"

#: 승인도 J002의 턴테이블 지름. VehicleEnvelopeConfig에는 없는 값이라 여기 둔다.
TURNTABLE_DIAMETER_MM = 6100.0


# --------------------------------------------------------------------- 월드 모델


@dataclass(frozen=True)
class GroundModel:
    """바닥면에 실제로 그려져 있는 것들 (mm). 기본값은 승인도 J001/J002."""

    pallet_length_mm: float = 5350.0
    pallet_width_mm: float = 2200.0
    rail_inner_width_mm: float = 2106.0
    turntable_diameter_mm: float = TURNTABLE_DIAMETER_MM
    #: 턴테이블 중심이 팔레트 중심에서 x로 얼마나 떨어져 있는가 (현장 실측으로 보정).
    turntable_offset_x_mm: float = 0.0
    turntable_offset_y_mm: float = 0.0

    @classmethod
    def from_envelope(cls, envelope: VehicleEnvelopeConfig | None = None, **overrides) -> "GroundModel":
        envelope = envelope or VehicleEnvelopeConfig()
        return cls(
            pallet_length_mm=envelope.pallet_length_mm,
            pallet_width_mm=envelope.pallet_width_mm,
            rail_inner_width_mm=envelope.rail_inner_width_mm,
            **overrides,
        )

    @property
    def rail_half(self) -> float:
        return self.rail_inner_width_mm / 2.0

    @property
    def turntable_radius(self) -> float:
        return self.turntable_diameter_mm / 2.0

    def rail_lines(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """레일 안쪽 모서리 두 줄 (팔레트 길이 전체)."""
        half = self.pallet_length_mm / 2.0
        return (
            [(-half, -self.rail_half), (half, -self.rail_half)],
            [(-half, self.rail_half), (half, self.rail_half)],
        )

    def pallet_rect(self) -> list[tuple[float, float]]:
        hx = self.pallet_length_mm / 2.0
        hy = self.pallet_width_mm / 2.0
        return [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]

    def turntable_circle(self, steps: int = 180) -> list[tuple[float, float]]:
        cx, cy, r = self.turntable_offset_x_mm, self.turntable_offset_y_mm, self.turntable_radius
        return [
            (cx + r * math.cos(2 * math.pi * i / steps), cy + r * math.sin(2 * math.pi * i / steps))
            for i in range(steps)
        ]

    def rail_circle_intersections(self) -> list[tuple[str, float, float]]:
        """레일 안쪽 모서리와 턴테이블 원이 만나는 네 점 — 영상에서 눈으로 찍기 좋은 기준점."""
        points: list[tuple[str, float, float]] = []
        cx, cy, r = self.turntable_offset_x_mm, self.turntable_offset_y_mm, self.turntable_radius
        for side, y in (("R-", -self.rail_half), ("R+", self.rail_half)):
            dy = y - cy
            if abs(dy) >= r:
                continue
            dx = math.sqrt(r * r - dy * dy)
            points.append((f"{side}x-", cx - dx, y))
            points.append((f"{side}x+", cx + dx, y))
        return points


# --------------------------------------------------------------- 지면 호모그래피


@dataclass
class GroundCalibration:
    """한 카메라의 지면 호모그래피. 대응점은 손으로 맞춘 뒤 JSON에 저장한다."""

    camera_id: str
    #: (world_x_mm, world_y_mm, u_norm, v_norm) 네 쌍 이상
    correspondences: list[tuple[float, float, float, float]] = field(default_factory=list)
    note: str = ""
    _matrix: object | None = field(default=None, repr=False, compare=False)

    def matrix(self):
        """world(mm) → 정규화 영상좌표 호모그래피."""
        import numpy as np
        import cv2

        if self._matrix is None:
            if len(self.correspondences) < 4:
                raise ValueError(f"{self.camera_id}: 대응점이 4개 미만입니다.")
            world = np.array([[c[0], c[1]] for c in self.correspondences], dtype=np.float64)
            image = np.array([[c[2], c[3]] for c in self.correspondences], dtype=np.float64)
            matrix, _ = cv2.findHomography(world, image, method=0)
            if matrix is None:
                raise ValueError(f"{self.camera_id}: 호모그래피를 구할 수 없습니다.")
            self._matrix = matrix
        return self._matrix

    def inverse(self):
        import numpy as np

        return np.linalg.inv(self.matrix())

    def world_to_image(self, points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
        """월드(mm) → 정규화 영상좌표."""
        import numpy as np

        pts = np.array([[p[0], p[1], 1.0] for p in points], dtype=np.float64).T
        projected = self.matrix() @ pts
        with np.errstate(divide="ignore", invalid="ignore"):
            projected = projected[:2] / projected[2]
        return [(float(u), float(v)) for u, v in projected.T]

    def image_to_world(self, points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
        """정규화 영상좌표 → 월드(mm). **바닥면 위의 점에만 유효**."""
        import numpy as np

        pts = np.array([[p[0], p[1], 1.0] for p in points], dtype=np.float64).T
        projected = self.inverse() @ pts
        with np.errstate(divide="ignore", invalid="ignore"):
            projected = projected[:2] / projected[2]
        return [(float(x), float(y)) for x, y in projected.T]

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "note": self.note,
            "correspondences": [list(c) for c in self.correspondences],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GroundCalibration":
        return cls(
            camera_id=str(data["camera_id"]),
            correspondences=[tuple(float(v) for v in row) for row in data.get("correspondences", [])],
            note=str(data.get("note", "")),
        )

    def reprojection_error_mm(self) -> float:
        """대응점을 다시 투영했을 때의 평균 오차를 mm로 환산(대략치)."""
        import numpy as np

        world = [(c[0], c[1]) for c in self.correspondences]
        measured = [(c[2], c[3]) for c in self.correspondences]
        projected = self.world_to_image(world)
        back = self.image_to_world(measured)
        errors = [math.dist(w, b) for w, b in zip(world, back)]
        _ = projected
        return float(np.mean(errors)) if errors else float("nan")


@dataclass
class SiteCalibration:
    ground: GroundModel = field(default_factory=GroundModel.from_envelope)
    cameras: dict[str, GroundCalibration] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_CALIB_PATH) -> "SiteCalibration":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        ground = GroundModel(**data.get("ground", {}))
        cameras = {
            key: GroundCalibration.from_dict(value) for key, value in (data.get("cameras") or {}).items()
        }
        return cls(ground=ground, cameras=cameras)

    def save(self, path: Path = DEFAULT_CALIB_PATH) -> None:
        payload = {
            "ground": {
                "pallet_length_mm": self.ground.pallet_length_mm,
                "pallet_width_mm": self.ground.pallet_width_mm,
                "rail_inner_width_mm": self.ground.rail_inner_width_mm,
                "turntable_diameter_mm": self.ground.turntable_diameter_mm,
                "turntable_offset_x_mm": self.ground.turntable_offset_x_mm,
                "turntable_offset_y_mm": self.ground.turntable_offset_y_mm,
            },
            "cameras": {key: value.to_dict() for key, value in self.cameras.items()},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------- 자세 복원 (3D 투영)


@dataclass
class CameraPose:
    """지면 호모그래피에서 복원한 핀홀 카메라. 3D 점을 직접 투영할 수 있다.

    체커보드 측정이 없으므로 주점은 화면 중심, 화소는 정사각형으로 가정하고 초점거리만
    호모그래피의 회전 직교 조건(r1⊥r2, |r1|=|r2|)에서 푼다. 어안 왜곡은 모델에 없다 —
    화면 가장자리일수록 오차가 커진다는 뜻이며, 결과 이미지에 그 사실을 적는다.
    """

    focal: float  # 정규화 단위(가로 폭 = 1)
    aspect: float  # height / width
    rotation: object  # 3x3
    translation: object  # 3

    def project(self, points_3d):
        """월드 3D(mm) → 정규화 영상좌표."""
        import numpy as np

        pts = np.asarray(points_3d, dtype=np.float64).reshape(-1, 3)
        cam = (self.rotation @ pts.T).T + self.translation
        with np.errstate(divide="ignore", invalid="ignore"):
            u = self.focal * cam[:, 0] / cam[:, 2] + 0.5
            v = self.focal * cam[:, 1] / cam[:, 2] + 0.5 * self.aspect
        return [(float(a), float(b / self.aspect)) for a, b in zip(u, v)]

    def height_of(self, ground_xy: tuple[float, float], image_point: tuple[float, float]) -> float | None:
        """바닥 위치 (x, y)가 알려진 연직선에서, 영상의 한 점이 갖는 높이 z(mm).

        연직선 위의 점은 z에 대해 단조로우므로 이분 탐색으로 푼다.
        """
        target_v = image_point[1]
        low, high = 0.0, 4000.0
        base = self.project([(ground_xy[0], ground_xy[1], 0.0)])[0]
        top = self.project([(ground_xy[0], ground_xy[1], high)])[0]
        if not (min(base[1], top[1]) - 0.02 <= target_v <= max(base[1], top[1]) + 0.02):
            return None
        rising = top[1] < base[1]
        for _ in range(60):
            mid = (low + high) / 2.0
            v = self.project([(ground_xy[0], ground_xy[1], mid)])[0][1]
            if (v > target_v) == rising:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0


def pose_from_homography(
    calib: GroundCalibration, aspect: float, *, fallback_focal: float | None = 0.55
) -> CameraPose | None:
    """world(mm, z=0) → 정규화 영상 호모그래피에서 K와 [R|t]를 복원한다."""
    import numpy as np

    matrix = np.asarray(calib.matrix(), dtype=np.float64)
    # 정규화 좌표를 등방(가로 폭 = 1)으로 바꾸고 주점을 원점으로 옮긴다.
    to_iso = np.array([[1.0, 0.0, -0.5], [0.0, aspect, -0.5 * aspect], [0.0, 0.0, 1.0]])
    matrix = to_iso @ matrix
    h1, h2 = matrix[:, 0], matrix[:, 1]

    # ω = diag(w, w, 1), w = 1/f².  h1ᵀωh2 = 0,  h1ᵀωh1 = h2ᵀωh2
    a = np.array([h1[0] * h2[0] + h1[1] * h2[1], h1[0] ** 2 + h1[1] ** 2 - h2[0] ** 2 - h2[1] ** 2])
    b = np.array([-h1[2] * h2[2], -(h1[2] ** 2 - h2[2] ** 2)])
    denominator = float(a @ a)
    w = float(a @ b) / denominator if denominator > 0 else 0.0
    if w > 1e-12:
        focal = math.sqrt(1.0 / w)
    elif fallback_focal:
        # 직교 조건이 풀리지 않는다 = 대응점/어안 왜곡 때문에 호모그래피가 정사각 화소
        # 핀홀과 맞지 않는다는 뜻. 초점거리를 가정하고 근사 자세를 쓴다(결과에 표기).
        focal = float(fallback_focal)
    else:
        return None

    k_inv = np.array([[1.0 / focal, 0.0, 0.0], [0.0, 1.0 / focal, 0.0], [0.0, 0.0, 1.0]])
    r1 = k_inv @ h1
    r2 = k_inv @ h2
    scale = 2.0 / (np.linalg.norm(r1) + np.linalg.norm(r2))
    r1, r2 = r1 * scale, r2 * scale
    translation = (k_inv @ matrix[:, 2]) * scale
    if translation[2] < 0:  # 카메라 앞쪽이 되도록 부호 정리
        r1, r2, translation = -r1, -r2, -translation
    r3 = np.cross(r1, r2)
    rotation = np.column_stack([r1, r2, r3])
    # 가장 가까운 정규직교 행렬로 보정
    u, _, vt = np.linalg.svd(rotation)
    rotation = u @ vt
    return CameraPose(focal=focal, aspect=aspect, rotation=rotation, translation=translation)


# ------------------------------------------------------------------------ 3D 상자


def box_corners(
    center_x: float,
    center_y: float,
    length: float,
    width: float,
    height: float,
    yaw_deg: float = 0.0,
) -> tuple[list[tuple[float, float]], float]:
    """차량 직육면체의 바닥 네 모서리(월드 mm)와 높이를 돌려준다."""
    yaw = math.radians(yaw_deg)
    hl, hw = length / 2.0, width / 2.0
    corners = []
    for dx, dy in ((-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)):
        corners.append(
            (
                center_x + dx * math.cos(yaw) - dy * math.sin(yaw),
                center_y + dx * math.sin(yaw) + dy * math.cos(yaw),
            )
        )
    return corners, height
