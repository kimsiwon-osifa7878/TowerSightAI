from __future__ import annotations

from pathlib import Path

from towersightai.config.settings import Settings


def discover_hailo_hef_models(settings: Settings) -> tuple[Path, ...]:
    """Return only the HEF paired with the configured general-AI postprocess."""
    return (Path(settings.hailo_hef_path),)
