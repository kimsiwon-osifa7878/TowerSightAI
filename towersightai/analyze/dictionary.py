"""Korean data dictionary shown in the dashboard (event types, payload fields, derived values)."""

from __future__ import annotations

from typing import Any

EVENT_TYPES: list[dict[str, Any]] = [
    {"name": "detection_batch", "desc": "카메라 한 프레임의 Hailo 감지 결과 목록. 자식 프로세스 최소 신뢰도 0.2 이상만 기록됨(운영자 임계값 적용 전).", "module": "storage/raw_data.py record_detection_batch", "safety": "raw_only",
     "fields": {"task_id": "감지를 낸 추론 작업(process_monitoring=프로세스 감시, person_presence=사람 감지 테스트, vehicle_detection=차량 감지)", "camera_id": "카메라 ID(front/rear_side/opposite_side/ceiling)", "detections[].label": "COCO 라벨(person, car …)", "detections[].confidence": "0~1 신뢰도", "detections[].bbox": "정규화 좌표(x,y,w,h; UI 회전 적용 후)"}},
    {"name": "person_window_started", "desc": "어느 카메라든 person 라벨이 처음 나오면 열리는 raw 사람 창. 엔진 디바운스와 무관하게 신뢰도 0.2 이상 1프레임에 열린다.", "module": "storage/raw_data.py PersonWindowSampler", "safety": "raw_only",
     "fields": {"person_window_id": "창 ID(person_sample과 연결)", "camera_id": "창을 연 카메라"}},
    {"name": "person_sample", "desc": "사람 창 동안 0.5초마다 기록되는 카메라별 상태 + 그 시각의 LD2410 스냅샷. 마지막 감지 후 1초(stale)+5초(유예)까지 계속.", "module": "storage/raw_data.py tick()", "safety": "raw_only",
     "fields": {"sampled_at": "샘플 시각(UTC)", "person_present": "어느 카메라든 1초 안에 사람을 봤는가", "cameras.<id>.person_present": "카메라별 사람 존재", "cameras.<id>.detections": "그 카메라의 마지막 사람 감지 목록", "ld2410.status": "fresh(≤1초)/stale(버퍼 안 오래된 값)/unavailable(없음)", "ld2410.target_status": "0 없음, 1 이동, 2 정지, 3 이동+정지", "ld2410.detection_distance_cm": "레이더가 보고한 거리(cm)", "ld2410.moving_energy/motionless_energy": "이동/정지 에너지(0~100)", "ld2410.*_gate_energy": "거리 게이트(0.75m 단위)별 에너지 배열"}},
    {"name": "person_window_closed", "desc": "사람 창 종료(유예 5초 경과).", "module": "storage/raw_data.py", "safety": "raw_only", "fields": {"person_window_id": "창 ID"}},
    {"name": "ld2410_sample", "desc": "(보강 후) 카메라와 무관하게 1 Hz로 기록되는 레이더 최신 프레임 요약. stale 프레임은 같은 프레임을 반복 기록하지 않음.", "module": "storage/raw_data.py _sample_ld2410", "safety": "raw_only",
     "fields": {"sampled_at": "샘플 시각", "status": "fresh/stale/unavailable", "target_status": "0 없음, 1 이동, 2 정지, 3 이동+정지", "detection_distance_cm": "거리(cm)", "moving_energy": "이동 에너지", "motionless_energy": "정지 에너지", "age_ms": "프레임 나이(ms)"}},
    {"name": "radar_window_started", "desc": "(보강 후) fresh이면서 target_status≠0이 확정 시간(기본 3초) 이상 이어지면 열리는 레이더 감지 창. recorded_at은 첫 감지 시각으로 소급.", "module": "storage/raw_data.py RadarWindowTracker", "safety": "raw_only",
     "fields": {"radar_window_id": "창 ID", "started_at": "첫 감지 시각", "confirm_seconds": "확정에 쓴 시간", "first_target_status": "첫 샘플 상태", "first_detection_distance_cm": "첫 샘플 거리"}},
    {"name": "radar_window_closed", "desc": "(보강 후) 감지 없음/알 수 없음이 해제 시간(기본 5초) 이상 이어지면 닫힘. recorded_at은 마지막 감지 시각.", "module": "storage/raw_data.py RadarWindowTracker", "safety": "raw_only",
     "fields": {"reason": "cleared(레이더가 없음 보고)/radar_unavailable(프레임 끊김)/service_stopped/application_stopped", "duration_seconds": "창 길이", "present_sample_count": "감지 샘플 수", "target_status_counts": "상태별 샘플 수", "max_moving_energy/max_motionless_energy": "최대 에너지", "min/max_detection_distance_cm": "거리 범위"}},
    {"name": "radar_sample", "desc": "(보강 후) 레이더 감지 창이 열려 있는 동안 1초마다, 그 순간 각 카메라가 사람을 보고 있었는지 함께 기록한다. person_sample의 거울상 — 둘을 합치면 어느 쪽이 감지했든 두 센서의 상태를 같은 시각에 비교할 수 있다. 카메라 사람 창이 열려 있으면 person_sample이 이미 양쪽을 담으므로 기록하지 않고, RAW_DATA_RADAR_SAMPLE_SECONDS(기본 60초)까지만 기록한다(정적 클러터로 창이 몇 시간 열릴 수 있음).", "module": "storage/raw_data.py _sample_radar_cameras", "safety": "raw_only",
     "fields": {"radar_window_id": "레이더 창 ID", "sampled_at": "샘플 시각", "camera_person_present": "어느 카메라든 사람을 봤는가", "cameras.<id>.person_present": "카메라별 사람 존재", "cameras.<id>.detections": "그 카메라의 마지막 사람 감지 목록", "ld2410": "같은 시각 레이더 스냅샷"}},
    {"name": "media_artifact_created", "desc": "스냅샷/클립 파일이 저장됨. 파일 내용은 JSONL에 없고 경로·크기·SHA-256만 기록.", "module": "storage/evidence.py", "safety": "raw_only",
     "fields": {"kind": "snapshot/video/plate_image/plate_crop", "metadata.event_kind": "person(창 시작)/person_end(창 종료)/vehicle/radar(레이더 창 시작)/radar_end", "camera_id": "카메라", "relative_path": "날짜 폴더 기준 경로", "related_event_id": "원인 이벤트의 event_id", "captured_at": "촬영 시각"}},
    {"name": "media_capture_failed", "desc": "증거 캡처 실패 또는 의도적 생략(사유 명시).", "module": "storage/evidence.py", "safety": "raw_only",
     "fields": {"reason": "camera_not_healthy, latest_frame_missing_or_stale, recorder_not_ready, no_complete_fragments, person_window_active(카메라 창이 미디어 소유), radar_evidence_throttled(레이더 증거 간격 제한) …"}},
    {"name": "ai_started / ai_stopped", "desc": "추론 자식 프로세스 시작/종료. process_monitoring·person_presence 구간이 카메라 감시 커버리지다.", "module": "storage/raw_data.py", "safety": "raw_only",
     "fields": {"task_id": "작업 ID", "camera_ids": "입력 카메라", "reason": "정지 사유(requested, worker_finished …)", "simulated": "시뮬레이션 여부"}},
    {"name": "ld2410_server_status", "desc": "ESP32 TCP 클라이언트 연결 상태. client_connected~client_disconnected 구간이 레이더 커버리지다.", "module": "sensors/ld2410.py", "safety": "raw_only",
     "fields": {"state": "listening/client_connected/client_disconnected/stopped/error", "details.client_ip": "ESP32 IP", "details.reason": "해제 사유(idle_timeout, peer_closed …)"}},
    {"name": "application_started / application_stopped", "desc": "운영 UI 실행/종료. 종료 시 열린 감시·레이더·창이 모두 닫힌 것으로 본다.", "module": "ui/pyqt_app.py", "safety": "raw_only",
     "fields": {"camera_ids": "활성 카메라", "birdview_mode": "버드뷰 모드", "ld2410_tcp_enabled": "레이더 서버 사용 여부"}},
    {"name": "plate_attempt", "desc": "(보강 후) 차량 진입 중 전면 카메라 1초 주기 번호판 판독 1회. 거부된 판독도 기록한다 — '진입 중 LPR이 실제로 번호판을 본 비율'의 분모다.", "module": "process/engine.py observe_lpr_attempt → storage/raw_data.py", "safety": "raw_only",
     "fields": {"plate_number": "판독된 번호(없으면 빈 문자열)", "confidence": "0~1 신뢰도", "accepted": "다수결 투표에 반영되었는가", "reason": "거부 사유 — above_entry_line(차량진입선 위 = 주차기 밖), no_plate_detected(번호판 미검출), no_plate_bbox, invalid_bbox", "plate_bbox": "전면 프레임 좌표(x1,y1,x2,y2)"}},
    {"name": "vehicle_entered / vehicle_session_ended / plate_recognized", "desc": "차량 세션과 번호판 인식. 사람 에피소드의 문맥(차량 진입 중이었는가)으로만 쓴다.", "module": "storage/raw_data.py", "safety": "raw_only",
     "fields": {"camera_id": "트리거 카메라", "confidence": "차량 신뢰도", "managed": "프로세스 엔진 세션 여부", "plate_number": "번호판(미인식이면 '미인식')", "recognized": "번호판을 읽었는가 — false면 진입은 있었으나 판독 실패", "reads": "다수결에 쓰인 판독 횟수", "reason": "vote(정상 결정) / aborted:*(진입 중단) / 세션 종료 사유", "simulated": "시뮬레이션이면 true(분석에서 제외 권장)"}},
]

