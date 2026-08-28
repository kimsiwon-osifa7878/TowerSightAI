from __future__ import annotations

import socket
import time
from datetime import datetime, timedelta, timezone

from towersightai.config.settings import LD2410Config
from towersightai.sensors.ld2410 import (
    LD2410Parser,
    LD2410ReadingBuffer,
    LD2410TCPService,
    REPORT_HEADER,
    REPORT_TAIL,
)


def _engineering_frame(*, moving_energy: int = 42, motionless_energy: int = 31) -> bytes:
    moving_gates = bytes(range(10, 19))
    motionless_gates = bytes(range(20, 29))
    target = (
        bytes((3,))
        + (225).to_bytes(2, "little")
        + bytes((moving_energy,))
        + (150).to_bytes(2, "little")
        + bytes((motionless_energy,))
        + (300).to_bytes(2, "little")
        + bytes((8, 8))
        + moving_gates
        + motionless_gates
        + bytes((7, 1))
    )
    payload = bytes((1, 0xAA)) + target + b"\x55\x00"
    return REPORT_HEADER + len(payload).to_bytes(2, "little") + payload + REPORT_TAIL


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate, *, timeout: float = 2.0) -> None:  # noqa: ANN001 - compact test helper.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_parser_handles_noise_split_and_coalesced_engineering_frames():
    parser = LD2410Parser()
    first_raw = _engineering_frame(moving_energy=42)
    second_raw = _engineering_frame(moving_energy=88)
    received_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)

    assert parser.feed(b"noise" + first_raw[:7], received_at=received_at) == ()
    frames = parser.feed(first_raw[7:] + second_raw, received_at=received_at)

    assert len(frames) == 2
    first, second = frames
    assert first.received_at == received_at
    assert first.target_status_text == "Moving + Motionless"
    assert first.moving_distance_cm == 225
    assert first.motionless_distance_cm == 150
    assert first.detection_distance_cm == 300
    assert first.moving_gate_energy == tuple(range(10, 19))
    assert first.motionless_gate_energy == tuple(range(20, 29))
    assert first.light == 7
    assert first.out_pin == 1
    assert first.raw_hex == first_raw.hex(" ").upper()
    assert second.moving_energy == 88
    assert parser.parse_error_count >= 1


def test_parser_rejects_bad_tail_and_recovers_for_next_frame():
    parser = LD2410Parser()
    invalid = bytearray(_engineering_frame())
    invalid[-1] = 0

    frames = parser.feed(bytes(invalid) + _engineering_frame(moving_energy=77))

    assert len(frames) == 1
    assert frames[0].moving_energy == 77
    assert parser.parse_error_count == 1


def test_reading_buffer_selects_latest_past_frame_and_marks_age():
    parser = LD2410Parser()
    start = datetime(2026, 8, 25, tzinfo=timezone.utc)
    buffer = LD2410ReadingBuffer(buffer_seconds=30, max_sample_age_seconds=1)
    first = parser.feed(_engineering_frame(moving_energy=11), received_at=start)[0]
    second = parser.feed(_engineering_frame(moving_energy=22), received_at=start + timedelta(seconds=0.5))[0]
    buffer.add(first, client_ip="192.168.1.50")
    buffer.add(second, client_ip="192.168.1.50")

    assert buffer.snapshot_at(start - timedelta(milliseconds=1))["status"] == "unavailable"
    aligned = buffer.snapshot_at(start + timedelta(milliseconds=750))
    assert aligned["status"] == "fresh"
    assert aligned["age_ms"] == 250
    assert aligned["moving_energy"] == 22
    assert aligned["client_ip"] == "192.168.1.50"
    assert buffer.snapshot_at(start + timedelta(seconds=2))["status"] == "stale"
    assert buffer.snapshot_at(start + timedelta(seconds=31))["status"] == "unavailable"


def test_tcp_service_receives_split_frame_and_accepts_reconnection():
    received_at = datetime.now(timezone.utc)
    statuses: list[tuple[str, dict]] = []
    callback_frames: list[tuple[int, str]] = []
    service = LD2410TCPService(
        LD2410Config(bind_host="127.0.0.1", port=_free_port(), client_idle_timeout_seconds=1),
        status_callback=lambda state, details: statuses.append((state, dict(details))),
        frame_callback=lambda frame, client_ip: callback_frames.append((frame.moving_energy, client_ip)),
        clock=lambda: received_at,
    )
    service.start()
    try:
        first_raw = _engineering_frame(moving_energy=33)
        with socket.create_connection(("127.0.0.1", service.bound_port), timeout=1) as client:
            client.sendall(first_raw[:9])
            client.sendall(first_raw[9:])
            _wait_until(lambda: service.snapshot_at(received_at)["status"] == "fresh")
        _wait_until(lambda: any(state == "client_disconnected" for state, _ in statuses))

        with socket.create_connection(("127.0.0.1", service.bound_port), timeout=1) as client:
            client.sendall(_engineering_frame(moving_energy=99))
            _wait_until(lambda: service.snapshot_at(received_at).get("moving_energy") == 99)
    finally:
        service.stop()

    assert statuses[0][0] == "listening"
    assert sum(state == "client_connected" for state, _ in statuses) == 2
    assert statuses[-1][0] == "stopped"
    assert callback_frames == [(33, "127.0.0.1"), (99, "127.0.0.1")]


def test_frame_callback_failure_does_not_escape_sensor_service(caplog):
    frame = LD2410Parser().feed(_engineering_frame())[0]
    service = LD2410TCPService(
        LD2410Config(),
        frame_callback=lambda _frame, _client_ip: (_ for _ in ()).throw(RuntimeError("ui unavailable")),
    )

    service._emit_frame(frame, "192.0.2.30")

    assert "LD2410 frame callback failed" in caplog.text


def test_tcp_service_disconnects_client_that_sends_no_valid_frame():
    statuses: list[tuple[str, dict]] = []
    service = LD2410TCPService(
        LD2410Config(bind_host="127.0.0.1", port=_free_port(), client_idle_timeout_seconds=0.15),
        status_callback=lambda state, details: statuses.append((state, dict(details))),
    )
    service.start()
    try:
        with socket.create_connection(("127.0.0.1", service.bound_port), timeout=1) as client:
            client.sendall(b"invalid")
            _wait_until(
                lambda: any(
                    state == "client_disconnected" and details["reason"] == "idle_timeout"
                    for state, details in statuses
                )
            )
    finally:
        service.stop()

    disconnected = next(details for state, details in statuses if state == "client_disconnected")
    assert disconnected["parse_error_count"] >= 1
