# 레이더(LD2410) raw 기록 보강 — 작업 명세

작성일 2026-09-10. 상위 계획: `docs/implementation/analyze-dashboard.md` Phase 0 중 **레이더 관련 항목만**
떼어낸 독립 작업 명세. 이 문서만 읽고 구현할 수 있도록 현재 코드 위치·목표 스키마·테스트·문서 갱신
범위를 모두 담았다.

**레이더의 지위(2026-09-16 확정)**: LD2410은 **검증 전용**이다. 카메라 감지와의 정확도 비교를 위해 기록만 하고,
프로세스 엔진·사용자 모드·안전 게이트·주차기 작동에는 일절 사용하지 않는다. 엔진의 `observe_radar`는 제거됐다.

**범위 밖(이 작업에서 하지 않음)**: 통제(주기) 스냅샷, 엔진 판정(`engine_person_state`) 이벤트.
NAS 호스트별 원격 경로(`raw/<source_host>/<day>`)는 2026-09-10 대시보드 작업에서 `storage/archive.py`
`remote_raw_day_dir`로 이미 적용됨 — 건드리지 말 것. 엔진의 레이더 처리는 2026-09-16에 **완전히 제거**됐다(검증 전용).

---

## 1. 왜 필요한가 (현재 동작)

LD2410 값이 raw 데이터에 남는 경로는 하나뿐이다.

- `towersightai/storage/raw_data.py` `RawDataManager.tick()` (359행)이 **카메라 사람 창이 열려 있는 동안만**
  0.5초 `person_sample`을 만들고, 그 안에 `ld2410_snapshot_provider(sample_time)`의 결과를 `ld2410` 키로 붙인다.
- 제공자는 `towersightai/sensors/ld2410.py` `LD2410TCPService.snapshot_at()` (299행) →
  `LD2410ReadingBuffer.snapshot_at()` (221행): 30초 링 버퍼(`LD2410_BUFFER_SECONDS`)에서 샘플 시각 이전의
  최신 프레임을 골라 `status` = `fresh`(≤1초) / `stale` / `unavailable` 로 돌려준다.
- 사람 창 밖에서는 제공자를 호출하지 않는다(테스트 `test_ld2410_provider_is_not_called_outside_person_window`가
  이를 고정). 프레임은 링 버퍼에서 30초 뒤 사라진다.
- UI: `towersightai/ui/pyqt_app.py` `_start_raw_data_collection()` (1325행)이 서비스와 매니저를 만들고
  제공자를 연결하며, `_tick()` (2569행, 1 Hz)이 `_sample_raw_person_window()` → `RawDataManager.tick()`을 호출한다.
  프레임 콜백 `_append_ld2410_frame()`은 엔진 `observe_radar`와 콘솔 표시만 한다.
- 증거: `towersightai/storage/evidence.py` `EvidenceCoordinator.handle_raw_event()` (111행)는
  `vehicle_entered`, `vehicle_session_ended`, `person_window_started/closed`, `plate_recognized`에만 반응한다.

결과: **레이더만 감지한 시간은 기록도, 스냅샷도 없다.** 현장 09-03 데이터로 확인했을 때 카메라 clear 구간에서
레이더는 `Motionless`를 매우 자주 보고하는데(827/2592 샘플), 그것이 사람인지 정적 클러터인지 확인할 방법이 없다.
레이더 오탐률·카메라 미탐률을 계산하려면 아래 세 가지가 필요하다.

---

## 2. 목표 동작

### 2.1 `ld2410_sample` — 레이더 상시 샘플 (신규 이벤트)

- 카메라 사람 창과 무관하게 `RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS`(기본 `1.0`, `0` = 끔) 간격으로 기록.
- `RawDataManager.tick(now)` 안에서 `PersonWindowSampler`와 같은 "due samples" 방식으로 생성한다
  (마지막 샘플 시각부터 `now`까지 간격마다 `snapshot_at(sample_time)` 호출 — 링 버퍼가 30초이므로 tick이
  몇 초 늦어도 과거 시각을 정확히 조회할 수 있다). 첫 샘플 시각은 매니저 생성 시각(또는 제공자 연결 시각)으로 정렬.
