from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


REPORT_HEADER = b"\xF4\xF3\xF2\xF1"
REPORT_TAIL = b"\xF8\xF7\xF6\xF5"

DATA_TYPE_ENGINEERING = 0x01
DATA_TYPE_BASIC = 0x02


@dataclass
class LD2410Frame:
    timestamp: datetime
    data_type: int
    target_status: int
    moving_distance_cm: int
    moving_energy: int
    motionless_distance_cm: int
    motionless_energy: int
    detection_distance_cm: int
    max_moving_gate: int
    max_motionless_gate: int
    moving_gate_energy: List[int]
    motionless_gate_energy: List[int]
    light: Optional[int] = None
    out_pin: Optional[int] = None
    raw_hex: str = ""

    @property
    def target_status_text(self) -> str:
        return {
            0x00: "No target",
            0x01: "Moving",
            0x02: "Motionless",
            0x03: "Moving + Motionless",
        }.get(self.target_status, f"Unknown({self.target_status})")


class LD2410ParseError(ValueError):
    pass


class LD2410Parser:
    """Incremental parser for LD2410 engineering/basic uplink frames."""

    def __init__(self, raw_log_path: str | Path = "raw_hex_errors.log") -> None:
        self.buffer = bytearray()
        self.raw_log_path = Path(raw_log_path)

    def feed(self, data: bytes) -> List[LD2410Frame]:
        self.buffer.extend(data)
        frames: List[LD2410Frame] = []

        while True:
            header_index = self.buffer.find(REPORT_HEADER)
            if header_index < 0:
                # A serial read may end in the middle of the four-byte header.
                # Keep the longest suffix that can still become a valid header
                # when the next chunk arrives.
                keep = 0
                max_prefix = min(len(self.buffer), len(REPORT_HEADER) - 1)
                for prefix_len in range(max_prefix, 0, -1):
                    if self.buffer[-prefix_len:] == REPORT_HEADER[:prefix_len]:
                        keep = prefix_len
                        break
                discard_len = len(self.buffer) - keep
                if discard_len > 0:
                    self._log_bad_bytes(bytes(self.buffer[:discard_len]), "header_not_found")
                    del self.buffer[:discard_len]
                break

            if header_index > 0:
                self._log_bad_bytes(bytes(self.buffer[:header_index]), "discard_before_header")
                del self.buffer[:header_index]

            if len(self.buffer) < 10:
                break

            payload_len = int.from_bytes(self.buffer[4:6], "little")
            total_len = 4 + 2 + payload_len + 4
            if payload_len <= 0 or payload_len > 128:
                self._log_bad_bytes(bytes(self.buffer[:6]), "invalid_length")
                del self.buffer[0]
                continue

            if len(self.buffer) < total_len:
                break

            raw = bytes(self.buffer[:total_len])
            del self.buffer[:total_len]

            if raw[-4:] != REPORT_TAIL:
                self._log_bad_bytes(raw, "tail_mismatch")
                continue

            payload = raw[6:-4]
            try:
                frames.append(self.parse_payload(payload, raw))
            except LD2410ParseError as exc:
                self._log_bad_bytes(raw, str(exc))

        return frames

    def parse_payload(self, payload: bytes, raw: bytes = b"") -> LD2410Frame:
        if len(payload) < 13:
            raise LD2410ParseError("payload_too_short")
        if payload[1] != 0xAA or payload[-2:] != b"\x55\x00":
            raise LD2410ParseError("payload_marker_mismatch")

        data_type = payload[0]
        target = payload[2:-2]
        if len(target) < 9:
            raise LD2410ParseError("target_data_too_short")

        status = target[0]
        moving_distance = int.from_bytes(target[1:3], "little")
        moving_energy = target[3]
        motionless_distance = int.from_bytes(target[4:6], "little")
        motionless_energy = target[6]
        detection_distance = int.from_bytes(target[7:9], "little")

        moving_gates = [0] * 9
        motionless_gates = [0] * 9
        max_moving_gate = 0
        max_motionless_gate = 0
        light = None
        out_pin = None

        if data_type == DATA_TYPE_ENGINEERING:
            if len(target) < 13:
                raise LD2410ParseError("engineering_data_too_short")
            max_moving_gate = target[9]
            max_motionless_gate = target[10]
            moving_count = max_moving_gate + 1
            motionless_count = max_motionless_gate + 1
            pos = 11
            min_needed = pos + moving_count + motionless_count
            if len(target) < min_needed:
                raise LD2410ParseError("gate_energy_truncated")

            moving_values = list(target[pos : pos + moving_count])
            pos += moving_count
            motionless_values = list(target[pos : pos + motionless_count])
            pos += motionless_count

            for i, value in enumerate(moving_values[:9]):
                moving_gates[i] = value
            for i, value in enumerate(motionless_values[:9]):
                motionless_gates[i] = value

            if len(target) > pos:
                light = target[pos]
                pos += 1
            if len(target) > pos:
                out_pin = target[pos]
        elif data_type == DATA_TYPE_BASIC:
            moving_gates[0] = moving_energy
            motionless_gates[0] = motionless_energy
        else:
            raise LD2410ParseError(f"unknown_data_type_{data_type:02X}")

        return LD2410Frame(
            timestamp=datetime.now(),
            data_type=data_type,
            target_status=status,
            moving_distance_cm=moving_distance,
            moving_energy=moving_energy,
            motionless_distance_cm=motionless_distance,
            motionless_energy=motionless_energy,
            detection_distance_cm=detection_distance,
            max_moving_gate=max_moving_gate,
            max_motionless_gate=max_motionless_gate,
            moving_gate_energy=moving_gates,
            motionless_gate_energy=motionless_gates,
            light=light,
            out_pin=out_pin,
            raw_hex=raw.hex(" ").upper() if raw else "",
        )

    def _log_bad_bytes(self, data: bytes, reason: str) -> None:
        if not data:
            return
        self.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {reason}: {data.hex(' ').upper()}\n"
        with self.raw_log_path.open("a", encoding="utf-8") as fp:
            fp.write(line)


def build_command(command_word: int, value: bytes = b"") -> bytes:
    payload = command_word.to_bytes(2, "little") + value
    return b"\xFD\xFC\xFB\xFA" + len(payload).to_bytes(2, "little") + payload + b"\x04\x03\x02\x01"


ENABLE_CONFIG_COMMAND = build_command(0x00FF, b"\x01\x00")
END_CONFIG_COMMAND = build_command(0x00FE)
ENABLE_ENGINEERING_COMMAND = build_command(0x0062)
CLOSE_ENGINEERING_COMMAND = build_command(0x0063)
