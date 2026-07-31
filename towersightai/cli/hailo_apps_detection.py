from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from towersightai.inference.events import normalize_hailo_detections


class PipelineDiagnostics:
    """Track progress through the pipeline without relying on detections."""

    def __init__(
        self,
        camera_ids: tuple[str, ...],
        path: Path,
        *,
        interval_seconds: float = 2.0,
        stall_seconds: float = 15.0,
        startup_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.camera_ids = camera_ids
        self.path = path
        self.interval_seconds = interval_seconds
        self.stall_seconds = stall_seconds
        self.startup_seconds = startup_seconds
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._ingress_counts: Counter[str] = Counter()
        self._inference_counts: Counter[str] = Counter()
        self._last_ingress: dict[str, float] = {}
        self._last_inference: dict[str, float] = {}
        self._stage_counts: dict[str, Counter[str]] = {}
        self._last_stage: dict[str, dict[str, float]] = {}
        self._queue_levels_provider: Callable[[], dict[str, object]] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def record_ingress(self, camera_id: str) -> None:
        now = self._monotonic()
        with self._lock:
            self._ingress_counts[camera_id] += 1
            self._last_ingress[camera_id] = now
            self._record_stage_locked("roundrobin_input", camera_id, now)

    def record_inference(self, camera_id: str) -> None:
        now = self._monotonic()
        with self._lock:
            self._inference_counts[camera_id] += 1
            self._last_inference[camera_id] = now
            self._record_stage_locked("callback", camera_id, now)

    def record_stage(self, stage: str, camera_id: str) -> None:
        now = self._monotonic()
        with self._lock:
            self._record_stage_locked(stage, camera_id, now)

    def set_queue_levels_provider(self, provider: Callable[[], dict[str, object]]) -> None:
        self._queue_levels_provider = provider

    def _record_stage_locked(self, stage: str, camera_id: str, now: float) -> None:
        self._stage_counts.setdefault(stage, Counter())[camera_id] += 1
        self._last_stage.setdefault(stage, {})[camera_id] = now

    def snapshot(self) -> dict[str, object]:
        now = self._monotonic()
        with self._lock:
            ingress_counts = dict(self._ingress_counts)
            inference_counts = dict(self._inference_counts)
            last_ingress = dict(self._last_ingress)
            last_inference = dict(self._last_inference)
            stage_counts = {stage: dict(counts) for stage, counts in self._stage_counts.items()}
            last_stage = {stage: dict(timestamps) for stage, timestamps in self._last_stage.items()}

        cameras: dict[str, object] = {}
        stale_cameras: list[str] = []
        for camera_id in self.camera_ids:
            ingress_age = _age_seconds(now, last_ingress.get(camera_id))
            inference_age = _age_seconds(now, last_inference.get(camera_id))
            camera_stale = (
                now - self._started_at >= self.startup_seconds
                and (inference_age is None or inference_age >= self.stall_seconds)
            )
            if camera_stale:
                stale_cameras.append(camera_id)
            cameras[camera_id] = {
                "ingress_buffers": ingress_counts.get(camera_id, 0),
                "inference_buffers": inference_counts.get(camera_id, 0),
                "ingress_age_seconds": ingress_age,
                "inference_age_seconds": inference_age,
                "stale": camera_stale,
            }

        stages = _stage_snapshots(now, stage_counts, last_stage)
        try:
            queue_levels = self._queue_levels_provider() if self._queue_levels_provider is not None else {}
        except Exception as exc:  # The pipeline may be tearing down while this thread samples.
            queue_levels = {"error": f"{type(exc).__name__}: {exc}"}

        status = "stalled" if stale_cameras else ("running" if last_inference else "starting")
        reason = (
            "no post-inference buffers for required camera(s): " + ",".join(stale_cameras)
            if stale_cameras
            else None
        )
        return {
            "type": "pipeline_heartbeat",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": reason,
            "uptime_seconds": round(now - self._started_at, 3),
            "stale_cameras": stale_cameras,
            "cameras": cameras,
            "stages": stages,
            "queue_levels": queue_levels,
        }

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self._thread = threading.Thread(target=self._monitor, name="pipeline-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))

    def _monitor(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            snapshot = self.snapshot()
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
            marker = "PIPELINE_STALL" if snapshot["status"] == "stalled" else "PIPELINE_HEARTBEAT"
            print(f"{marker} {json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}", flush=True)


def main() -> int:
    args = _parse_args()
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    resources = Path(args.resources).expanduser().resolve(strict=False)
    hef_path = Path(args.hef).expanduser().resolve(strict=False)
    postprocess_so = Path(args.postprocess).expanduser().resolve(strict=False)
    event_path = Path(args.event_path).expanduser().resolve(strict=False)
    cameras = _parse_cameras(args.camera)
    rotations = _parse_rotations(args.rotation)

    sys.path.insert(0, str(workspace))
    stream_id_so = _find_stream_id_so(resources)
    _configure_tappas_postprocess(stream_id_so)
    os.environ["HAILO_ARCH"] = args.arch

    import hailo  # type: ignore[import-not-found]
    from hailo_apps.python.core.gstreamer import gstreamer_app
    from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class
    from hailo_apps.python.pipeline_apps.multisource import multisource_pipeline

    gstreamer_app.GST_VIDEO_SINK = "fakesink"
    original_get_resource_path = multisource_pipeline.get_resource_path

    def explicit_resource_path(pipeline_name, resource_type, arch=None, model=None):
        if model == "libyolo_hailortpp_postprocess.so":
            return postprocess_so
        return original_get_resource_path(pipeline_name, resource_type, arch, model)

    multisource_pipeline.get_resource_path = explicit_resource_path
    event_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_labels = {label.strip().lower() for label in args.allowed_label if label.strip()}
    stream_to_camera = {f"src_{index}": camera_id for index, (camera_id, _url) in enumerate(cameras)}
    write_lock = threading.Lock()
    diagnostics = PipelineDiagnostics(
        tuple(camera_id for camera_id, _url in cameras),
        Path(args.diagnostic_path).expanduser().resolve(strict=False),
        interval_seconds=args.diagnostic_interval,
        stall_seconds=args.stall_timeout,
        startup_seconds=args.startup_timeout,
    )

    def callback(_element, buffer, _user_data):
        if buffer is None:
            return
        roi = hailo.get_roi_from_buffer(buffer)
        stream_id = str(roi.get_stream_id() or "")
        camera_id = stream_to_camera.get(stream_id, stream_id or "unknown")
        diagnostics.record_inference(camera_id)
        events = normalize_hailo_detections(
            roi.get_objects_typed(hailo.HAILO_DETECTION),
            camera_id=camera_id,
            timestamp=datetime.now(timezone.utc),
            min_confidence=args.min_confidence,
        )
        if allowed_labels:
            events = tuple(event for event in events if event.label.lower() in allowed_labels)
        events = tuple(_rotate_event(event, rotations.get(camera_id, 0)) for event in events)
        records = [
            {
                **event.to_dict(),
                "source": "hailo_apps",
            }
            for event in events
        ]
        if records:
            with write_lock, event_path.open("a", encoding="utf-8") as fp:
                for record in records:
                    fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            print(
                f"TowerSightAI Hailo Apps detections [{camera_id}]: "
                + ", ".join(f"{record['label']}:{record['confidence']:.2f}" for record in records),
                flush=True,
            )

    sys.argv = [
        sys.argv[0],
        "--sources",
        ",".join(url for _camera_id, url in cameras),
        "--hef-path",
        str(hef_path),
        "--arch",
        args.arch,
        "--disable-sync",
    ]
    print(
        "TowerSightAI Hailo Apps adapter: "
        f"workspace={workspace} resources={resources} hef={hef_path} "
        f"postprocess={postprocess_so} stream-id-so={stream_id_so} "
        f"cameras={','.join(camera_id for camera_id, _url in cameras)}",
        flush=True,
    )
    class DiagnosticMultisourceApp(multisource_pipeline.GStreamerMultisourceApp):
        def get_pipeline_string(self):
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline_string = super().get_pipeline_string()
            pipeline_string = _use_nonblocking_roundrobin(pipeline_string)
            print(_redact_rtsp_credentials(pipeline_string), flush=True)
            return pipeline_string

        def connect_diagnostic_probes(self) -> None:
            for index, (camera_id, _url) in enumerate(cameras):
                queue = self.pipeline.get_by_name(f"src_q_{index}")
                self._connect_pad_probe(queue, "sink", "source_queue_input", fixed_camera_id=camera_id)
                self._connect_pad_probe(
                    queue,
                    "src",
                    "roundrobin_input",
                    fixed_camera_id=camera_id,
                    legacy_ingress=True,
                )
                source = self.pipeline.get_by_name(f"source_{index}")
                if source is None:
                    print(f"PIPELINE_DIAGNOSTIC_ERROR missing-element name=source_{index}", flush=True)
                else:
                    source.connect("pad-added", self._on_rtsp_pad_added, camera_id)

            for element_name, pad_name, stage in (
                ("robin", "src", "roundrobin_output"),
                ("inference_hailonet", "sink", "hailonet_input"),
                ("inference_hailonet", "src", "hailonet_output"),
                ("inference_hailofilter", "src", "postprocess_output"),
                ("identity_callback", "sink", "callback_input"),
            ):
                self._connect_pad_probe(self.pipeline.get_by_name(element_name), pad_name, stage)

        def _on_rtsp_pad_added(self, _source, pad, camera_id) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            caps_text = caps.to_string().lower() if caps is not None else ""
            if "media=(string)audio" in caps_text or "media=audio" in caps_text:
                return

            def on_buffer(_pad, _info, tracked_camera_id=camera_id):
                diagnostics.record_stage("rtsp_packet", tracked_camera_id)
                return Gst.PadProbeReturn.OK

            pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
            print(f"PIPELINE_DIAGNOSTIC_PROBE stage=rtsp_packet camera={camera_id}", flush=True)

        def _connect_pad_probe(
            self,
            element,
            pad_name: str,
            stage: str,
            *,
            fixed_camera_id: str | None = None,
            legacy_ingress: bool = False,
        ) -> None:
            pad = element.get_static_pad(pad_name) if element is not None else None
            if pad is None:
                element_name = element.get_name() if element is not None else "missing"
                print(
                    f"PIPELINE_DIAGNOSTIC_ERROR missing-pad element={element_name} "
                    f"pad={pad_name} stage={stage}",
                    flush=True,
                )
                return

            def on_buffer(_pad, info):
                camera_id = fixed_camera_id or _camera_id_from_buffer(
                    info.get_buffer(),
                    hailo=hailo,
                    stream_to_camera=stream_to_camera,
                )
                if legacy_ingress:
                    diagnostics.record_ingress(camera_id)
                else:
                    diagnostics.record_stage(stage, camera_id)
                return Gst.PadProbeReturn.OK

            pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
            print(
                f"PIPELINE_DIAGNOSTIC_PROBE stage={stage} "
                f"camera={fixed_camera_id or 'from-buffer'}",
                flush=True,
            )

        def queue_levels(self) -> dict[str, object]:
            per_camera: dict[str, object] = {}
            for index, (camera_id, _url) in enumerate(cameras):
                per_camera[camera_id] = {
                    name: self._queue_level(f"source_{index}_{name}")
                    for name in ("queue_decode", "scale_q", "convert_q")
                }
                per_camera[camera_id]["roundrobin_q"] = self._queue_level(f"src_q_{index}")
            shared = {
                name: self._queue_level(name)
                for name in (
                    "inference_scale_q",
                    "inference_convert_q",
                    "inference_hailonet_q",
                    "inference_hailofilter_q",
                    "inference_output_q",
                    "hailo_tracker_q",
                    "identity_callback_q",
                    "call_q",
                )
            }
            return {"cameras": per_camera, "shared": shared}

        def _queue_level(self, name: str) -> int | None:
            queue = self.pipeline.get_by_name(name) if self.pipeline is not None else None
            if queue is None:
                return None
            return int(queue.get_property("current-level-buffers"))

    from gi.repository import Gst

    user_data = app_callback_class()
    app = DiagnosticMultisourceApp(callback, user_data)
    app.connect_diagnostic_probes()
    diagnostics.set_queue_levels_provider(app.queue_levels)
    if _all_sources_are_files(cameras):
        app.on_eos = app.shutdown
    diagnostics.start()
    try:
        app.run()
    finally:
        diagnostics.stop()
    return 0


def _all_sources_are_files(cameras: tuple[tuple[str, str], ...]) -> bool:
    return bool(cameras) and all(Path(url).expanduser().is_file() for _camera_id, url in cameras)


def _configure_tappas_postprocess(stream_id_so: Path) -> None:
    postprocess_dir = str(stream_id_so.parent)
    os.environ["TAPPAS_POSTPROC_PATH"] = postprocess_dir
    # Hailo Apps 26.03.1 reads the lower-case key from its defines module.
    os.environ["tappas_postproc_path"] = postprocess_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TowerSightAI adapter for the current Hailo Apps multisource API.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--hef", required=True)
    parser.add_argument("--postprocess", required=True)
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--diagnostic-path", required=True)
    parser.add_argument("--diagnostic-interval", type=float, default=2.0)
    parser.add_argument("--stall-timeout", type=float, default=15.0)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--camera", action="append", required=True, help="CAMERA_ID=RTSP_URL")
    parser.add_argument("--rotation", action="append", default=[], help="CAMERA_ID=0|90|180|270")
    parser.add_argument("--allowed-label", action="append", default=[])
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--arch", choices=("hailo8", "hailo8l"), default="hailo8")
    return parser.parse_args()


def _parse_cameras(values: list[str]) -> tuple[tuple[str, str], ...]:
    cameras = []
    for value in values:
        camera_id, separator, url = value.partition("=")
        if not separator or not camera_id.strip() or not url.strip():
            raise ValueError(f"Invalid --camera value: {value!r}")
        cameras.append((camera_id.strip(), url.strip()))
    return tuple(cameras)


def _parse_rotations(values: list[str]) -> dict[str, int]:
    rotations = {}
    for value in values:
        camera_id, separator, rotation_text = value.partition("=")
        if not separator:
            raise ValueError(f"Invalid --rotation value: {value!r}")
        rotation = int(rotation_text)
        if rotation not in {0, 90, 180, 270}:
            raise ValueError(f"Invalid rotation for {camera_id}: {rotation}")
        rotations[camera_id] = rotation
    return rotations


def _rotate_event(event, rotation: int):
    if rotation == 0:
        return event
    from towersightai.inference.events import BoundingBox, DetectionEvent

    box = event.bbox
    if rotation == 90:
        rotated = BoundingBox(x=box.y, y=_unit(1.0 - box.x - box.w), w=box.h, h=box.w)
    elif rotation == 180:
        rotated = BoundingBox(
            x=_unit(1.0 - box.x - box.w),
            y=_unit(1.0 - box.y - box.h),
            w=box.w,
            h=box.h,
        )
    else:
        rotated = BoundingBox(x=_unit(1.0 - box.y - box.h), y=box.x, w=box.h, h=box.w)
    return DetectionEvent(
        camera_id=event.camera_id,
        label=event.label,
        confidence=event.confidence,
        bbox=rotated,
        timestamp=event.timestamp,
        source=event.source,
    )


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _age_seconds(now: float, timestamp: float | None) -> float | None:
    if timestamp is None:
        return None
    return round(max(0.0, now - timestamp), 3)


def _redact_rtsp_credentials(text: str) -> str:
    return re.sub(r"(rtsp://)([^@\s/]+)@", r"\1***:***@", text, flags=re.IGNORECASE)


def _use_nonblocking_roundrobin(pipeline: str) -> str:
    return re.sub(
        r"hailoroundrobin\s+mode=1\s+name=robin",
        "hailoroundrobin mode=2 name=robin queue-size=3 retries-num=2 wait-time=5",
        pipeline,
        count=1,
    )


def _camera_id_from_buffer(buffer, *, hailo, stream_to_camera: dict[str, str]) -> str:
    if buffer is None:
        return "unknown"
    try:
        stream_id = str(hailo.get_roi_from_buffer(buffer).get_stream_id() or "")
    except Exception:
        return "unknown"
    return stream_to_camera.get(stream_id, stream_id or "unknown")


def _stage_snapshots(
    now: float,
    stage_counts: dict[str, dict[str, int]],
    last_stage: dict[str, dict[str, float]],
) -> dict[str, object]:
    snapshots: dict[str, object] = {}
    for stage in sorted(set(stage_counts) | set(last_stage)):
        counts = stage_counts.get(stage, {})
        timestamps = last_stage.get(stage, {})
        cameras = {
            camera_id: {
                "buffers": count,
                "age_seconds": _age_seconds(now, timestamps.get(camera_id)),
            }
            for camera_id, count in sorted(counts.items())
        }
        newest_timestamp = max(timestamps.values()) if timestamps else None
        snapshots[stage] = {
            "buffers": sum(counts.values()),
            "age_seconds": _age_seconds(now, newest_timestamp),
            "cameras": cameras,
        }
    return snapshots


def _find_stream_id_so(resources: Path) -> Path:
    configured_dir = os.environ.get("TAPPAS_POSTPROC_PATH")
    candidates = []
    if configured_dir:
        candidates.append(Path(configured_dir).expanduser() / "libstream_id_tool.so")
    candidates.append(resources / "so" / "libstream_id_tool.so")
    candidates.append(resources / "libstream_id_tool.so")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    matches = tuple(resources.rglob("libstream_id_tool.so")) if resources.is_dir() else ()
    if matches:
        return matches[0].resolve()
    searched = ", ".join(str(path.resolve(strict=False)) for path in candidates)
    raise FileNotFoundError(f"libstream_id_tool.so not found; searched: {searched}")


if __name__ == "__main__":
    raise SystemExit(main())
