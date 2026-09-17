"""렌즈 왜곡 보정 — 운영자 콘솔에서 측정한 내부 파라미터를 랩에 들여온다.

현장 카메라는 네 대 모두 **같은 기종(Tapo C310)**이라, 한 대에서 측정한 내부 파라미터를
나머지에 우선 빌려 쓴다(2026-09-16 사용자 지시). 빌려 쓴 카메라는 결과에 그렇게 표시한다 —
개체 편차가 있을 수 있으므로 각 카메라 실측이 최종 목표다.

이 모듈을 거치면 랩의 모든 좌표는 **보정된(undistorted) 영상 좌표계**가 된다. 보정 후에는
직선이 직선으로 찍히므로, 지면 호모그래피가 화면 전체에서 성립하고 카메라 자세 복원
(정사각 화소 핀홀 가정)도 비로소 맞아떨어진다 — 1차 검증에서 막혔던 지점이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from towersightai.calibration.intrinsics import IntrinsicsResult, load_intrinsics, result_from_dict

DEFAULT_INTRINSICS_ROOT = Path("data/calibration/intrinsics")
#: 자기 측정값이 없는 카메라가 빌려 쓸 기증 카메라.
DONOR_CAMERA = "opposite_side"


@dataclass(frozen=True)
class CameraUndistort:
    camera_id: str
    result: IntrinsicsResult
    borrowed_from: str = ""

    @property
    def borrowed(self) -> bool:
        return bool(self.borrowed_from)

    def label(self) -> str:
        if self.borrowed:
            return f"{self.camera_id}: {self.borrowed_from} 측정값을 빌려 씀 (동일 기종 C310)"
        return f"{self.camera_id}: 자체 측정값"

    def matrix_for(self, width: int, height: int):
        """측정 당시 해상도와 다르면 같은 비율로 옮긴 카메라 행렬."""
        import numpy as np

        matrix = np.asarray(self.result.camera_matrix, dtype=np.float64).copy()
        if (width, height) != (self.result.image_width, self.result.image_height):
            scale_x = width / float(self.result.image_width)
            scale_y = height / float(self.result.image_height)
            matrix[0, 0] *= scale_x
            matrix[0, 2] *= scale_x
            matrix[1, 1] *= scale_y
            matrix[1, 2] *= scale_y
        return matrix

    def distortion(self):
        import numpy as np

        return np.asarray(self.result.distortion, dtype=np.float64)

    def image(self, bgr):
        """영상 한 장을 보정한다. 출력 카메라 행렬은 입력과 동일하게 유지한다.

        같은 행렬을 쓰면 원본 화각의 일부가 잘려 나가지만, 좌표 변환이 단순해지고
        (``undistortPoints(..., P=K)``와 정확히 짝이 맞는다) 화면 비율도 그대로다.
        """
        import cv2

        height, width = bgr.shape[:2]
        return cv2.undistort(bgr, self.matrix_for(width, height), self.distortion())

    def points_norm(
        self, points: Iterable[tuple[float, float]], width: int, height: int
    ) -> list[tuple[float, float]]:
        """정규화 좌표(왜곡 있음) → 정규화 좌표(보정 후).

        손으로 찍어 둔 지면 대응점을 다시 읽지 않고 그대로 옮기기 위한 것이다.
        """
        import cv2
        import numpy as np

        pixels = np.array([[p[0] * width, p[1] * height] for p in points], dtype=np.float64)
        if pixels.size == 0:
            return []
        matrix = self.matrix_for(width, height)
        mapped = cv2.undistortPoints(pixels.reshape(-1, 1, 2), matrix, self.distortion(), P=matrix)
        return [(float(x) / width, float(y) / height) for x, y in mapped.reshape(-1, 2)]


def load_undistorts(
    cameras: Sequence[str],
    *,
    root: Path = DEFAULT_INTRINSICS_ROOT,
    donor: str = DONOR_CAMERA,
) -> dict[str, CameraUndistort]:
    """카메라별 보정기. 자체 측정이 없으면 기증 카메라 값을 빌려 쓴다."""
    root = Path(root)
    donor_result: IntrinsicsResult | None = None
    donor_path = root / f"{donor}.json"
    if donor_path.is_file():
        donor_result = result_from_dict(load_intrinsics(donor_path))

    loaded: dict[str, CameraUndistort] = {}
    for camera in cameras:
        path = root / f"{camera}.json"
        if path.is_file():
            loaded[camera] = CameraUndistort(camera, result_from_dict(load_intrinsics(path)))
        elif donor_result is not None:
            loaded[camera] = CameraUndistort(camera, donor_result, borrowed_from=donor)
    return loaded