- 기록 규칙(중복 억제):
  - `status == "fresh"` → 항상 기록.
  - `status == "stale"` → 직전에 기록한 샘플과 `received_at`이 다를 때만 기록(같은 프레임을 30초간 반복 기록하지 않음).
  - `status == "unavailable"` 또는 제공자 없음(`LD2410_TCP_ENABLED=false`) → 기록하지 않음.
    (연결 커버리지는 기존 `ld2410_server_status`의 `client_connected/disconnected`로 계산한다.)
  - 제공자 예외 → 기존 `person_sample`과 같이 `status: "unavailable", reason: "provider_error"`를 **한 번** 기록하고
    로그(`exception`)를 남긴다. 기록 실패가 UI를 죽이면 안 된다.
- payload (모두 제공자 반환값 그대로, `raw_hex`는 **제외**해 용량을 줄인다 — `person_sample`은 기존대로 유지):

```json
{
  "sampled_at": "2026-09-10T06:40:40.000000+00:00",
  "status": "fresh",
  "source": "ld2410_tcp",
  "received_at": "2026-09-10T06:40:39.874000+00:00",
  "age_ms": 126,
  "client_ip": "192.168.0.50",
  "data_type": 1,
  "target_status": 2,
  "target_status_text": "Motionless",
  "moving_distance_cm": 0,
  "moving_energy": 0,
  "motionless_distance_cm": 226,
  "motionless_energy": 63,
  "detection_distance_cm": 226,
  "max_moving_gate": 8,
  "max_motionless_gate": 8,
  "moving_gate_energy": [0, 0, 0, 0, 0, 0, 0, 0, 0],
  "motionless_gate_energy": [0, 0, 0, 63, 12, 0, 0, 0, 0],
  "light": 120,
  "out_pin": 1,
  "safety_effect": "raw_only"
}
```

- `_DURABLE_EVENTS`에 넣지 **않는다**(1 Hz라 fsync 비용이 크다). 용량 추정: 1 Hz × 86,400 × ≈500 B ≈ 43 MB/일,
  gzip 후 ≈ 4~6 MB/일.

### 2.2 `radar_window_started` / `radar_window_closed` — 레이더 감지 창 (신규 이벤트)

같은 tick 안에서 2.1의 샘플로 구동되는 `RadarWindowTracker`(신규 클래스, `raw_data.py`)가 만든다.

- "present" = `status == "fresh"` **이고** `target_status != 0`. `stale`/`unavailable`은 present도 absent도 아닌
  **unknown**으로 다룬다(레이더가 사람을 못 봤다고 단정하지 않는다).
- **열림**: present가 `RAW_DATA_RADAR_WINDOW_MIN_SECONDS`(기본 `3.0`) 이상 연속되면 `radar_window_started`를
  기록한다. `recorded_at`은 **첫 present 샘플 시각**(소급). payload:
  `{"radar_window_id": uuid hex, "started_at": iso, "confirm_seconds": 3.0, "first_target_status": 2,
  "first_detection_distance_cm": 226, "safety_effect": "raw_only"}`
- **닫힘**: absent(`fresh` + `target_status == 0`) 또는 unknown이 `RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS`
  (기본 `5.0`) 이상 이어지면 `radar_window_closed`. `recorded_at`은 **마지막 present 샘플 시각**. payload:
  `{"radar_window_id", "started_at", "ended_at", "duration_seconds", "reason": "cleared" | "radar_unavailable"
  | "service_stopped" | "application_stopped", "sample_count", "present_sample_count",
  "target_status_counts": {"1": n, "2": n, "3": n}, "max_moving_energy", "max_motionless_energy",
  "min_detection_distance_cm", "max_detection_distance_cm", "safety_effect": "raw_only"}`
