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
    #: 측정된 내부 파라미터로 푼 카메라 자세. 있으면 호모그래피를 여기서 만든다 —
    #: 자세와 지면 투영이 정의상 일치하므로 3D 상자를 그릴 수 있다.
    pose: "CameraPose | None" = None
    _matrix: object | None = field(default=None, repr=False, compare=False)

    def matrix(self):
        """world(mm) → 정규화 영상좌표 호모그래피."""
        import numpy as np
        import cv2

        if self.pose is not None:
            return self.pose.ground_homography()
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
        payload = {
            "camera_id": self.camera_id,
            "note": self.note,
            "correspondences": [list(c) for c in self.correspondences],
        }
        if self.pose is not None:
            payload["pose"] = self.pose.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "GroundCalibration":
        return cls(
            camera_id=str(data["camera_id"]),
            correspondences=[tuple(float(v) for v in row) for row in data.get("correspondences", [])],
            note=str(data.get("note", "")),
            pose=CameraPose.from_dict(data["pose"]) if data.get("pose") else None,
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
    #: "distorted" = 원본 영상 좌표, "undistorted" = 렌즈 왜곡 보정 후 좌표.
    #: 보정을 도입한 뒤로는 undistorted가 기본이며, 랩의 모든 이미지도 같은 공간에서 다룬다.
    space: str = "distorted"

    @classmethod
    def load(cls, path: Path = DEFAULT_CALIB_PATH) -> "SiteCalibration":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        ground = GroundModel(**data.get("ground", {}))
        cameras = {
            key: GroundCalibration.from_dict(value) for key, value in (data.get("cameras") or {}).items()
        }
        return cls(ground=ground, cameras=cameras, space=str(data.get("space", "distorted")))

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
            "space": self.space,
            "cameras": {key: value.to_dict() for key, value in self.cameras.items()},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------- 자세 복원 (3D 투영)


@dataclass
class CameraPose:
    """핀홀 카메라. 3D 점을 정규화 영상좌표로 직접 투영한다.

    내부 파라미터는 **운영자 콘솔에서 체커보드로 측정한 값**을 그대로 쓴다(추정하지 않는다).
    영상은 미리 왜곡 보정된 상태여야 한다 — 그래야 이 핀홀 모델이 화면 전체에서 성립한다.
    길이는 전부 '가로 폭 = 1'로 정규화한다.
    """

    fx: float
    fy: float
    cx: float
    cy: float
    aspect: float  # height / width
    rotation: object  # 3x3
    translation: object  # 3
    #: 대응점을 이 자세로 되투영했을 때의 평균 오차 (정규화 가로 폭 기준)
    reprojection_error: float = 0.0
    borrowed_intrinsics: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        import cv2
        import numpy as np

        rvec, _ = cv2.Rodrigues(np.asarray(self.rotation, dtype=np.float64))
        return {
            "fx": self.fx,
            "fy": self.fy,
            "cx": self.cx,
            "cy": self.cy,
            "aspect": self.aspect,
            "rvec": [float(v) for v in np.asarray(rvec).reshape(3)],
            "tvec": [float(v) for v in np.asarray(self.translation).reshape(3)],
            "residual_mm": round(float(self.reprojection_error), 2),
            "borrowed_intrinsics": self.borrowed_intrinsics,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CameraPose":
        import cv2
        import numpy as np

        rotation, _ = cv2.Rodrigues(np.array(data["rvec"], dtype=np.float64))
        return cls(
            fx=float(data["fx"]),
            fy=float(data["fy"]),
            cx=float(data["cx"]),
            cy=float(data["cy"]),
            aspect=float(data["aspect"]),
            rotation=rotation,
            translation=np.array(data["tvec"], dtype=np.float64),
            reprojection_error=float(data.get("residual_mm", 0.0)),
            borrowed_intrinsics=str(data.get("borrowed_intrinsics", "")),
            note=str(data.get("note", "")),
        )

    @property
    def focal(self) -> float:
        """이전 코드가 쓰던 단일 초점거리 (fx, fy 평균)."""
        return (self.fx + self.fy) / 2.0

    @property
    def center(self):
        """월드 좌표계에서의 카메라 위치 (mm)."""
        import numpy as np

        return -np.asarray(self.rotation).T @ np.asarray(self.translation)

    def project(self, points_3d):
        """월드 3D(mm) → 정규화 영상좌표."""
        import numpy as np

        pts = np.asarray(points_3d, dtype=np.float64).reshape(-1, 3)
        cam = (self.rotation @ pts.T).T + self.translation
        with np.errstate(divide="ignore", invalid="ignore"):
            u = self.fx * cam[:, 0] / cam[:, 2] + self.cx
            v = self.fy * cam[:, 1] / cam[:, 2] + self.cy
        return [(float(a), float(b / self.aspect)) for a, b in zip(u, v)]

    def ground_homography(self):
        """월드 지면(z=0, mm) → 정규화 영상좌표 호모그래피.

        자세에서 직접 만들기 때문에 투영과 **정확히 일치**한다. 손으로 찍은 대응점으로
        따로 맞춘 호모그래피와 자세가 어긋나던 1차 검증의 문제가 여기서 사라진다.
        """
        import numpy as np

        rotation = np.asarray(self.rotation, dtype=np.float64)
        intrinsics = np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])
        matrix = intrinsics @ np.column_stack([rotation[:, 0], rotation[:, 1], np.asarray(self.translation)])
        # v를 aspect로 나눠 정규화 영상좌표(0..1)로 맞춘다.
        matrix = np.diag([1.0, 1.0 / self.aspect, 1.0]) @ matrix
        return matrix / matrix[2, 2]

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


