from __future__ import annotations

import pytest

from towersightai.cli.operator_ui import _build_parser, _resolve_fullscreen


@pytest.mark.parametrize(
    ("configured", "force_fullscreen", "force_windowed", "expected"),
    (
        (False, True, False, True),
        (True, False, True, False),
        (True, False, False, True),
        (False, False, False, False),
    ),
)
def test_operator_ui_display_mode_precedence(configured, force_fullscreen, force_windowed, expected):
    assert (
        _resolve_fullscreen(
            configured=configured,
            force_fullscreen=force_fullscreen,
            force_windowed=force_windowed,
        )
        is expected
    )


def test_operator_ui_rejects_conflicting_display_modes():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(("--fullscreen", "--windowed"))
