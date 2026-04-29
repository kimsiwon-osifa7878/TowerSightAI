# gstreamer -> openCV 출력 레이턴시 테스트 용,   
#


import cv2
import gi
import queue
import numpy as np
import os

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib, GObject

# GStreamer 초기화
Gst.init(None)

#  RTSP 카메라 리스트 (4채널)
RTSP_STREAMS = [
    "rtsp://erumtni:erumtni@192.168.15.235:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.95:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.5:554/stream1",
    "rtsp://erumtni:erumtni@192.168.0.75:554/stream1",
]

RTSP_STREAMS = [
    "rtsp://erumtni:erumtni1@192.168.15.235:554/stream1"
]


#  최신 프레임만 유지하는 큐 (콜백 방식)
frame_queues = [queue.Queue(maxsize=1) for _ in range(len(RTSP_STREAMS))]

#  OpenCV 창 설정
for i in range(len(RTSP_STREAMS)):
    cv2.namedWindow(f"Camera {i+1}", cv2.WINDOW_NORMAL)
    cv2.resizeWindow(f"Camera {i+1}", 640, 480)


class RTSPStream:
    def __init__(self, index, rtsp_url):
        self.index = index
        self.rtsp_url = rtsp_url
        self.running = True

        #  GStreamer 파이프라인 (최적화 적용)
        self.pipeline_str = f"""
            rtspsrc location={self.rtsp_url} latency=0 ! queue !
            rtph264depay ! h264parse ! decodebin ! videoconvert ! 
            video/x-raw,format=RGB ! videorate max-rate=15 ! video/x-raw,framerate=15/1 ! 
            appsink name=sink emit-signals=true sync=false max-buffers=5 drop=true
        """

        #  GStreamer 파이프라인 생성
        self.pipeline = Gst.parse_launch(self.pipeline_str)
        self.appsink = self.pipeline.get_by_name("sink")
        self.appsink.set_property("emit-signals", True)
        self.appsink.set_property("max-buffers", 5)
        self.appsink.set_property("drop", True) 

        self.appsink.connect("new-sample", self.on_new_sample, None)

        #  GLib MainLoop 실행 (GStreamer 이벤트 루프)
        self.mainloop = GLib.MainLoop()

    def start(self):
        """GStreamer 이벤트 루프 실행 (메인 루프에서 실행)"""
        self.pipeline.set_state(Gst.State.PLAYING)
        self.mainloop.run()

    def on_new_sample(self, sink, data):
        """콜백 방식으로 프레임을 Python으로 전달"""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        width = caps.get_structure(0).get_int("width")[1]
        height = caps.get_structure(0).get_int("height")[1]

        #  버퍼에서 데이터 가져오기
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR

        #  최신 프레임만 유지 (지연 방지)
        frame = np.frombuffer(map_info.data, dtype=np.uint8).reshape((height, width, 3))
        buffer.unmap(map_info)

        #  OpenCV는 BGR 사용 → 변환 필요
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        #  가장 최신 프레임만 유지
        while not frame_queues[self.index].empty():
            frame_queues[self.index].get_nowait()
        frame_queues[self.index].put(frame)

        return Gst.FlowReturn.OK

    def stop(self):
        """RTSP 스트림 종료"""
        self.running = False
        self.pipeline.set_state(Gst.State.NULL)
        self.mainloop.quit()


#  GStreamer의 GLib 메인 루프 실행 (멀티스레드 제거)
streams = [RTSPStream(i, url) for i, url in enumerate(RTSP_STREAMS)]

#  GStreamer 메인 루프 실행
for stream in streams:
    GObject.idle_add(stream.start)  

#  OpenCV로 프레임 표시 
def display():
    while True:
        updated = False  #  프레임이 업데이트되었는지 확인

        for i in range(len(RTSP_STREAMS)):
            if not frame_queues[i].empty():
                frame = frame_queues[i].get()

                #  디버깅 - 프레임 정보 출력
                print(f"[DEBUG] Camera {i+1} - Frame shape: {frame.shape}, dtype: {frame.dtype}")

                cv2.imshow(f"Camera {i+1}", frame)
                updated = True  #  프레임이 표시되었음을 기록

        if updated:
            key = cv2.waitKey(1)  
            if key == ord("q"):
                break
        else:
            cv2.waitKey(10)  

    cv2.destroyAllWindows()


loop = GLib.MainLoop()
GObject.idle_add(display)  
loop.run()
