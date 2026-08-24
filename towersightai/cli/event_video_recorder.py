from __future__ import annotations

import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    try:
        config = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, TypeError):
        _emit("error", reason="invalid recorder configuration")
        return 2
    camera_id = str(config.get("camera_id") or "")
    rtsp_url = str(config.get("rtsp_url") or "")
    output_dir_text = str(config.get("output_dir") or "")
    output_dir = Path(output_dir_text)
    segment_seconds = float(config.get("segment_seconds") or 2.0)
    if not camera_id or not rtsp_url or not output_dir_text:
        _emit("error", camera_id=camera_id, reason="missing recorder configuration")
        return 2

    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib, Gst
    except (ImportError, ValueError) as exc:
        _emit("error", camera_id=camera_id, reason=f"GStreamer GI unavailable: {type(exc).__name__}")
        return 3

    output_dir.mkdir(parents=True, exist_ok=True)
    Gst.init(None)
    pipeline = Gst.parse_launch(
        "rtspsrc name=source protocols=tcp latency=100 drop-on-latency=true "
        "source. ! application/x-rtp,media=video,encoding-name=H264 ! "
        "rtph264depay ! h264parse config-interval=-1 ! "
        "splitmuxsink name=mux muxer-factory=matroskamux async-finalize=true "
        f"send-keyframe-requests=true max-size-time={int(segment_seconds * 1_000_000_000)} max-size-bytes=0"
    )
    source = pipeline.get_by_name("source")
    mux = pipeline.get_by_name("mux")
    source.set_property("location", rtsp_url)
    mux.set_property("location", str(output_dir / "segment-%08d.mkv"))
    loop = GLib.MainLoop()

    def request_stop(*_args) -> None:
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, message) -> None:
        message_type = message.type
        if message_type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            _emit("error", camera_id=camera_id, reason=_redact(str(error))[:240])
            loop.quit()
            return
        if message_type == Gst.MessageType.EOS:
            loop.quit()
            return
        if message_type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is None or structure.get_name() != "splitmuxsink-fragment-closed":
                return
            location = structure.get_string("location")
            if location:
                _emit(
                    "fragment_closed",
                    camera_id=camera_id,
                    path=location,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                    segment_seconds=segment_seconds,
                )

    bus.connect("message", on_message)
    state_result = pipeline.set_state(Gst.State.PLAYING)
    if state_result == Gst.StateChangeReturn.FAILURE:
        _emit("error", camera_id=camera_id, reason="pipeline failed to enter PLAYING")
        pipeline.set_state(Gst.State.NULL)
        return 4
    _emit("ready", camera_id=camera_id)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
    _emit("stopped", camera_id=camera_id)
    return 0


def _emit(event_type: str, **payload) -> None:
    print(json.dumps({"type": event_type, **payload}, ensure_ascii=False, sort_keys=True), flush=True)


def _redact(message: str) -> str:
    import re

    return re.sub(r"(rtsp://)[^/@\s]+@", r"\1***@", message, flags=re.IGNORECASE)


if __name__ == "__main__":
    raise SystemExit(main())
