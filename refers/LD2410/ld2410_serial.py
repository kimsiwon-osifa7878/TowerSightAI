from __future__ import annotations

import math
import random
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

from ld2410_parser import (
    ENABLE_CONFIG_COMMAND,
    ENABLE_ENGINEERING_COMMAND,
    END_CONFIG_COMMAND,
    LD2410Frame,
    LD2410Parser,
)


BAUDRATE = 256000
TCP_PORT = 2410


def available_ports() -> list[str]:
    if list_ports is None:
        return []
    return [port.device for port in list_ports.comports()]


class LD2410SerialReader:
    def __init__(
        self,
        on_frame: Callable[[LD2410Frame], None],
        on_status: Callable[[str], None],
        raw_log_path: str = "raw_hex_errors.log",
    ) -> None:
        self.on_frame = on_frame
        self.on_status = on_status
        self.parser = LD2410Parser(raw_log_path)
        self._serial = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, port: str, baudrate: int = BAUDRATE) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        if self.connected:
            return
        self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=0.05)
        self._stop.clear()
        self._send_engineering_mode_commands()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self.on_status(f"Connected: {port} @ {baudrate}")

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self.on_status("Disconnected")

    def _send_engineering_mode_commands(self) -> None:
        if not self._serial:
            return
        for command in (ENABLE_CONFIG_COMMAND, ENABLE_ENGINEERING_COMMAND, END_CONFIG_COMMAND):
            self._serial.write(command)
            self._serial.flush()
            time.sleep(0.08)
            self._serial.read(self._serial.in_waiting or 0)

    def _read_loop(self) -> None:
        while not self._stop.is_set() and self._serial is not None:
            try:
                data = self._serial.read(self._serial.in_waiting or 64)
                if not data:
                    continue
                for frame in self.parser.feed(data):
                    self.on_frame(frame)
            except Exception as exc:
                self.on_status(f"Serial error: {exc}")
                break


class LD2410TCPServer:
    """Receive raw LD2410 protocol bytes from one TCP client at a time."""

    def __init__(
        self,
        on_frame: Callable[[LD2410Frame], None],
        on_status: Callable[[str], None],
        raw_log_path: str = "raw_hex_errors.log",
    ) -> None:
        self.on_frame = on_frame
        self.on_status = on_status
        self.parser = LD2410Parser(raw_log_path)
        self._server: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._bound_host = ""
        self._bound_port = 0

    @property
    def connected(self) -> bool:
        """True while the TCP server is listening, even before a client connects."""
        return self._server is not None

    @property
    def bound_port(self) -> int:
        return self._bound_port

    def start(self, host: str = "0.0.0.0", port: int = TCP_PORT) -> None:
        if self.connected:
            return
        if not 0 <= port <= 65535:
            raise ValueError("TCP port must be between 0 and 65535")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(1)
            server.settimeout(0.25)
        except Exception:
            server.close()
            raise

        bound_host, bound_port = server.getsockname()[:2]
        self._server = server
        self._bound_host = str(bound_host)
        self._bound_port = int(bound_port)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        self.on_status(f"TCP server listening: {self._bound_host}:{self._bound_port}")

    def stop(self) -> None:
        was_running = self.connected or (self._thread is not None and self._thread.is_alive())
        self._stop.set()
        self._close_socket(self._client)
        self._client = None
        self._close_socket(self._server)
        self._server = None
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        if was_running:
            self.on_status("TCP server stopped")

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                break
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.on_status(f"TCP server error: {exc}")
                break

            self._client = client
            client.settimeout(0.25)
            self.parser.buffer.clear()
            self.on_status(f"TCP client connected: {address[0]}:{address[1]}")
            try:
                self._receive_client(client)
            except Exception as exc:
                if not self._stop.is_set():
                    self.on_status(f"TCP receive error: {exc}")
            finally:
                self._close_socket(client)
                if self._client is client:
                    self._client = None
            if not self._stop.is_set():
                self.on_status(f"TCP client disconnected; listening: {self._bound_host}:{self._bound_port}")

        # An unexpected accept failure must not leave the server looking active.
        if not self._stop.is_set():
            self._close_socket(self._server)
            self._server = None

    def _receive_client(self, client: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data = client.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.on_status(f"TCP client error: {exc}")
                return
            if not data:
                return
            for frame in self.parser.feed(data):
                self.on_frame(frame)

    @staticmethod
    def _close_socket(sock: Optional[socket.socket]) -> None:
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


class SimulationReader:
    def __init__(self, on_frame: Callable[[LD2410Frame], None], on_status: Callable[[str], None]) -> None:
        self.on_frame = on_frame
        self.on_status = on_status
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mode = "empty_car"
        self._start_time = time.time()

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.connected:
            return
        self._stop.clear()
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.on_status("Simulation mode running")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.on_status("Simulation mode stopped")

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.on_frame(self._make_frame())
            time.sleep(0.18)

    def _make_frame(self) -> LD2410Frame:
        t = time.time() - self._start_time
        moving = []
        motionless = []
        for gate in range(9):
            base = 4 + gate * 0.8 + random.uniform(0, 4)
            still_boost = 0
            moving_boost = 0
            if self._mode == "person_still" and gate in (3, 4, 5):
                still_boost = 25 + 8 * math.sin(t + gate)
            elif self._mode == "person_moving" and gate in (3, 4, 5):
                moving_boost = 35 + 18 * abs(math.sin(t * 2.3 + gate))
                still_boost = 10 + 4 * math.sin(t + gate)
            moving.append(max(0, min(100, int(base + moving_boost + random.uniform(-2, 2)))))
            motionless.append(max(0, min(100, int(base + still_boost + random.uniform(-2, 2)))))

        status = 0
        if max(moving) > 25 and max(motionless) > 20:
            status = 3
        elif max(moving) > 25:
            status = 1
        elif max(motionless) > 20:
            status = 2

        return LD2410Frame(
            timestamp=datetime.now(),
            data_type=1,
            target_status=status,
            moving_distance_cm=75 * moving.index(max(moving)),
            moving_energy=max(moving),
            motionless_distance_cm=75 * motionless.index(max(motionless)),
            motionless_energy=max(motionless),
            detection_distance_cm=75 * max(moving.index(max(moving)), motionless.index(max(motionless))),
            max_moving_gate=8,
            max_motionless_gate=8,
            moving_gate_energy=moving,
            motionless_gate_energy=motionless,
            light=random.randint(20, 180),
            out_pin=1 if status else 0,
            raw_hex="SIMULATION",
        )