- 닫힘 후 5초 안에 다시 present가 시작되면 **새 창**이다(병합은 분석 쪽에서 한다). 즉 트래커는 단순하게:
  `idle → confirming(streak) → open → closing(gap)`.
- 매니저 `close()` (499행)와 LD2410 서비스 `stopped` 상태 콜백 시 열린 창을 `application_stopped` /
  `service_stopped` 사유로 닫는다(제공자가 없어져도 창이 영원히 열려 있으면 안 된다).
- 두 이벤트는 `_DURABLE_EVENTS`에 **추가**한다(창 경계는 증거 연결 키라 유실되면 안 된다).
- 2.1이 꺼져 있으면(`RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS=0`) 트래커도 동작하지 않는다(샘플이 입력이므로).

### 2.3 레이더 창 증거 미디어 (`evidence.py`)

`handle_raw_event()`에 두 분기를 추가한다. 기존 `simulated` 무시 규칙은 그대로 적용된다.

- `radar_window_started` →
  1. `RAW_MEDIA_RADAR_EVIDENCE`가 `false`면 아무것도 하지 않는다.
  2. 카메라 사람 세션(`_person_session_id`)이 이미 열려 있으면 스냅샷·클립을 **찍지 않고** 모든 정상 카메라에
     대해 `media_capture_failed(kind="snapshot", reason="person_window_active")`를 남긴다(카메라 창에 이미 미디어가
     있고, 분석은 이 사유로 "미디어는 카메라 창 쪽에 있음"을 안다).
  3. 직전 레이더 증거 번들로부터 `RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS`(기본 `60`) 미만이면 찍지 않고
     `media_capture_failed(reason="radar_evidence_throttled")`를 남긴다.
  4. 그 외: 정상 카메라 전부에 `radar` 종류 스냅샷(`_capture_snapshots(event_id, "radar", …)`,
     파일명 `HHMMSS-ffffff-radar-<camera>.jpg`) + `radar` 종류 클립 세션을 연다.
     클립 `close_at = started + RAW_MEDIA_RADAR_CLIP_MAX_SECONDS`(기본 `30`) — 레이더 창은 정적 클러터로 몇 시간
     이어질 수 있어 **상한이 반드시 필요**하다. 세션 ID를 `_radar_session_id`로 보관.
- `radar_window_closed` → `_radar_session_id`가 있으면 `radar_end` 스냅샷(정상 카메라)을 찍고 세션을
  `min(close_at, event_at)`으로 닫는다. 창이 30초보다 길면 클립은 이미 30초에서 닫혔고 `radar_end` 스냅샷만 추가된다.
- `media_artifact_created.metadata.event_kind`는 `radar` / `radar_end`, `related_event_id`는 각각
  `radar_window_started` / `radar_window_closed`의 `event_id` (기존 person 규칙과 동일).
- 용량 상한(최악): 60초 제한 → 시간당 60번들 × (카메라 3대 × 스냅샷 2장 ≈ 1.2 MB + 30초 클립 3개 ≈ 3 MB) ≈ 250 MB/시간.
  실제로는 창이 이어지는 동안 새 창이 열리지 않으므로 훨씬 작다. 현장 대역폭이 걱정되면 `.env`에서 간격을 늘린다.

### 2.4 설정 (`config/settings.py` `RawStorageConfig`, `config/env_loader.py`, `.env.example`)

| 키 | 필드 | 기본 | 검증 |
|---|---|---|---|
| `RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS` | `ld2410_sample_interval_seconds: float` | `1.0` | `>= 0` (`0` = 끔) |
| `RAW_DATA_RADAR_WINDOW_MIN_SECONDS` | `radar_window_min_seconds: float` | `3.0` | `> 0` |
| `RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS` | `radar_window_clear_seconds: float` | `5.0` | `> 0` |
| `RAW_MEDIA_RADAR_EVIDENCE` | `media_radar_evidence: bool` | `true` | — (media_enabled=false면 무의미) |
| `RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS` | `media_radar_min_interval_seconds: float` | `60.0` | `> 0` |
| `RAW_MEDIA_RADAR_CLIP_MAX_SECONDS` | `media_radar_clip_max_seconds: float` | `30.0` | `>= media_segment_seconds` |