DERIVED: list[dict[str, str]] = [
    {"name": "카메라 에피소드", "desc": "detection_batch의 person 프레임을 엔진 규칙으로 재생한 구간: 신뢰도 ≥ min_confidence, 연속 consecutive_frames 프레임(stale_seconds 안), 마지막 감지 후 stale_seconds 지나면 종료, 카메라 간 merge_gap_seconds 이내 병합."},
    {"name": "레이더 에피소드", "desc": "레이더 샘플이 fresh이고 target_status≠0(옵션: 이동만/거리/에너지 게이트)이 radar_confirm_seconds 이상 이어진 구간. 없음/알 수 없음이 radar_clear_seconds 이상이면 종료. 과거 데이터는 person_sample 안의 스냅샷만 있어 '부분 레이더'로 표시된다."},
    {"name": "일치 분류", "desc": "카메라·레이더 에피소드가 ±pair_tolerance_seconds 안에서 겹치면 '둘 다', 아니면 '카메라만'/'레이더만'. 겹친 에피소드들은 하나의 존재 그룹(group_id)이다."},
    {"name": "정밀도", "desc": "그 소스의 라벨된 에피소드 중 '사람 있음' 비율 = 있음 / (있음 + 없음). 판단 불가는 제외."},
    {"name": "상호 재현율", "desc": "어느 소스든 잡고 사람이 '있음'으로 확인한 존재 그룹 중 그 소스가 잡은 비율. 둘 다 놓친 사람은 기록이 없어 절대 재현율은 알 수 없다."},
    {"name": "시간당 오탐", "desc": "'사람 없음' 에피소드 수 ÷ 그 소스의 커버리지 시간. 커버리지가 0이면 표시하지 않는다."},
    {"name": "커버리지", "desc": "카메라: process_monitoring/person_presence 추론이 실행 중이던 시간. 레이더: ESP32 연결 구간 ∪ fresh 샘플 구간. 커버리지 밖의 '감지 없음'은 '사람 없음'이 아니다."},
    {"name": "감지 지연", "desc": "둘 다 감지한 쌍에서 레이더 시작 − 카메라 시작(초). 음수면 레이더가 먼저."},
    {"name": "카메라 대조", "desc": "그 에피소드를 덮는 레이더 창의 radar_sample을 모아 '레이더가 감지한 동안 카메라는 몇 번 사람을 봤는가'를 계산한 값. 동의율 0%면 레이더 단독 감지, 100%면 두 센서가 같은 판단을 한 구간이다."},
    {"name": "에피소드 번호판", "desc": "그 에피소드와 겹치는 차량 세션의 번호판, 없으면 ±60초 안의 plate_recognized 결과. 미인식이면 '미인식'으로 표시하고, 그 뒤의 1초 주기 판독(plate_attempt)을 함께 보여준다."},
    {"name": "에피소드 ID", "desc": "sha1(site|host|source|시작 초)의 앞 16자. 파라미터를 바꿔도 시작 초가 같으면 라벨이 유지된다."},
]


def dictionary_payload() -> dict[str, Any]:
    return {"event_types": EVENT_TYPES, "derived": DERIVED}


__all__ = ["DERIVED", "EVENT_TYPES", "dictionary_payload"]
