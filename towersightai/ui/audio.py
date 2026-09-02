"""Warning-audio playback for the driver display.

Audio is a best-effort supplement to the on-screen warning: when QtMultimedia
or an output device is unavailable (headless tests, no speaker) playback
degrades to silence with a single log line — the visual warning always stands
on its own.
"""

from __future__ import annotations

import logging
import math
import struct
import wave
from pathlib import Path

_LOGGER = logging.getLogger("towersightai.ui.audio")

DEFAULT_AUDIO_DIR = Path("artifacts/runtime/audio")
_TONE_SECONDS = 0.6
_TONE_HZ = 880
_SAMPLE_RATE = 22050


def ensure_warning_wav(directory: Path = DEFAULT_AUDIO_DIR) -> Path:
    """Generate the warning tone WAV on first use (stdlib only, no binary asset)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "warning-tone.wav"
    if path.is_file() and path.stat().st_size > 0:
        return path
    frames = int(_SAMPLE_RATE * _TONE_SECONDS)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        for index in range(frames):
            # short attack/decay envelope so the tone does not click
            envelope = min(1.0, index / 400, (frames - index) / 400)
            value = int(0.6 * envelope * 32767 * math.sin(2 * math.pi * _TONE_HZ * index / _SAMPLE_RATE))
            wav.writeframesraw(struct.pack("<h", value))
    return path


class AudioAlertPlayer:
    """Plays the repeated warning tone; silently unavailable without QtMultimedia."""

    def __init__(self, audio_dir: Path = DEFAULT_AUDIO_DIR) -> None:
        self.available = False
        self._effect = None
        self._reported = False
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtMultimedia import QSoundEffect

            wav_path = ensure_warning_wav(audio_dir)
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(wav_path.resolve())))
            effect.setVolume(0.9)
            self._effect = effect
            self.available = True
        except Exception as exc:  # noqa: BLE001 - audio must never break the safety UI
            _LOGGER.warning("audio unavailable, warnings are screen-only: %s", exc)

    def play(self, cue_id: str) -> None:
        if not self.available or self._effect is None:
            if not self._reported:
                _LOGGER.info("audio cue %s skipped (no audio output)", cue_id)
                self._reported = True
            return
        try:
            if not self._effect.isPlaying():
                self._effect.play()
        except Exception as exc:  # noqa: BLE001
            if not self._reported:
                _LOGGER.warning("audio playback failed, continuing screen-only: %s", exc)
                self._reported = True
            self.available = False


__all__ = ["AudioAlertPlayer", "DEFAULT_AUDIO_DIR", "ensure_warning_wav"]
