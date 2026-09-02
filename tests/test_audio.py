import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from towersightai.ui.audio import AudioAlertPlayer, ensure_warning_wav


def test_warning_wav_is_generated_once(tmp_path: Path):
    first = ensure_warning_wav(tmp_path)
    assert first.is_file()
    size = first.stat().st_size
    assert size > 1000
    second = ensure_warning_wav(tmp_path)
    assert second == first
    assert second.stat().st_size == size


def test_audio_player_degrades_silently(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("PyQt6.QtMultimedia") or name == "PyQt6.QtMultimedia":
            raise ImportError("QtMultimedia unavailable in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    player = AudioAlertPlayer(tmp_path)
    assert player.available is False
    player.play("exit_warning")  # must not raise
    player.play("exit_warning")


def test_audio_player_play_never_raises(tmp_path: Path):
    player = AudioAlertPlayer(tmp_path)
    player.play("exit_warning")
    player.play("exit_warning")
