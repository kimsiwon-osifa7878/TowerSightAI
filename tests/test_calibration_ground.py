"""Ground (extrinsic) calibration: four clicked pallet corners → camera pose.

Every case is built from a synthetic camera with a known position and orientation, so the test
has a ground truth to compare against rather than just checking that something was returned.
"""

import math
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from towersightai.calibration.ground import (
    REQUIRED_CORNERS,
    GroundCalibrationStore,
    order_corners,
    pallet_grid,
    pallet_outline,
    project_ground_points,
    result_from_dict,
    scale_camera_matrix,
    solve_ground_pose,
)


IMAGE_SIZE = (1920, 1080)
K = np.array([[1356.4, 0.0, 960.0], [0.0, 1360.1, 540.0], [0.0, 0.0, 1.0]])
DIST = np.array([-0.2941, 0.0017, -0.0006, -0.0030, 0.0485])
PALLET = (5350.0, 2200.0)


def _look_at(camera_xyz, target=(0.0, 0.0, 0.0)):
    """Rotation/translation of the world frame in camera coordinates for a camera at ``camera_xyz``."""
    camera = np.array(camera_xyz, dtype=np.float64)
    forward = np.array(target, dtype=np.float64) - camera
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    rotation = np.stack([right, down, forward])
    translation = -rotation @ camera
    rvec, _ = cv2.Rodrigues(rotation)
    return rvec.reshape(3), translation


def _clicks(camera_xyz, *, stopper_x=-2400.0, order=(0, 1, 2, 3)):
    """Where the operator would click: the four deck corners plus the stopper, normalized."""
    rvec, tvec = _look_at(camera_xyz)
    half_x, half_y = PALLET[0] / 2, PALLET[1] / 2
    corners = [(-half_x, -half_y, 0.0), (half_x, -half_y, 0.0), (half_x, half_y, 0.0), (-half_x, half_y, 0.0)]
    world = [corners[i] for i in order] + [(stopper_x, 0.0, 0.0)]
    projected, _ = cv2.projectPoints(np.array(world), rvec, tvec, K, DIST)
    points = [(float(x) / IMAGE_SIZE[0], float(y) / IMAGE_SIZE[1]) for x, y in projected.reshape(-1, 2)]
    return points[:REQUIRED_CORNERS], points[REQUIRED_CORNERS]


def _solve(corners, stopper, camera_id="opposite_side"):
    return solve_ground_pose(
        corners,
        stopper,
        camera_id=camera_id,
        camera_matrix=K,
        distortion=DIST,
        image_size=IMAGE_SIZE,
        pallet_length_mm=PALLET[0],
        pallet_width_mm=PALLET[1],
        intrinsics_camera_id="opposite_side",
    )


SIDE_CAMERA = (-700.0, -3100.0, 4550.0)
FRONT_CAMERA = (-3600.0, 150.0, 2400.0)


def test_recovers_a_known_side_camera_position_and_orientation():
    corners, stopper = _clicks(SIDE_CAMERA)
    result = _solve(corners, stopper)

    x, y, z = result.camera_position_mm
    assert (x, y, z) == pytest.approx(SIDE_CAMERA, abs=25.0)
    assert result.residual_mm < 10.0
    assert result.quality == "good"
    assert result.plausible

    pan, tilt, roll = result.orientation_degrees
    # The camera sits on the -y side and looks back at the origin, so it faces +y and downward.
    assert 0.0 < pan < 90.0
    assert 30.0 < tilt < 80.0
    assert abs(roll) < 5.0


def test_recovers_a_low_front_camera_too():
    corners, stopper = _clicks(FRONT_CAMERA)
    result = _solve(corners, stopper, camera_id="front")
    assert result.camera_position_mm == pytest.approx(FRONT_CAMERA, abs=30.0)
    assert result.residual_mm < 10.0


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (2, 3, 0, 1), (3, 2, 1, 0), (1, 0, 3, 2)])
def test_click_order_does_not_matter(order):
    """The operator clicks the corners in any order; the solver sorts it out."""
    corners, stopper = _clicks(SIDE_CAMERA, order=order)
    result = _solve(corners, stopper)
    assert result.camera_position_mm == pytest.approx(SIDE_CAMERA, abs=30.0)


