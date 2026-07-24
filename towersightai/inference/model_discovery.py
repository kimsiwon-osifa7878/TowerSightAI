from __future__ import annotations

from pathlib import Path

from towersightai.config.settings import Settings


def discover_hailo_hef_models(settings: Settings) -> tuple[Path, ...]:
    """Return local HEF candidates that can be selected for Hailo inference."""
    candidates: dict[str, Path] = {}
    for directory in _hef_search_dirs(settings):
        if not directory.exists() or not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.hef"), key=lambda item: str(item).lower()):
            candidates[str(path.resolve())] = path
    if not candidates:
        return (Path(settings.hailo_hef_path),)
    return tuple(sorted(candidates.values(), key=lambda path: (path.name.lower(), str(path).lower())))


def _hef_search_dirs(settings: Settings) -> tuple[Path, ...]:
    return (
        Path(settings.hailo_model_dir),
        Path(settings.hailo_hef_path).parent,
    )