def pose_from_ground_file(camera_id: str, *, root: Path | None = None, width: int = 1920, height: int = 1080):
    """운영자가 `지면 기준점` 페이지에서 찍어 저장한 자세를 랩으로 들여온다.

    이 파일이 있으면 랩이 추정한 어떤 값보다 **우선**한다 — 사람이 팔레트 모서리를 직접
    지정한 것이라 x 기준이 추측이 아니기 때문이다.
    """
    import cv2
    import numpy as np

    from towersightai.calibration.ground import GroundCalibrationStore, scale_camera_matrix

    store = GroundCalibrationStore(root=Path(root) if root else Path("data/calibration/ground"))
    result = store.load(camera_id)
    if result is None:
        return None
    rotation, _ = cv2.Rodrigues(np.array(result.rvec, dtype=np.float64))

    from towersightai.calibration.intrinsics import load_intrinsics, result_from_dict

    # 자세는 **그때 쓴 K로** 풀린 값이다. 같은 렌즈 파일을 찾아 써야 투영이 맞는다.
    # 후보 순서: 기록된 렌즈 카메라 → 이 카메라 자신 → 폴더에 있는 유일한 측정값.
    lens_root = Path("data/calibration/intrinsics")
    candidates = [result.intrinsics_camera_id, camera_id]
    available = sorted(path.stem for path in lens_root.glob("*.json"))
    if len(available) == 1:
        candidates.append(available[0])
    intrinsics_path = next(
        (lens_root / f"{name}.json" for name in candidates if name and (lens_root / f"{name}.json").is_file()),
        None,
    )
    if intrinsics_path is None:
        return None
    intrinsics = result_from_dict(load_intrinsics(intrinsics_path))
    scaled = scale_camera_matrix(
        intrinsics.camera_matrix, (intrinsics.image_width, intrinsics.image_height), (width, height)
    )
    return CameraPose(
        fx=float(scaled[0][0]) / width if isinstance(scaled, list) else float(scaled[0, 0]) / width,
        fy=float(scaled[1][1]) / width if isinstance(scaled, list) else float(scaled[1, 1]) / width,
        cx=float(scaled[0][2]) / width if isinstance(scaled, list) else float(scaled[0, 2]) / width,
        cy=float(scaled[1][2]) / width if isinstance(scaled, list) else float(scaled[1, 2]) / width,
        aspect=height / float(width),
        rotation=rotation,
        translation=np.array(result.tvec, dtype=np.float64),
        reprojection_error=float(result.residual_mm),
        borrowed_intrinsics=intrinsics_path.stem if result.intrinsics_borrowed else "",
        note=(
            f"운영자 `지면 기준점` 측정 ({result.measured_at[:19]}) · 잔차 {result.residual_mm:.0f} mm · "
            f"측정 장비 {result.source_host or '이 장비'} · 렌즈 {intrinsics_path.stem}"
        ),
    )


