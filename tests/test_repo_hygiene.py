"""Repository hygiene rules that a commit must not break.

These are cheap guards for mistakes that are expensive in the field rather than in review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_calibration_results_are_never_tracked_by_git():
    """Calibration measurements travel over the NAS (`calibration/share.py`), never through git.

    Each machine measures its own site, so a tracked result file collides with the local
    measurement and makes `git pull` abort with "untracked working tree files would be
    overwritten" — which is exactly what blocked the field device on 2026-09-21.
    """
    listing = _git("ls-files", "data/calibration")
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = [line for line in listing.stdout.splitlines() if line.endswith(".json")]
    assert tracked == [], (
        "캘리브레이션 결과가 git에 추적되고 있습니다. NAS 공유(share.py)를 쓰세요: " + ", ".join(tracked)
    )


@pytest.mark.parametrize(
    "path",
    [
        "data/calibration/ground/rear_side.json",
        "data/calibration/ground/hosts/site-host/rear_side.json",
        "data/calibration/intrinsics/front.json",
        "data/calibration/intrinsics/sessions/front-20260101Z/pose-01-center.png",
    ],
)
def test_calibration_runtime_paths_stay_ignored(path: str):
    """The ignore rules must keep covering every runtime calibration path, including new hosts."""
    result = _git("check-ignore", "-q", path)
    if result.returncode > 1:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, f".gitignore 가 {path} 를 더 이상 제외하지 않습니다"
