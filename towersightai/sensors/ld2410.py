from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from towersightai.config.settings import LD2410Config

REPORT_HEADER = b"\xF4\xF3\xF2\xF1"
REPORT_TAIL = b"\xF8\xF7\xF6\xF5"
DATA_TYPE_ENGINEERING = 0x01
DATA_TYPE_BASIC = 0x02
_MAX_PAYLOAD_BYTES = 128
_MAX_BUFFERED_READINGS = 4096


@dataclass(frozen=True)
class LD2410Frame:
    received_at: datetime
    data_type: int
    target_status: int
    moving_distance_cm: int
    moving_energy: int
    motionless_distance_cm: int
    motionless_energy: int
    detection_distance_cm: int
    max_moving_gate: int
    max_motionless_gate: int
    moving_gate_energy: tuple[int, ...]
    motionless_gate_energy: tuple[int, ...]
    light: int | None = None
    out_pin: int | None = None
    raw_hex: str = ""

    @property
    def target_status_text(self) -> str:
        return {
            0x00: "No target",
            0x01: "Moving",
            0x02: "Motionless",
            0x03: "Moving + Motionless",
        }.get(self.target_status, f"Unknown({self.target_status})")

    def to_raw_payload(self) -> dict[str, Any]:
        return {
            "data_type": self.data_type,
            "target_status": self.target_status,
            "target_status_text": self.target_status_text,
            "moving_distance_cm": self.moving_distance_cm,
            "moving_energy": self.moving_energy,
            "motionless_distance_cm": self.motionless_distance_cm,
            "motionless_energy": self.motionless_energy,
            "detection_distance_cm": self.detection_distance_cm,
            "max_moving_gate": self.max_moving_gate,
            "max_motionless_gate": self.max_motionless_gate,
            "moving_gate_energy": list(self.moving_gate_energy),
            "motionless_gate_energy": list(self.motionless_gate_energy),
            "light": self.light,
            "out_pin": self.out_pin,
            "raw_hex": self.raw_hex,
        }


class LD2410ParseError(ValueError):
    pass