def pose_from_intrinsics(
    calib: GroundCalibration,
    undistort,
    *,
    width: int = 1920,
    height: int = 1080,
) -> CameraPose | None:
    """측정된 내부 파라미터 + 지면 대응점 → 카메라 자세 (solvePnP).

    대응점은 전부 z=0 평면 위에 있으므로 평면 전용 해법(IPPE)을 쓴다. 영상은 이미 왜곡
    보정됐다고 보고 왜곡 계수는 0을 넘긴다.
    """
    import cv2
    import numpy as np

    if len(calib.correspondences) < 4:
        return None
    object_points = np.array([[c[0], c[1], 0.0] for c in calib.correspondences], dtype=np.float64)
    image_points = np.array([[c[2] * width, c[3] * height] for c in calib.correspondences], dtype=np.float64)
    matrix = undistort.matrix_for(width, height)
    flags = cv2.SOLVEPNP_IPPE if len(object_points) >= 4 else cv2.SOLVEPNP_ITERATIVE
    ok, rvec, tvec = cv2.solvePnP(
        object_points, image_points, matrix, np.zeros(5), flags=flags
    )
    if not ok:
        return None
    ok, rvec, tvec = cv2.solvePnP(
        object_points, image_points, matrix, np.zeros(5), rvec=rvec, tvec=tvec,
        useExtrinsicGuess=True, flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not ok:
        return None
    rotation, _ = cv2.Rodrigues(rvec)
    projected, _ = cv2.projectPoints(object_points, rvec, tvec, matrix, np.zeros(5))
    error = float(np.mean(np.linalg.norm(projected.reshape(-1, 2) - image_points, axis=1))) / width
    return CameraPose(
        fx=float(matrix[0, 0]) / width,
        fy=float(matrix[1, 1]) / width,
        cx=float(matrix[0, 2]) / width,
        cy=float(matrix[1, 2]) / width,
        aspect=height / float(width),
        rotation=rotation,
        translation=np.asarray(tvec, dtype=np.float64).reshape(3),
        reprojection_error=error,
        borrowed_intrinsics=getattr(undistort, "borrowed_from", ""),
    )


def _nelder_mead(cost, seed, *, step, iterations: int = 1200, tolerance: float = 1e-9):
    """작은 Nelder-Mead. scipy가 없는 환경이라 직접 둔다 (6개 변수, 결정적)."""
    import numpy as np

    seed = np.asarray(seed, dtype=np.float64)
    size = seed.size
    simplex = [seed.copy()]
    for index in range(size):
        point = seed.copy()
        point[index] += step[index]
        simplex.append(point)
    values = [cost(point) for point in simplex]

    for _ in range(iterations):
        order = np.argsort(values)
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) <= tolerance * (abs(values[0]) + tolerance):
            break
        centroid = np.mean(simplex[:-1], axis=0)
        reflected = centroid + (centroid - simplex[-1])
        reflected_value = cost(reflected)
        if reflected_value < values[0]:
            expanded = centroid + 2.0 * (centroid - simplex[-1])
            expanded_value = cost(expanded)
            simplex[-1], values[-1] = (
                (expanded, expanded_value) if expanded_value < reflected_value else (reflected, reflected_value)
            )
        elif reflected_value < values[-2]:
            simplex[-1], values[-1] = reflected, reflected_value
        else:
            contracted = centroid + 0.5 * (simplex[-1] - centroid)
            contracted_value = cost(contracted)
            if contracted_value < values[-1]:
                simplex[-1], values[-1] = contracted, contracted_value
            else:
                for index in range(1, len(simplex)):
                    simplex[index] = simplex[0] + 0.5 * (simplex[index] - simplex[0])
                    values[index] = cost(simplex[index])
    order = int(np.argmin(values))
    return simplex[order], values[order]