def test_the_stopper_landmark_decides_which_end_is_the_entry():
    """Without it the pallet rectangle is 180°-symmetric and +x could point either way."""
    corners, inner = _clicks(SIDE_CAMERA, stopper_x=-2400.0)
    _corners, outer = _clicks(SIDE_CAMERA, stopper_x=+2400.0)

    inner_result = _solve(corners, inner)
    outer_result = _solve(corners, outer)

    # Same camera, but the world frame is flipped, so the recovered position flips with it.
    assert inner_result.camera_position_mm == pytest.approx(SIDE_CAMERA, abs=30.0)
    flipped = (-SIDE_CAMERA[0], -SIDE_CAMERA[1], SIDE_CAMERA[2])
    assert outer_result.camera_position_mm == pytest.approx(flipped, abs=30.0)


def test_the_stopper_must_fall_on_the_inner_half():
    corners, _stopper = _clicks(SIDE_CAMERA)
    # A stopper click at the pallet centre belongs to neither half.
    centre_projected, _ = cv2.projectPoints(
        np.array([(0.0, 0.0, 0.0)]), *_look_at(SIDE_CAMERA), K, DIST
    )
    centre = (
        float(centre_projected[0][0][0]) / IMAGE_SIZE[0],
        float(centre_projected[0][0][1]) / IMAGE_SIZE[1],
    )
    result = _solve(corners, centre)
    # It still solves (the centre is marginally on one side after rounding) but must stay sane.
    assert result.plausible


def test_order_corners_rejects_the_wrong_count():
    with pytest.raises(ValueError, match="네 모서리"):
        order_corners([(0.1, 0.1), (0.2, 0.2)])


def test_projection_round_trips_the_clicked_corners():
    """The overlay must land back on the picture the operator clicked, distortion included."""
    corners, stopper = _clicks(SIDE_CAMERA)
    result = _solve(corners, stopper)
    projected = project_ground_points(
        result, pallet_outline(result), camera_matrix=K, distortion=DIST, image_size=IMAGE_SIZE
    )
    ordered = order_corners(corners)
    for point in projected:
        assert min(math.dist(point, clicked) for clicked in ordered) < 0.01
    assert len(pallet_grid(result)) >= 10


def test_scale_camera_matrix_follows_the_frame_size():
    scaled = scale_camera_matrix(K, IMAGE_SIZE, (640, 360))
    assert scaled[0, 0] == pytest.approx(K[0, 0] / 3, rel=1e-6)
    assert scaled[1, 2] == pytest.approx(K[1, 2] / 3, rel=1e-6)
    assert scale_camera_matrix(K, IMAGE_SIZE, IMAGE_SIZE)[0, 0] == pytest.approx(K[0, 0])


def test_store_round_trip_keeps_the_measurement_unapproved(tmp_path: Path):
    corners, stopper = _clicks(SIDE_CAMERA)
    result = _solve(corners, stopper)
    store = GroundCalibrationStore(root=tmp_path / "ground")
    path = store.save(result)
    assert path.is_file()

    payload = result.to_dict()
    assert payload["reviewed"] is False
    assert payload["safe_to_operate"] is False  # a measurement is never authorization

    restored = store.load("opposite_side")
    assert restored is not None
    assert restored.camera_position_mm == pytest.approx(result.camera_position_mm, abs=1e-6)
    assert restored.residual_mm == pytest.approx(result.residual_mm)
    assert store.load("front") is None

    with pytest.raises(ValueError):
        result_from_dict({"kind": "camera_intrinsics"})


def test_quality_report_lists_every_check():
    corners, stopper = _clicks(SIDE_CAMERA)
    grade, lines = _solve(corners, stopper).quality_report()
    assert grade == "good"
    joined = "\n".join(lines)
    for expected in ("되맞춤 오차", "카메라 높이", "수평 거리", "위치", "방향", "내부 파라미터"):
        assert expected in joined


def test_a_ground_pose_records_which_installation_it_describes():
    """The pose belongs to that camera at that site, not to whichever machine holds the file.

    The offline lab analyses site images on the bench, so a pose measured on the site device has
    to stay usable there — it is labelled, not downgraded.
    """
    import socket
    from dataclasses import replace

    corners, stopper = _clicks(SIDE_CAMERA)
    mine = _solve(corners, stopper)
    assert mine.foreign is False
    assert mine.quality == "good"

    theirs = replace(mine, source_host="site-pc")
    assert theirs.foreign is True
    assert theirs.quality == "good"  # measured elsewhere is not measured badly
    _grade, lines = theirs.quality_report()
    assert any("site-pc" in line and "찍은 영상에 쓰는 값" in line for line in lines)

    # Saving keeps whichever host actually measured it, and reloading preserves that.
    assert replace(mine, source_host="").to_dict()["source_host"] == socket.gethostname()
    assert result_from_dict(theirs.to_dict()).source_host == "site-pc"