`.env.example`에는 각 키에 한 줄 주석(“레이더 상시 샘플 — 분석 전용, 안전 게이트 영향 없음”). 현장 `.env`
안내: 기본값으로 켜진다.

### 2.5 UI (`ui/pyqt_app.py`) — 최소 변경

- `RawDataManager.tick()`이 이미 1 Hz로 호출되므로 샘플링·창 추적을 위한 호출 추가는 없다.
- `_record_ld2410_status()`에서 `state == "stopped"`일 때 매니저에 `close_radar_window(reason="service_stopped")`
  (신규 메서드)를 호출한다. `client_disconnected`는 닫지 않는다(unknown → 5초 후 `radar_unavailable`로 자연 종료).
- `레이더 (LD2410)` 페이지 상태줄에 “raw 기록: 1 Hz 샘플 + 감지 창” 한 줄 추가는 **선택**(테스트가 고정하는
  기존 라벨은 바꾸지 않는다).

### 2.6 스키마

`schema_version`은 **2 유지**(추가 이벤트만, 기존 레코드 형식 불변). `person_sample.ld2410` 형식도 불변.
분석 쪽은 이 이벤트가 없는 과거 날짜를 “레이더 부분 기록”으로 처리한다.

---

## 3. 코드 변경 목록

| 파일 | 변경 |
|---|---|
| `towersightai/config/settings.py` | `RawStorageConfig`에 2.4의 6개 필드 + `__post_init__` 검증 |
| `towersightai/config/env_loader.py` | 6개 키 파싱(167~211행 블록에 추가) |
| `towersightai/storage/raw_data.py` | `RadarWindowTracker`(순수, 시계 주입) · `RawDataManager.tick()`에 ld2410 샘플 생성 + 트래커 구동 · `close_radar_window(reason)` · `close()`에서 창 닫기 · `_DURABLE_EVENTS`에 `radar_window_started/closed` 추가 · `record_ld2410_status("stopped")` 시 창 닫기 |
| `towersightai/storage/evidence.py` | `radar_window_started/closed` 분기, `_radar_session_id`, `_last_radar_evidence_at`, 스로틀·사람창 활성 사유 |
| `towersightai/ui/pyqt_app.py` | `_record_ld2410_status`에서 `stopped` → 창 닫기 |
| `.env.example` | 6개 키 + 주석 |
| `README.md` §5 원격 아카이브(NAS) | 새 이벤트·미디어 종류·설정 키 설명 |
| `CLAUDE.md` §8 | 이벤트 목록에 `ld2410_sample`, `radar_window_*`, `radar`/`radar_end` 미디어 추가; LD2410은 검증 전용(엔진 입력 아님) 문장으로 갱신; 테스트 수 갱신 |
| `docs/implementation/testing-strategy.md` | 단위 테스트 목록에 레이더 샘플·창·증거 항목 추가 |
| `INTENT.md` §4 | 결정 기록 한 줄: “레이더 상시 raw 기록(1 Hz)+감지 창+증거는 분석 전용, 엔진 입력 아님” |

---

## 4. 테스트 (모두 하드웨어 없이, `pytest -q`)

`tests/test_raw_data.py`
1. 사람 창 밖에서도 1 Hz `ld2410_sample`이 기록된다(제공자 `fresh` 반환, 5초 tick → 5개; `sampled_at` 정렬 확인).
2. `stale`은 같은 `received_at`이면 한 번만 기록된다; `unavailable`은 기록되지 않는다.
3. 간격 `0`이면 샘플·창 모두 기록되지 않고 제공자도 호출되지 않는다.
4. 제공자 예외 → `status: unavailable, reason: provider_error` 1회 + 이후 정상 복귀.
5. 기존 `test_ld2410_provider_is_not_called_outside_person_window`는 **간격 0으로** 바꿔 의미를 유지하거나,
   “사람 창 밖 호출은 ld2410_sample 용도뿐”으로 이름·단언을 갱신한다.