def refine_pose_with_rails(
    pose: CameraPose,
    rail_samples: Sequence[tuple[float, float, float]],
    anchors: Sequence[tuple[float, float, float, float]],
    *,
    anchor_weight: float = 1.0,
):
    """레일 전체 + 소수의 기준점으로 자세를 다시 맞춘다.

    ``rail_samples`` = (u_norm, v_norm, world_y) — 자동 검출한 노란 레일 위의 점들. x는
    모르지만 y는 안다(±1100). 이 점들이 보정 후에는 **직선**이므로 화면 끝까지 믿을 수 있어,
    손으로 읽어야 했던 팔레트 먼 쪽 모서리가 더 이상 필요 없다.
    ``anchors`` = (world_x, world_y, u_norm, v_norm) — x 원점을 잡아 줄 확실한 점 몇 개.

    반환: 새 CameraPose. 잔차는 mm 단위 RMS로 ``reprojection_error``에 mm 그대로 담는다.
    """
    import cv2
    import numpy as np

    if not rail_samples or not anchors:
        return pose

    rail_image = np.array([[u, v] for u, v, _y in rail_samples], dtype=np.float64)
    rail_y = np.array([y for _u, _v, y in rail_samples], dtype=np.float64)
    anchor_world = np.array([[a[0], a[1]] for a in anchors], dtype=np.float64)
    anchor_image = np.array([[a[2], a[3]] for a in anchors], dtype=np.float64)

    def build(params) -> CameraPose:
        rotation, _ = cv2.Rodrigues(np.asarray(params[:3], dtype=np.float64))
        return CameraPose(
            fx=pose.fx,
            fy=pose.fy,
            cx=pose.cx,
            cy=pose.cy,
            aspect=pose.aspect,
            rotation=rotation,
            translation=np.asarray(params[3:], dtype=np.float64),
            borrowed_intrinsics=pose.borrowed_intrinsics,
        )

    def ground_points(candidate: CameraPose, image_points):
        matrix = np.linalg.inv(candidate.ground_homography())
        pts = np.column_stack([image_points, np.ones(len(image_points))]).T
        world = matrix @ pts
        with np.errstate(divide="ignore", invalid="ignore"):
            world = world[:2] / world[2]
        return world.T

    seed_center = np.asarray(pose.center, dtype=np.float64)

    def cost(params) -> float:
        candidate = build(params)
        if candidate.translation[2] <= 0:
            return 1e12
        center = np.asarray(candidate.center, dtype=np.float64)
        # 물리적으로 말이 안 되는 해(카메라가 바닥에 붙거나 천장을 뚫음)는 버린다. 제약이
        # 빠듯하면 최적화가 기준점 위로 카메라를 붕괴시켜 잔차 0을 만들어 버린다.
        if not (600.0 <= center[2] <= 6000.0):
            return 1e12
        try:
            rails = ground_points(candidate, rail_image)
            fixed = ground_points(candidate, anchor_image)
        except np.linalg.LinAlgError:
            return 1e12
        if not np.all(np.isfinite(rails)) or not np.all(np.isfinite(fixed)):
            return 1e12
        rail_error = float(np.mean((rails[:, 1] - rail_y) ** 2))
        anchor_error = float(np.mean(np.sum((fixed - anchor_world) ** 2, axis=1)))
        # 씨앗(solvePnP 해)에서 너무 멀어지지 않게 하는 약한 항. 제약이 6개뿐일 때
        # 엉뚱한 국소해로 달아나는 것을 막는다.
        prior = float(np.sum((center - seed_center) ** 2)) * 5e-3
        return rail_error + anchor_weight * anchor_error + prior

    rvec, _ = cv2.Rodrigues(np.asarray(pose.rotation, dtype=np.float64))
    seed = np.concatenate([np.asarray(rvec).reshape(3), np.asarray(pose.translation).reshape(3)])
    step = np.array([0.05, 0.05, 0.05, 200.0, 200.0, 200.0])
    best, value = _nelder_mead(cost, seed, step=step)
    refined = build(best)
    refined.reprojection_error = math.sqrt(max(value, 0.0))
    return refined


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
    return CameraPose(
        fx=focal,
        fy=focal,
        cx=0.5,
        cy=0.5 * aspect,
        aspect=aspect,
        rotation=rotation,
        translation=translation,
    )


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
