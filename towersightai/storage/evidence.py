from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from towersightai.config.settings import CameraConfig, CameraRole, RawStorageConfig


ArtifactCallback = Callable[..., None]
FailureCallback = Callable[..., None]


@dataclass(frozen=True)
class _Frame:
    image: Any
    received_at: datetime


@dataclass(frozen=True)
class _Fragment:
    path: Path
    closed_at: datetime
    seconds: float

    @property
    def started_at(self) -> datetime:
        return self.closed_at - timedelta(seconds=self.seconds)


@dataclass
class _Session:
    session_id: str
    related_event_id: str
    kind: str
    started_at: datetime
    camera_ids: tuple[str, ...]
    close_at: datetime | None = None
    fragments: dict[str, list[_Fragment]] = field(default_factory=dict)
    finalized: set[str] = field(default_factory=set)


class EvidenceCoordinator:
    """Capture snapshots and lossless H.264/MKV evidence without blocking the Qt thread."""

    def __init__(
        self,
        config: RawStorageConfig,
        cameras: Sequence[CameraConfig],
        *,
        artifact_callback: ArtifactCallback,
        failure_callback: FailureCallback,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.cameras = {camera.id: camera for camera in cameras}
        self.artifact_callback = artifact_callback
        self.failure_callback = failure_callback
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timezone = ZoneInfo(config.timezone_name)
        self._frames: dict[str, _Frame] = {}
        self._healthy: dict[str, bool] = {camera.id: False for camera in cameras}
        self._fragments: dict[str, deque[_Fragment]] = {camera.id: deque() for camera in cameras}
        self._sessions: dict[str, _Session] = {}
        self._person_session_id: str | None = None
        self._vehicle_session_id: str | None = None
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._buffer_dirs: set[Path] = set()
        self._recorder_ready: set[str] = set()
        self._recorder_errors: dict[str, str] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="raw-evidence")
        self._closed = False
        if config.media_enabled:
            for camera in cameras:
                self._start_recorder(camera)

    @property
    def status_summary(self) -> str:
        if not self.config.media_enabled:
            return "증거 OFF"
        with self._lock:
            if self._recorder_errors:
                return f"증거 NG {len(self._recorder_errors)}"
            return f"증거 {len(self._recorder_ready)}/{len(self.cameras)}"

    def update_frame(self, camera_id: str, image: Any, *, received_at: datetime | None = None) -> None:
        if self._closed or camera_id not in self.cameras:
            return
        detached = image.copy() if hasattr(image, "copy") else image
        with self._lock:
            self._frames[camera_id] = _Frame(detached, received_at or self.clock())

    def update_camera_status(self, camera_id: str, status: str) -> None:
        with self._lock:
            if camera_id in self._healthy:
                self._healthy[camera_id] = status.startswith("정상")

    def handle_raw_event(self, record: Mapping[str, Any]) -> None:
        if self._closed or not self.config.media_enabled:
            return
        event_type = str(record.get("event_type") or "")
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if bool(payload.get("simulated")):
            return
        try:
            event_at = datetime.fromisoformat(str(record["recorded_at"]))
        except (KeyError, ValueError):
            event_at = self.clock()
        event_id = str(record.get("event_id") or uuid.uuid4().hex)
        if event_type == "vehicle_entered":
            camera_id = str(payload.get("camera_id") or self._front_camera_id() or "front")
            self._capture_snapshots(event_id, "vehicle", (camera_id,), event_at)
            if bool(payload.get("managed")):
                # Process-engine session: stays open until vehicle_session_ended
                # (parking start), instead of the legacy fixed post-roll close.
                self._vehicle_session_id = self._open_session(
                    event_id, "vehicle", (camera_id,), event_at
                )
            else:
                self._open_session(
                    event_id,
                    "vehicle",
                    (camera_id,),
                    event_at,
                    close_at=event_at + timedelta(seconds=self.config.media_vehicle_post_seconds),
                )
        elif event_type == "vehicle_session_ended" and self._vehicle_session_id:
            self._close_session(self._vehicle_session_id, event_at)
            self._vehicle_session_id = None
        elif event_type == "person_window_started":
            camera_ids = self._healthy_camera_ids()
            self._capture_snapshots(event_id, "person", camera_ids, event_at)
            self._person_session_id = self._open_session(event_id, "person", camera_ids, event_at)
        elif event_type == "person_window_closed" and self._person_session_id:
            # End-of-window screenshot pairs with the start screenshot for the bundle.
            self._capture_snapshots(event_id, "person_end", self._healthy_camera_ids(), event_at)
            self._close_session(self._person_session_id, event_at)
            self._person_session_id = None
        elif event_type == "plate_recognized":
            camera_id = str(payload.get("camera_id") or self._front_camera_id() or "front")
            source = payload.get("source_image_path")
            bbox = payload.get("plate_bbox")
            self._capture_plate(
                event_id,
                camera_id,
                event_at,
                str(source) if source else None,
                bbox if isinstance(bbox, Mapping) else None,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        now = self.clock()
        with self._lock:
            for session in self._sessions.values():
                if session.close_at is None:
                    session.close_at = now
        for process in tuple(self._processes.values()):
            if process.poll() is None:
                process.terminate()
        for process in tuple(self._processes.values()):
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        with self._lock:
            pending = tuple(self._sessions.values())
        for session in pending:
            for camera_id in session.camera_ids:
                self._schedule_finalize(session, camera_id)
        self._executor.shutdown(wait=True)
        for buffer_dir in self._buffer_dirs:
            shutil.rmtree(buffer_dir, ignore_errors=True)

    def _healthy_camera_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(camera_id for camera_id, healthy in self._healthy.items() if healthy)

    def _front_camera_id(self) -> str | None:
        return next((camera.id for camera in self.cameras.values() if camera.role is CameraRole.front), None)

    def _capture_snapshots(
        self,
        event_id: str,
        kind: str,
        camera_ids: Sequence[str],
        captured_at: datetime,
    ) -> None:
        for camera_id in camera_ids:
            with self._lock:
                frame = self._frames.get(camera_id)
                healthy = self._healthy.get(camera_id, False)
            if not healthy:
                self._fail(event_id, "snapshot", camera_id, "camera_not_healthy", captured_at)
                continue
            if frame is None or abs((captured_at - frame.received_at).total_seconds()) > self.config.media_frame_max_age_seconds:
                self._fail(event_id, "snapshot", camera_id, "latest_frame_missing_or_stale", captured_at)
                continue
            self._executor.submit(self._save_qimage, event_id, kind, camera_id, frame.image, captured_at)

    def _capture_plate(
        self,
        event_id: str,
        camera_id: str,
        captured_at: datetime,
        source_image_path: str | None,
        plate_bbox: Mapping[str, Any] | None,
    ) -> None:
        source = Path(source_image_path).expanduser() if source_image_path else None
        if source is not None and source.is_file():
            self._executor.submit(self._copy_plate_image, event_id, camera_id, source, captured_at, plate_bbox)
            return
        self._capture_snapshots(event_id, "plate", (camera_id,), captured_at)

    def _save_qimage(
        self,
        event_id: str,
        kind: str,
        camera_id: str,
        image: Any,
        captured_at: datetime,
    ) -> None:
        final = self._artifact_path(captured_at, "images", f"{captured_at:%H%M%S-%f}-{kind}-{camera_id}.jpg")
        temporary = final.with_name(f".{final.name}.{os.getpid()}.part")
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not image.save(str(temporary), "JPEG", self.config.media_snapshot_jpeg_quality):
                raise OSError("QImage JPEG encoding failed")
            temporary.replace(final)
            self._artifact(event_id, "snapshot", camera_id, final, captured_at, {"event_kind": kind})
        except Exception as exc:  # noqa: BLE001
            temporary.unlink(missing_ok=True)
            self._fail(event_id, "snapshot", camera_id, type(exc).__name__, captured_at)

    def _copy_plate_image(
        self,
        event_id: str,
        camera_id: str,
        source: Path,
        captured_at: datetime,
        plate_bbox: Mapping[str, Any] | None,
    ) -> None:
        suffix = source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
        final = self._artifact_path(captured_at, "images", f"{captured_at:%H%M%S-%f}-plate-{camera_id}{suffix}")
        temporary = final.with_name(f".{final.name}.{os.getpid()}.part")
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(final)
            self._artifact(event_id, "plate_image", camera_id, final, captured_at, {"source": "lpr"})
            if plate_bbox:
                self._save_plate_crop(event_id, camera_id, source, captured_at, plate_bbox)
        except Exception as exc:  # noqa: BLE001
            temporary.unlink(missing_ok=True)
            self._fail(event_id, "plate_image", camera_id, type(exc).__name__, captured_at)

    def _save_plate_crop(
        self,
        event_id: str,
        camera_id: str,
        source: Path,
        captured_at: datetime,
        bbox: Mapping[str, Any],
    ) -> None:
        final = self._artifact_path(captured_at, "images", f"{captured_at:%H%M%S-%f}-plate-crop-{camera_id}.jpg")
        temporary = final.with_name(f".{final.name}.{os.getpid()}.part")
        try:
            from PyQt6.QtGui import QImage

            image = QImage(str(source))
            x1 = max(0, min(image.width(), int(bbox["x1"])))
            y1 = max(0, min(image.height(), int(bbox["y1"])))
            x2 = max(x1, min(image.width(), int(bbox["x2"])))
            y2 = max(y1, min(image.height(), int(bbox["y2"])))
            if image.isNull() or x2 <= x1 or y2 <= y1:
                raise ValueError("invalid_plate_bbox")
            crop = image.copy(x1, y1, x2 - x1, y2 - y1)
            if not crop.save(str(temporary), "JPEG", self.config.media_snapshot_jpeg_quality):
                raise OSError("plate crop JPEG encoding failed")
            temporary.replace(final)
            self._artifact(
                event_id,
                "plate_crop",
                camera_id,
                final,
                captured_at,
                {"source": "lpr", "bbox": {key: int(bbox[key]) for key in ("x1", "y1", "x2", "y2")}},
            )
        except Exception as exc:  # noqa: BLE001
            temporary.unlink(missing_ok=True)
            self._fail(event_id, "plate_crop", camera_id, type(exc).__name__, captured_at)

    def _open_session(
        self,
        event_id: str,
        kind: str,
        camera_ids: Sequence[str],
        started_at: datetime,
        *,
        close_at: datetime | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        usable: list[str] = []
        with self._lock:
            for camera_id in camera_ids:
                if camera_id not in self._recorder_ready:
                    self._fail(event_id, "video", camera_id, "recorder_not_ready", started_at)
                    continue
                usable.append(camera_id)
            session = _Session(session_id, event_id, kind, started_at, tuple(usable), close_at)
            for camera_id in usable:
                threshold = started_at - timedelta(seconds=self.config.media_pre_seconds)
                session.fragments[camera_id] = [
                    fragment for fragment in self._fragments[camera_id] if fragment.closed_at >= threshold
                ]
            self._sessions[session_id] = session
        return session_id

    def _close_session(self, session_id: str, close_at: datetime) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.close_at = close_at

    def _start_recorder(self, camera: CameraConfig) -> None:
        rtsp_url = camera.record_rtsp_url or camera.rtsp_url
        if not rtsp_url:
            self._recorder_errors[camera.id] = "record_rtsp_url_missing"
            return
        buffer_dir = self.config.local_dir / ".buffer" / camera.id / f"run-{uuid.uuid4().hex[:12]}"
        buffer_dir.mkdir(parents=True, exist_ok=True)
        self._buffer_dirs.add(buffer_dir)
        helper = Path(__file__).resolve().parents[1] / "cli" / "event_video_recorder.py"
        try:
            process = subprocess.Popen(
                [str(self.config.media_gstreamer_python), str(helper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "camera_id": camera.id,
                        "rtsp_url": rtsp_url,
                        "output_dir": str(buffer_dir),
                        "segment_seconds": self.config.media_segment_seconds,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            process.stdin.close()
            self._processes[camera.id] = process
            threading.Thread(
                target=self._read_recorder,
                args=(camera.id, process),
                name=f"raw-recorder-{camera.id}",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001
            self._recorder_errors[camera.id] = type(exc).__name__

    def _read_recorder(self, camera_id: str, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            message_type = message.get("type")
            if message_type == "ready":
                with self._lock:
                    self._recorder_ready.add(camera_id)
                    self._recorder_errors.pop(camera_id, None)
            elif message_type == "error":
                with self._lock:
                    self._recorder_ready.discard(camera_id)
                    self._recorder_errors[camera_id] = str(message.get("reason") or "recorder_error")[:240]
            elif message_type == "fragment_closed":
                try:
                    fragment = _Fragment(
                        Path(str(message["path"])),
                        datetime.fromisoformat(str(message["closed_at"])),
                        float(message.get("segment_seconds") or self.config.media_segment_seconds),
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                self._on_fragment(camera_id, fragment)
        with self._lock:
            self._recorder_ready.discard(camera_id)
            if not self._closed and camera_id not in self._recorder_errors:
                self._recorder_errors[camera_id] = f"recorder_exit_{process.poll()}"

    def _on_fragment(self, camera_id: str, fragment: _Fragment) -> None:
        finalize: list[_Session] = []
        with self._lock:
            fragments = self._fragments[camera_id]
            fragments.append(fragment)
            for session in self._sessions.values():
                if camera_id not in session.camera_ids or camera_id in session.finalized:
                    continue
                lower = session.started_at - timedelta(seconds=self.config.media_pre_seconds)
                if fragment.closed_at >= lower and (session.close_at is None or fragment.started_at <= session.close_at):
                    session.fragments.setdefault(camera_id, []).append(fragment)
                if session.close_at is not None and fragment.closed_at >= session.close_at:
                    session.finalized.add(camera_id)
                    finalize.append(session)
            held = {
                item.path
                for session in self._sessions.values()
                for items in session.fragments.values()
                for item in items
            }
            cutoff = fragment.closed_at - timedelta(seconds=self.config.media_pre_seconds + self.config.media_segment_seconds)
            while fragments and fragments[0].closed_at < cutoff and fragments[0].path not in held:
                old = fragments.popleft()
                old.path.unlink(missing_ok=True)
        for session in finalize:
            self._executor.submit(self._finalize_video, session, camera_id)

    def _schedule_finalize(self, session: _Session, camera_id: str) -> None:
        with self._lock:
            if camera_id in session.finalized:
                return
            session.finalized.add(camera_id)
        self._executor.submit(self._finalize_video, session, camera_id)

    def _finalize_video(self, session: _Session, camera_id: str) -> None:
        fragments = list(dict.fromkeys(item.path for item in session.fragments.get(camera_id, ())))
        fragments = [path for path in fragments if path.is_file()]
        if not fragments:
            self._fail(session.related_event_id, "video", camera_id, "no_complete_fragments", session.started_at)
            self._finish_session_camera(session)
            return
        per_part = max(1, int(self.config.media_clip_part_seconds / self.config.media_segment_seconds))
        for index in range(0, len(fragments), per_part):
            part = fragments[index : index + per_part]
            part_number = index // per_part + 1
            final = self._artifact_path(
                session.started_at,
                "videos",
                f"{session.started_at:%H%M%S-%f}-{session.kind}-{camera_id}-part{part_number:03d}.mkv",
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            temporary = final.with_name(f".{final.name}.{os.getpid()}.part")
            try:
                if len(part) == 1:
                    shutil.copyfile(part[0], temporary)
                else:
                    concat_file = final.with_name(f".{final.stem}.{uuid.uuid4().hex}.concat")
                    concat_file.write_text(
                        "".join(
                            f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                            for path in part
                        ),
                        encoding="utf-8",
                    )
                    try:
                        subprocess.run(
                            [
                                "/usr/bin/ffmpeg",
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-f",
                                "concat",
                                "-safe",
                                "0",
                                "-i",
                                str(concat_file),
                                "-map",
                                "0:v:0",
                                "-c",
                                "copy",
                                "-an",
                                "-f",
                                "matroska",
                                "-y",
                                str(temporary),
                            ],
                            check=True,
                            timeout=max(30.0, len(part) * self.config.media_segment_seconds),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                        )
                    finally:
                        concat_file.unlink(missing_ok=True)
                temporary.replace(final)
                self._artifact(
                    session.related_event_id,
                    "video",
                    camera_id,
                    final,
                    session.started_at,
                    {
                        "event_kind": session.kind,
                        "container": "mkv",
                        "video_codec": "h264_passthrough",
                        "audio": False,
                        "pre_seconds": self.config.media_pre_seconds,
                        "part": part_number,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                temporary.unlink(missing_ok=True)
                self._fail(session.related_event_id, "video", camera_id, type(exc).__name__, session.started_at)
        self._finish_session_camera(session)

    def _finish_session_camera(self, session: _Session) -> None:
        with self._lock:
            if session.finalized.issuperset(session.camera_ids):
                self._sessions.pop(session.session_id, None)

    def _artifact_path(self, at: datetime, group: str, name: str) -> Path:
        local = at.astimezone(self.timezone)
        return self.config.local_dir / local.date().isoformat() / "media" / group / name

    def _artifact(
        self,
        event_id: str,
        kind: str,
        camera_id: str,
        path: Path,
        captured_at: datetime,
        metadata: Mapping[str, Any],
    ) -> None:
        day_dir = self.config.local_dir / captured_at.astimezone(self.timezone).date().isoformat()
        self.artifact_callback(
            related_event_id=event_id,
            kind=kind,
            camera_id=camera_id,
            relative_path=path.relative_to(day_dir).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
            captured_at=captured_at,
            metadata=metadata,
        )

    def _fail(self, event_id: str, kind: str, camera_id: str, reason: str, at: datetime) -> None:
        try:
            self.failure_callback(
                related_event_id=event_id,
                kind=kind,
                camera_id=camera_id,
                reason=reason,
                at=at,
            )
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("failed to record evidence error")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["EvidenceCoordinator"]
