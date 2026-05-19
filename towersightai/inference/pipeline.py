from __future__ import annotations

from towersightai.config.settings import Settings
from towersightai.inference.image_smoke import HAILO_CALLBACK_MODULE


def build_multistream_hailo_pipeline(settings: Settings) -> str:
    camera_segments = []
    for index, camera in enumerate(settings.cameras):
        camera_segments.append(
            f"rtspsrc location={camera.rtsp_url} latency=100 ! rtph264depay ! h264parse ! decodebin ! "
            f"videoconvert ! video/x-raw,format=RGB,width=640,height=640 ! queue ! robin.sink_{index}"
        )

    infer_line = (
        "hailoroundrobin name=robin ! "
        f"hailonet hef-path={settings.hailo_hef_path} batch-size=4 ! "
        f"hailofilter function-name={settings.hailo_network_name} so-path={settings.hailo_postprocess_so} qos=false ! "
        f"hailopython module={HAILO_CALLBACK_MODULE} qos=false ! "
        "hailostreamrouter name=router"
    )

    router_outputs = " ".join(
        [f"router.src_{idx} ! queue ! fakesink name=sink_{cam.id}" for idx, cam in enumerate(settings.cameras)]
    )
    return " ".join(camera_segments + [infer_line, router_outputs])
