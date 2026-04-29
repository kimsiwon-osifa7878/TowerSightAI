# gstreamer multi rtsp detection 테스트 레이턴시 좋음. 


import gi
import os
import argparse

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

rtsp_sources = [
    "rtsp://erumtni:erumtni@192.168.15.235:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.95:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.5:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.75:554/stream1",
]

RTSP_STREAMS = [
    "rtsp://erumtni:erumtni@192.168.15.235:554/stream1"
]

network_width = 640
network_height = 640
network_format = "RGB"
video_sink = "ximagesink"
nms_score_threshold = 0.3
nms_iou_threshold = 0.45
thresholds_str = f"nms-score-threshold={nms_score_threshold} nms-iou-threshold={nms_iou_threshold} output-format-type=HAILO_FORMAT_TYPE_FLOAT32"


def parse_arguments():
    parser = argparse.ArgumentParser(description="Dynamic GStreamer Pipeline")
    parser.add_argument("--python-module", "-py", type=str, default="callback_template.py", help="Python module for callback function")
    parser.add_argument("--show-fps", "-f", action="store_true", help="Print FPS on sink")
    parser.add_argument("--disable-sync", action="store_true", help="Disable sync for video sink")
    return parser.parse_args()


def build_pipeline(rtsp_sources):
    rtsp_sources_str = ''
    streamrouter_input_streams = ''
    compositor_locations = ''
    num_sources = sum(1 for src in rtsp_sources if src)
    
    if num_sources == 0:
        raise ValueError("At least one RTSP source must be provided.")
    
    for idx, src in enumerate(rtsp_sources):
        if not src:
            continue
        
        #pipeline_string += f"src_{idx}::input-streams=\"<sink_{idx}>\" "
        #comp_sinks += f"sink_{idx}::xpos={idx * network_width} sink_{idx}::ypos=0 "
        rtsp_sources_str += f"rtspsrc location={src} latency=30 name=source_{idx} message-forward=true ! "
        rtsp_sources_str += "rtph264depay ! h264parse ! avdec_h264 ! "
        rtsp_sources_str += f"identity name=identity_{idx} single-segment=true ! "
        #rtsp_sources_str += f"identity_{idx}.set_qdata('camera_id', {idx}) "
        rtsp_sources_str += f"queue name=hailo_preprocess_q_{idx} leaky=no max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! "
        rtsp_sources_str += "decodebin ! queue leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! "
        rtsp_sources_str += "videoscale n-threads=8 ! video/x-raw,pixel-aspect-ratio=1/1 ! videoconvert n-threads=8 ! "
        rtsp_sources_str += f"video/x-raw,pixel-aspect-ratio=1/1 ! fun.sink_{idx} sid.src_{idx} ! "
        rtsp_sources_str += f"queue name=comp_q_{idx} leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! comp.sink_{idx} "

        streamrouter_input_streams += f"src_{idx}::input-streams=\"<sink_{idx}>\" "
        
        if idx<2 :
            xpos = network_width*idx
            ypos = 0
        else :
            xpos = network_width*(idx-2)
            ypos = network_height

        compositor_locations +=f"sink_{idx}::xpos={xpos} sink_{idx}::ypos={ypos} "
        


    pipeline_string = "hailoroundrobin mode=0 name=fun ! "
    pipeline_string += "queue name=hailo_pre_infer_q_0 leaky=downstream max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! "
    pipeline_string += f"hailonet hef-path=/home/erumtni/hailotappas/tappas_v3.31.0/apps/h8/gstreamer/resources/hef/yolov5m_wo_spp_60p.hef {thresholds_str} ! "
    pipeline_string += "queue name=hailo_postprocess0 leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0 ! "
    pipeline_string += "hailofilter so-path=/home/erumtni/hailotappas/tappas_v3.31.0/apps/h8/gstreamer/libs/post_processes/libyolo_hailortpp_post.so qos=false ! "
    pipeline_string += "queue name=hailo_python0 max-size-buffers=5 max-size-bytes=0 max-size-time=0 ! "
    pipeline_string += "hailopython qos=false module=callback_template.py ! "
    pipeline_string += "queue name=hailo_draw0 leaky=no max-size-buffers=30 max-size-bytes=0 max-size-time=0 ! "
    pipeline_string += f"hailooverlay ! hailostreamrouter name=sid {streamrouter_input_streams}"

    #compositor_locations = "sink_0::xpos=0 sink_0::ypos=0 sink_1::xpos=640 sink_1::ypos=0 !"
    comp_sinks = f"compositor name=comp start-time-selection=0 {compositor_locations} ! "
    
 
    
    pipeline_string += comp_sinks
    pipeline_string += "videoscale n-threads=8 name=disp_scale ! video/x-raw, width=1280, height=1280 ! videorate ! video/x-raw, framerate=15/1 ! "
    pipeline_string += f"fpsdisplaysink video-sink={video_sink} name=hailo_display sync=false text-overlay=false "
    pipeline_string += rtsp_sources_str
    return pipeline_string


def main():
    args = parse_arguments()
    Gst.init(None)
    
    pipeline_string = build_pipeline(rtsp_sources)

    try:
        pipeline = Gst.parse_launch(pipeline_string)
        print("Pipeline successfully created")
    except Exception as e:
        print("Failed to create pipeline:", e)
        print("Pipeline string:", pipeline_string)
        exit(1)
    
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", lambda bus, msg, loop: loop.quit() if msg.type == Gst.MessageType.ERROR else None, loop)
    
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()
