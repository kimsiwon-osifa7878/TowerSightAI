# Local Hailo Resources

This directory is the deployment-local home for TowerSightAI Hailo models, JSON configurations, and matching postprocess libraries.

Expected layout:

```text
models/hailo/
├── general/
│   └── yolov5m_wo_spp_60p.hef
├── vehicle_detection/
│   ├── yolov5m_vehicles.hef
│   └── configs/
│       └── yolov5_vehicle_detection.json
├── person_presence/
│   ├── yolov5s_personface_reid.hef
│   └── configs/
│       └── yolov5_personface.json
└── postprocess/
    ├── libyolo_hailortpp_post.so
    ├── libyolo_post.so
    └── cropping_algorithms/
        └── libwhole_buffer.so
```

The resource binaries are intentionally ignored by Git. Copy them from a matching Hailo-8/TAPPAS 3.31.0 installation as documented in the repository `README.md`. Do not mix HEFs, JSON configurations, or shared libraries from different TAPPAS/HailoRT releases.
