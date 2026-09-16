import json
import math
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from towersightai.calibration.checkerboard import (
    CheckerboardSpec,
    fits_paper,
    generate_checkerboard_files,
)
from towersightai.calibration.intrinsics import (
    CAPTURE_POSES,
    MIN_SAMPLES,
    IntrinsicsSample,
    IntrinsicsSessionStore,
    calibrate_intrinsics,
    detect_checkerboard,
    load_intrinsics,
)


SPEC = CheckerboardSpec(columns=10, rows=7, square_mm=25.0)
IMAGE_SIZE = (640, 480)
TRUE_K = np.array([[520.0, 0.0, 320.0], [0.0, 520.0, 240.0], [0.0, 0.0, 1.0]])
TRUE_DIST = np.array([-0.25, 0.08, 0.0, 0.0, 0.0])


def _synthetic_view(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    """Render the checkerboard through a known camera so calibration has a ground truth."""
    cols, rows = SPEC.inner_corners
    # Board texture: (columns+1) x (rows+1) squares including a white border cell.
    cell = 40
    board = np.full(((SPEC.rows + 2) * cell, (SPEC.columns + 2) * cell), 255, dtype=np.uint8)
    for r in range(SPEC.rows):
        for c in range(SPEC.columns):
            if (r + c) % 2 == 0:
                board[(r + 1) * cell : (r + 2) * cell, (c + 1) * cell : (c + 2) * cell] = 0
    # Board-plane coordinates in mm of the texture's four outer corners (origin = first inner corner).
    mm_per_cell = SPEC.square_mm
    src_mm = np.array(
        [
            [-2 * mm_per_cell, -2 * mm_per_cell, 0.0],
            [(cols + 1) * mm_per_cell, -2 * mm_per_cell, 0.0],
            [(cols + 1) * mm_per_cell, (rows + 1) * mm_per_cell, 0.0],
            [-2 * mm_per_cell, (rows + 1) * mm_per_cell, 0.0],
        ],
        dtype=np.float64,
    )
    src_px = np.array(
        [[0, 0], [board.shape[1], 0], [board.shape[1], board.shape[0]], [0, board.shape[0]]], dtype=np.float32
    )
    # Ideal pinhole view first (a homography is exact for a plane without distortion) ...
    dst, _ = cv2.projectPoints(src_mm, rvec, tvec, TRUE_K, np.zeros(5))
    homography = cv2.getPerspectiveTransform(src_px, dst.reshape(-1, 2).astype(np.float32))
    ideal = cv2.warpPerspective(board, homography, IMAGE_SIZE, borderValue=200)
    # ... then apply the true lens distortion by inverse mapping every output pixel.
    width, height = IMAGE_SIZE
    grid = np.stack(np.meshgrid(np.arange(width), np.arange(height)), axis=-1).reshape(-1, 1, 2).astype(np.float64)
    undistorted = cv2.undistortPoints(grid, TRUE_K, TRUE_DIST, P=TRUE_K).reshape(height, width, 2)
    map_x = undistorted[:, :, 0].astype(np.float32)
    map_y = undistorted[:, :, 1].astype(np.float32)
    return cv2.remap(ideal, map_x, map_y, cv2.INTER_LINEAR, borderValue=200)


def _views(count: int) -> list[np.ndarray]:
    """Board placements that mirror CAPTURE_POSES: centre, edges, corners, tilts, near/far."""
    cols, rows = SPEC.inner_corners
    board_center = np.array([(cols - 1) * SPEC.square_mm / 2, (rows - 1) * SPEC.square_mm / 2, 0.0])
    fx, cx, cy = TRUE_K[0, 0], TRUE_K[0, 2], TRUE_K[1, 2]
    placements = [
        ((320, 240), (0.0, 0.0, 0.0), 650),
        ((110, 240), (0.0, 0.0, 0.0), 650),
        ((530, 240), (0.0, 0.0, 0.0), 650),
        ((320, 85), (0.0, 0.0, 0.0), 650),
        ((320, 395), (0.0, 0.0, 0.0), 650),
        ((120, 90), (0.0, 0.0, 0.0), 700),
        ((520, 90), (0.0, 0.0, 0.0), 700),
        ((120, 390), (0.0, 0.0, 0.0), 700),
        ((520, 390), (0.0, 0.0, 0.0), 700),
        ((320, 240), (0.0, 0.5, 0.0), 650),
        ((320, 240), (0.0, -0.5, 0.0), 650),
        ((320, 240), (0.5, 0.0, 0.0), 650),
        ((320, 240), (-0.5, 0.0, 0.0), 650),
        ((320, 240), (0.2, 0.2, 0.3), 450),
        ((320, 240), (-0.2, 0.3, -0.2), 1000),
        ((200, 150), (0.3, -0.3, 0.1), 600),
    ]
    views = []
    for (px, py), rot, depth in placements[:count]:
        rvec = np.array(rot, dtype=np.float64)
        rotation, _ = cv2.Rodrigues(rvec)
        target = np.array([(px - cx) * depth / fx, (py - cy) * depth / fx, depth])
        tvec = target - rotation @ board_center
        views.append(_synthetic_view(rvec, tvec))
    return views


def test_checkerboard_spec_and_paper_fit():
    assert SPEC.inner_corners == (9, 6)
    assert fits_paper(SPEC, "A4")
    assert not fits_paper(CheckerboardSpec(columns=12, rows=9, square_mm=30.0), "A4")
    with pytest.raises(ValueError, match="must differ"):
        CheckerboardSpec(columns=8, rows=8)


def test_generated_board_files_are_detectable(tmp_path: Path):
    pdf, svg, png = generate_checkerboard_files(SPEC, "A4", tmp_path, dpi=100)
    assert pdf.read_bytes().startswith(b"%PDF-1.4")
    assert "<svg" in svg.read_text(encoding="utf-8")
    image = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
    assert image.shape == (827, 1169)  # A4 landscape at 100 dpi
    detection = detect_checkerboard(image, SPEC)
    assert detection is not None
    assert len(detection.corners) == 54


def test_detect_returns_none_without_a_board():
    blank = np.full((240, 320), 128, dtype=np.uint8)
    assert detect_checkerboard(blank, SPEC) is None
    assert detect_checkerboard(None, SPEC) is None


def test_capture_poses_cover_centre_edges_corners_tilts_and_distances():
    keys = [pose.key for pose in CAPTURE_POSES]
    assert len(keys) == len(set(keys)) >= MIN_SAMPLES
    for required in ("center", "left", "right", "top", "bottom", "tilt_left", "tilt_up", "near", "far"):
        assert required in keys
    assert all(pose.instruction for pose in CAPTURE_POSES)


def test_calibration_recovers_the_synthetic_camera(tmp_path: Path):
    store = IntrinsicsSessionStore(tmp_path / "intrinsics", "front")
    samples = []
    for index, view in enumerate(_views(16)):
        detection = detect_checkerboard(view, SPEC)
        assert detection is not None, f"synthetic view {index} not detected"
        bgr = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)
        samples.append(store.save_sample(index + 1, CAPTURE_POSES[index % len(CAPTURE_POSES)].key, bgr, detection))
    assert all(sample.image_path.is_file() for sample in samples)

    result = calibrate_intrinsics(samples, SPEC, camera_id="front", rotation_degrees=270)

    assert result.image_width == 640 and result.image_height == 480
    assert result.rms_reprojection_error < 0.6
    fx, fy = result.focal_px
    assert math.isclose(fx, 520.0, rel_tol=0.03)
    assert math.isclose(fy, 520.0, rel_tol=0.03)
    cx, cy = result.principal_point
    assert abs(cx - 320.0) < 12 and abs(cy - 240.0) < 12
    # Distortion coefficients trade off against each other (k1/k2/k3), so compare the
    # *effect* instead: undistorting sample points with the recovered model must agree with
    # the true model to within a pixel across the region the board covered.
    probe = np.array([[80, 60], [560, 60], [80, 420], [560, 420], [320, 240], [200, 400]], dtype=np.float64)
    probe = probe.reshape(-1, 1, 2)
    recovered_k = np.array(result.camera_matrix)
    recovered_dist = np.array(result.distortion)
    truth = cv2.undistortPoints(probe, TRUE_K, TRUE_DIST, P=TRUE_K).reshape(-1, 2)
    estimate = cv2.undistortPoints(probe, recovered_k, recovered_dist, P=recovered_k).reshape(-1, 2)
    assert np.max(np.linalg.norm(truth - estimate, axis=1)) < 1.5
    assert result.rotation_degrees == 270
    assert result.reviewed is False

    result_path, latest_path = store.save_result(result)
    saved = load_intrinsics(latest_path)
    assert saved["kind"] == "camera_intrinsics"
    assert saved["safe_to_operate"] is False
    assert saved["reviewed"] is False
    assert saved["checkerboard"]["inner_corners"] == [9, 6]
    assert json.loads(result_path.read_text(encoding="utf-8")) == saved
    assert "RMS" in result.summary()


def test_calibration_rejects_too_few_or_mixed_size_samples(tmp_path: Path):
    views = _views(MIN_SAMPLES)
    samples = []
    for index, view in enumerate(views):
        detection = detect_checkerboard(view, SPEC)
        assert detection is not None
        samples.append(IntrinsicsSample("center", tmp_path / f"{index}.png", detection, "t"))
    with pytest.raises(ValueError, match="부족"):
        calibrate_intrinsics(samples[:-1], SPEC, camera_id="front")

    smaller = cv2.resize(views[0], (320, 240))
    detection = detect_checkerboard(smaller, SPEC)
    assert detection is not None
    mixed = samples[:-1] + [IntrinsicsSample("center", tmp_path / "s.png", detection, "t")]
    with pytest.raises(ValueError, match="해상도"):
        calibrate_intrinsics(mixed, SPEC, camera_id="front")


def test_load_intrinsics_rejects_other_json(tmp_path: Path):
    path = tmp_path / "other.json"
    path.write_text('{"kind": "site"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a camera intrinsics"):
        load_intrinsics(path)