6. `person_sample.ld2410`은 변함없이 `raw_hex`를 포함한다(회귀).

`tests/test_raw_data.py` (또는 새 `tests/test_radar_window.py`) — `RadarWindowTracker`
7. present 2초 → 창 없음; 3초 → `radar_window_started`, `recorded_at` = 첫 present 시각(소급).
8. absent 4초 → 아직 열림; 5초 → `radar_window_closed`, `recorded_at` = 마지막 present, `duration_seconds`·
   `target_status_counts`·에너지/거리 집계 정확.
9. `stale`/`unavailable`은 present 스트릭을 끊지만 absent로 세지 않고, 5초 이상 이어지면 `reason="radar_unavailable"`.
10. 닫힘 뒤 재감지는 새 `radar_window_id`.
11. `close()`/`close_radar_window("service_stopped")`가 열린 창을 해당 사유로 닫고, 닫힌 뒤엔 아무것도 기록하지 않는다.
12. `radar_window_started/closed`는 durable로 append된다(기존 durable 테스트 방식 재사용).

`tests/test_evidence.py`
13. `radar_window_started` → 정상 카메라마다 `radar` 스냅샷 + `radar` 클립 세션(`close_at` = 시작+30초 상한).
14. `radar_window_closed` → `radar_end` 스냅샷, 세션 종료; 30초를 넘긴 창은 클립이 상한에서 닫힌다.
15. 사람 세션 활성 중 → 캡처 없음 + `media_capture_failed(reason="person_window_active")`.
16. 60초 스로틀 → 두 번째 창은 `radar_evidence_throttled`, 61초 뒤 세 번째 창은 캡처.
17. `RAW_MEDIA_RADAR_EVIDENCE=false` → 아무 동작 없음; `simulated: true` → 무시(기존 규칙).

`tests/test_config.py` / `tests/test_env_loader.py`
18. 새 키 기본값·파싱·검증(음수, 클립 상한 < 세그먼트).

`tests/test_pyqt_app.py`
19. LD2410 `stopped` 상태가 매니저의 `close_radar_window("service_stopped")`를 호출한다(가짜 매니저).
20. 이 작업으로 `can_show_final_ok`가 바뀌지 않는다(기존 안전 테스트 패턴 1개 추가).

CLAUDE.md의 “현재 스위트 수(324)”를 최종 결과로 갱신한다.

---

## 5. 안전 규칙 (반드시 지킬 것)

- 새 이벤트와 미디어는 **분석 전용**이다. 엔진 `observe_radar`, 상태기, `can_show_final_ok`, PLC 경로에 아무것도
  연결하지 않는다. 레이더는 검증 전용이며 엔진·사용자 모드·안전 게이트에 입력되지 않는다(2026-09-16 확정).
- 기록·캡처 실패는 로그와 `media_capture_failed`로 드러내되 UI를 멈추거나 안전 표시를 바꾸지 않는다.
- `simulated: true` 이벤트는 증거를 만들지 않는다(기존 규칙 유지).
- 자격 증명·IP는 기존 redaction 규칙을 따른다(`client_ip`는 사설 IP라 기존 `person_sample`과 같이 기록 허용).

## 6. 완료 기준

1. `pytest -q` 전부 통과, 하드웨어 불필요.
2. 개발기에서 UI를 띄우고 ESP32 없이도 오류 없이 동작(샘플·창 기록 없음, `ld2410_server_status: listening`만).
3. 현장기 배포 후 하루 수집: NAS 날짜 폴더의 샤드에 `ld2410_sample`이 1 Hz로 있고, `radar_window_started/closed`
   쌍과 `media/images/*-radar-*.jpg`, `*-radar_end-*.jpg`, `media/videos/*-radar-*-part001.mkv`가 존재한다.
   확인 명령 예:

```bash
zcat artifacts/raw/2026-09-1X/events-*.jsonl.gz | python3 -c "import sys,json,collections; c=collections.Counter(json.loads(l)['event_type'] for l in sys.stdin); print(c)"
```

---

## 7. 추가 구현 (2026-09-16, 사용자 지시)

목적은 **카메라 감지와 레이더 감지를 나중에 데이터로 비교분석**하는 것이다. 위 §2까지만으로는 레이더 창 동안
카메라가 무엇을 보고 있었는지 기록이 없어 비교가 불가능했다(카메라 창에는 `person_sample`이 레이더 값을 담는데
레이더 창에는 대칭 기록이 없었다). 그래서 다음을 추가했다.

- **`radar_sample` 이벤트** (`storage/raw_data.py` `_sample_radar_cameras`): 레이더 감지 창이 열려 있는 동안
  1초마다 `{radar_window_id, sampled_at, camera_person_present, cameras{<id>: {person_present,
  last_person_detected_at, detections}}, ld2410{...}, safety_effect: raw_only}`를 기록한다. 카메라 사람 창이
  활성이면 기록하지 않는다(그쪽 `person_sample`이 이미 양쪽을 담는다). durable 아님.
- **상한**: `RAW_DATA_RADAR_SAMPLE_SECONDS`(기본 60, 0=끔). 레이더 창은 정적 클러터로 몇 시간 열릴 수 있으므로
  창 시작으로부터 이 시간까지만 기록한다.
- **`PersonWindowSampler.camera_state(at)`**: 창과 무관하게 카메라별 사람 상태를 만드는 공용 메서드. 이를 위해
  최근 감지(`_latest`/`_latest_at`)는 창이 닫혀도 유지한다(신선도 규칙이 존재 판정을 담당하므로 안전).
- **대시보드**: 다이제스트 v3가 `radar_sample`을 레이더 창의 `samples`로 모아 `camera_samples` /
  `camera_present_samples` / `camera_agreement`를 계산하고, 에피소드에 `camera_check`로 붙인다. 목록에
  `카메라 대조` 열(동의 초/전체 초), 검토 화면에 `레이더 감지 중 카메라 상태` 표가 나온다.

이로써 레이더가 몇 초 이상 사람을 감지하면 ① 창 이벤트 ② 전 카메라 스냅샷과 클립 ③ 1초 단위 카메라 상태가
함께 남아 NAS로 올라간다.


---

## 8. 현장 1차 검증에서 나온 수정 (2026-09-16)

현장기 적용 후 NAS 데이터(09-16 00:13~11:13, 35,538건)로 확인한 결과 두 결함이 드러나 고쳤다.

- **레이더 스냅샷이 한 장도 저장되지 않음** (`latest_frame_missing_or_stale` 70건). 레이더 창의 시각은
  소급(시작=첫 감지 샘플, 종료=마지막 감지 샘플)인데 스냅샷 신선도 검사는 `abs(captured_at - frame.received_at)
  > 1초`라, 살아 있는 최신 프레임이 오히려 "1초보다 어긋난" 것으로 걸렸다. 스냅샷은 지금 프레임이므로
  **현재 시계로 스탬프**하도록 수정(클립은 프리롤이 실제 시작을 덮으므로 소급 시각 유지).
- **카메라 대조가 항상 0%**. `radar_sample`을 카메라 사람 창이 열려 있으면 건너뛰었는데, 그 구간이 바로
  두 센서가 일치하는 구간이다. 실제로 18개 창 중 16개가 카메라 사람 창과 겹쳤는데도 대조값은 전부 0이었다.
  → **레이더 창 동안에는 항상 기록**하도록 수정. `person_sample`과 일부 중복되지만 창이 자기완결적이 된다.