class LD2410Parser:
    """Incrementally parse raw LD2410 engineering/basic uplink frames."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.parse_error_count = 0

    def reset(self) -> None:
        self.buffer.clear()

    def feed(self, data: bytes, *, received_at: datetime | None = None) -> tuple[LD2410Frame, ...]:
        timestamp = received_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("LD2410 receive timestamp must be timezone-aware.")
        self.buffer.extend(data)
        frames: list[LD2410Frame] = []
        while True:
            header_index = self.buffer.find(REPORT_HEADER)
            if header_index < 0:
                self._keep_possible_header_prefix()
                break
            if header_index:
                del self.buffer[:header_index]
                self.parse_error_count += 1
            if len(self.buffer) < 10:
                break
            payload_len = int.from_bytes(self.buffer[4:6], "little")
            if payload_len <= 0 or payload_len > _MAX_PAYLOAD_BYTES:
                del self.buffer[0]
                self.parse_error_count += 1
                continue
            total_len = 4 + 2 + payload_len + 4
            if len(self.buffer) < total_len:
                break
            raw = bytes(self.buffer[:total_len])
            del self.buffer[:total_len]
            if raw[-4:] != REPORT_TAIL:
                self.parse_error_count += 1
                continue
            try:
                frames.append(self.parse_payload(raw[6:-4], raw=raw, received_at=timestamp))
            except LD2410ParseError:
                self.parse_error_count += 1
        return tuple(frames)

    def parse_payload(self, payload: bytes, *, raw: bytes, received_at: datetime) -> LD2410Frame:
        if len(payload) < 13:
            raise LD2410ParseError("payload_too_short")
        if payload[1] != 0xAA or payload[-2:] != b"\x55\x00":
            raise LD2410ParseError("payload_marker_mismatch")
        data_type = payload[0]
        target = payload[2:-2]
        if len(target) < 9:
            raise LD2410ParseError("target_data_too_short")

        target_status = target[0]
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
            if moving_count > 9 or motionless_count > 9:
                raise LD2410ParseError("gate_count_out_of_range")
            position = 11
            minimum = position + moving_count + motionless_count
            if len(target) < minimum:
                raise LD2410ParseError("gate_energy_truncated")
            for index, value in enumerate(target[position : position + moving_count]):
                moving_gates[index] = value
            position += moving_count
            for index, value in enumerate(target[position : position + motionless_count]):
                motionless_gates[index] = value
            position += motionless_count
            if len(target) > position:
                light = target[position]
                position += 1
            if len(target) > position:
                out_pin = target[position]
        elif data_type == DATA_TYPE_BASIC:
            moving_gates[0] = moving_energy
            motionless_gates[0] = motionless_energy
        else:
            raise LD2410ParseError(f"unknown_data_type_{data_type:02X}")

        return LD2410Frame(
            received_at=received_at.astimezone(timezone.utc),
            data_type=data_type,
            target_status=target_status,
            moving_distance_cm=moving_distance,
            moving_energy=moving_energy,
            motionless_distance_cm=motionless_distance,
            motionless_energy=motionless_energy,
            detection_distance_cm=detection_distance,
            max_moving_gate=max_moving_gate,
            max_motionless_gate=max_motionless_gate,
            moving_gate_energy=tuple(moving_gates),
            motionless_gate_energy=tuple(motionless_gates),
            light=light,
            out_pin=out_pin,
            raw_hex=raw.hex(" ").upper(),
        )

    def _keep_possible_header_prefix(self) -> None:
        keep = 0
        for length in range(min(len(self.buffer), len(REPORT_HEADER) - 1), 0, -1):
            if self.buffer[-length:] == REPORT_HEADER[:length]:
                keep = length
                break
        discard = len(self.buffer) - keep
        if discard > 0:
            del self.buffer[:discard]
            self.parse_error_count += 1


@dataclass(frozen=True)
class _ReceivedReading:
    frame: LD2410Frame
    client_ip: str


class LD2410ReadingBuffer:
    def __init__(self, *, buffer_seconds: float, max_sample_age_seconds: float) -> None:
        self.buffer_seconds = buffer_seconds
        self.max_sample_age_seconds = max_sample_age_seconds
        self._readings: deque[_ReceivedReading] = deque(maxlen=_MAX_BUFFERED_READINGS)
        self._lock = threading.Lock()

    def add(self, frame: LD2410Frame, *, client_ip: str) -> None:
        cutoff = frame.received_at - timedelta(seconds=self.buffer_seconds)
        with self._lock:
            self._readings.append(_ReceivedReading(frame, client_ip))
            while self._readings and self._readings[0].frame.received_at < cutoff:
                self._readings.popleft()

    def snapshot_at(self, sampled_at: datetime) -> dict[str, Any]:
        if sampled_at.tzinfo is None:
            raise ValueError("LD2410 sample timestamp must be timezone-aware.")
        sampled_utc = sampled_at.astimezone(timezone.utc)
        cutoff = sampled_utc - timedelta(seconds=self.buffer_seconds)
        selected: _ReceivedReading | None = None
        with self._lock:
            while self._readings and self._readings[0].frame.received_at < cutoff:
                self._readings.popleft()
            for reading in reversed(self._readings):
                if reading.frame.received_at <= sampled_utc:
                    selected = reading
                    break
        if selected is None:
            return {"status": "unavailable", "source": "ld2410_tcp", "received_at": None, "age_ms": None}
        age_seconds = max(0.0, (sampled_utc - selected.frame.received_at).total_seconds())
        return {
            "status": "fresh" if age_seconds <= self.max_sample_age_seconds else "stale",
            "source": "ld2410_tcp",
            "received_at": selected.frame.received_at.isoformat(),
            "age_ms": round(age_seconds * 1000),
            "client_ip": selected.client_ip,
            **selected.frame.to_raw_payload(),
        }


StatusCallback = Callable[[str, Mapping[str, Any]], None]
FrameCallback = Callable[[LD2410Frame, str], None]


class LD2410TCPService:
    """Listen for one ESP32 TCP client and retain recent parsed LD2410 readings."""

    def __init__(
        self,
        config: LD2410Config,
        *,
        status_callback: StatusCallback | None = None,
        frame_callback: FrameCallback | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.status_callback = status_callback
        self.frame_callback = frame_callback
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.readings = LD2410ReadingBuffer(
            buffer_seconds=config.buffer_seconds,
            max_sample_age_seconds=config.max_sample_age_seconds,
        )
        self._server: socket.socket | None = None
        self._client: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bound_host = ""
        self._bound_port = 0
        self._client_ip: str | None = None
        self._state_lock = threading.RLock()

    @property
    def listening(self) -> bool:
        with self._state_lock:
            return self._server is not None

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def client_ip(self) -> str | None:
        with self._state_lock:
            return self._client_ip

    def set_status_callback(self, callback: StatusCallback | None) -> None:
        self.status_callback = callback

    def set_frame_callback(self, callback: FrameCallback | None) -> None:
        self.frame_callback = callback

    def snapshot_at(self, sampled_at: datetime) -> dict[str, Any]:
        return self.readings.snapshot_at(sampled_at)

    def start(self) -> None:
        if self.listening:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.config.bind_host, self.config.port))
            server.listen(1)
            server.settimeout(0.25)
        except Exception:
            server.close()
            raise
        bound_host, bound_port = server.getsockname()[:2]
        with self._state_lock:
            self._server = server
            self._bound_host = str(bound_host)
            self._bound_port = int(bound_port)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve_loop, name="ld2410-tcp-server", daemon=True)
        self._emit_status("listening", {"bind_host": self._bound_host, "port": self._bound_port})
        self._thread.start()

    def stop(self) -> None:
        was_running = self.listening or bool(self._thread and self._thread.is_alive())
        self._stop.set()
        with self._state_lock:
            client, server = self._client, self._server
            self._client = None
            self._server = None
            self._client_ip = None
        self._close_socket(client)
        self._close_socket(server)
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        if was_running:
            self._emit_status("stopped", {})

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                server = self._server
            if server is None:
                return
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self._emit_status("error", {"reason": type(exc).__name__})
                break
            client.settimeout(0.25)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            client_ip = str(address[0])
            with self._state_lock:
                self._client = client
                self._client_ip = client_ip
            self._emit_status("client_connected", {"client_ip": client_ip})
            reason, parse_error_count = self._receive_client(client, client_ip)
            self._close_socket(client)
            with self._state_lock:
                if self._client is client:
                    self._client = None
                    self._client_ip = None
            if not self._stop.is_set():
                details = {
                    "client_ip": client_ip,
                    "reason": reason,
                    "parse_error_count": parse_error_count,
                }
                if parse_error_count:
                    logging.getLogger(__name__).warning(
                        "LD2410 discarded malformed input client_ip=%s count=%d",
                        client_ip,
                        parse_error_count,
                    )
                self._emit_status("client_disconnected", details)
        if not self._stop.is_set():
            with self._state_lock:
                self._close_socket(self._server)
                self._server = None

    def _receive_client(self, client: socket.socket, client_ip: str) -> tuple[str, int]:
        parser = LD2410Parser()
        last_valid = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_valid >= self.config.client_idle_timeout_seconds:
                return "idle_timeout", parser.parse_error_count
            try:
                data = client.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return "socket_error", parser.parse_error_count
            if not data:
                return "peer_closed", parser.parse_error_count
            frames = parser.feed(data, received_at=self.clock())
            if frames:
                last_valid = time.monotonic()
            for frame in frames:
                self.readings.add(frame, client_ip=client_ip)
                self._emit_frame(frame, client_ip)
        return "server_stopped", parser.parse_error_count

    def _emit_frame(self, frame: LD2410Frame, client_ip: str) -> None:
        if self.frame_callback is None:
            return
        try:
            self.frame_callback(frame, client_ip)
        except Exception:  # noqa: BLE001 - sensor ingest must survive telemetry callback failure.
            logging.getLogger(__name__).exception("LD2410 frame callback failed client_ip=%s", client_ip)

    def _emit_status(self, state: str, details: Mapping[str, Any]) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(state, details)
        except Exception:  # noqa: BLE001 - sensor ingest must survive telemetry callback failure.
            logging.getLogger(__name__).exception("LD2410 status callback failed state=%s", state)

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
