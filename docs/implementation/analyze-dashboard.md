# 데이터 분석 대시보드(`towersightai/analyze`) 구현 계획

작성일 2026-09-10. 목적: NAS에 쌓인 raw 데이터로 **레이더(LD2410) 사람 감지와 카메라(Hailo) 사람 감지의
정확도를 비교**하고, 사람이 스냅샷·영상을 보고 정답을 표시해 실제 성공률을 산출하는 분석 대시보드.
분석은 **읽기 전용·오프라인**이며 안전 게이트, 엔진, PLC와 무관하다(§7).

---

## 0. 결론 요약

1. NAS raw 데이터는 카메라 사람 감지를 비교·검증하기에 충분하다(0.5초 샘플, 시작/종료 스냅샷, 5초
   프리롤 영상이 모든 사람 창에 붙어 있음).
2. **레이더는 지금 구조로는 비교가 불가능하다.** LD2410 값은 카메라가 사람을 본 동안(`person_sample`)에만
   기록되고, 레이더만 감지한 시간은 아무 기록도 남지 않는다 → 레이더 오탐/카메라 미탐을 셀 수 없다.
   대시보드보다 **raw 기록 보강(Phase 0)** 이 먼저다.
3. 개발기와 현장기가 NAS의 **같은 날짜 폴더에 같은 파일명으로 덮어쓰고 있다**(§1.4). 분석 전에 호스트별
   폴더로 분리해야 한다.
4. 대시보드는 **독립 실행형 로컬 웹 앱**(`towersightai-analyze serve` → 브라우저)으로 만든다. 운영 콘솔(PyQt)에는
   실행 링크만 둔다. 이유는 §4.1.
5. 정답은 사람이 **에피소드 단위**로 표시한다(사람 있음/없음/판단 불가). 지표는 소스별 정밀도, 상호 대비
   재현율, 감지 지연, 시간당 오탐, 임계값 스윕(§2.4).

---

## 1. 지금 NAS에 있는 데이터 (2026-09-10 조사)

### 1.1 구조

```text
${SYNOLOGY_NAS_FOLDER}/raw/YYYY-MM-DD/
├── manifest.json                       # schema v2, source_host, 파일별 SHA-256/size
├── events-YYYYMMDD-HHMM[-NN].jsonl.gz  # 시간 샤드(재시작마다 -01, -02 …)
└── media/
    ├── images/HHMMSS-ffffff-{person|person_end|vehicle|plate|plate-crop}-<camera>.jpg
    └── videos/HHMMSS-ffffff-{person|vehicle}-<camera>-partNNN.mkv   # H.264 passthrough, 무음
```

NAS 현황: 2026-08-21 ~ 09-09, 15일. 현장기(`pakrio-shinantower`) 실데이터가 있는 날은 **09-03**
(129 샤드, 이미지 241, 영상 155, 398 MB)과 08-28~30, 09-04~05(사실상 재시작 로그만). 나머지는 개발기.
09-09는 manifest 없이 부분 업로드 상태.

### 1.2 분석에 쓰는 레코드 (schema v2, `towersightai/storage/raw_data.py`)

| event_type | 의미 | 분석 용도 |
|---|---|---|
| `detection_batch` | 카메라 1프레임의 Hailo 감지 목록(`label`, `confidence`, 정규화 `bbox`, `camera_id`, `task_id`). 자식 최소 신뢰도 **0.2** | 카메라 에피소드 재구성, 임계값 스윕 |
| `person_window_started/closed` | 어떤 카메라든 person 라벨이 나오면 열리고, 1초 stale + 5초 유예 후 닫힘. `person_window_id` | 카메라 에피소드의 기본 단위, 미디어 연결 키 |
| `person_sample` | 사람 창 동안 0.5초마다: 카메라별 `person_present`/감지 목록 + **`ld2410` 스냅샷**(status fresh/stale/unavailable, `target_status`, 거리, 에너지, 게이트 배열) | 창 내부 타임라인, 레이더 동시 비교 |
| `media_artifact_created` | `related_event_id` = `person_window_started`(시작 스냅샷+영상) 또는 `person_window_closed`(`person_end` 스냅샷)의 `event_id` | 에피소드 ↔ 스냅샷/영상 매핑 |
| `ai_started/stopped` | `process_monitoring` 등 추론 자식 기동/정지 | **커버리지**(감시가 살아 있던 시간) |
| `ld2410_server_status` | ESP32 연결/해제 | 레이더 커버리지 |
| `application_started/stopped`, `vehicle_entered`, `vehicle_session_ended`, `plate_recognized` | 세션·차량 문맥 | 필터/문맥 표시 |

레코드 공통 필드: `event_id`, `event_type`, `recorded_at`(UTC ISO), `application_session_id`,
`vehicle_session_id`, `payload`. 시간대는 `RAW_DATA_TIMEZONE=Asia/Seoul`로 표시한다.

### 1.3 현장 09-03 미리보기 (가장 큰 샤드 3개, 08~10시) — 방법 검증용, 결론 아님

| 항목 | 값 |
|---|---|
| 카메라 사람 창 | 37개, **전부 `opposite_side`**(문이 열리면 바깥이 보이는 카메라) |
| 창 길이 | 0.5초 짜리 블립 19개(최대 신뢰도 0.3~0.5), 60초 이상 9개 |
| 샘플 단위 카메라·레이더 일치 | 둘 다 감지 169 / 카메라만 278 / 레이더만(창 꼬리 5초) 1006 / 둘 다 없음 1586 |
| 레이더 상태 분포(카메라 clear 구간) | Motionless 827, Moving+Motionless 174, No target 1586 |
| 레이더 거리 | 102~436 cm, 중앙값 226 cm |
| 미디어 연결 | 37/37 창에 시작 스냅샷+영상+종료 스냅샷 |

시사점: (a) raw의 "사람 창"은 신뢰도 0.2 기준이라 엔진 판정보다 훨씬 잡음이 많다 → 대시보드는 엔진과
같은 규칙(카메라 역할, 디바운스, 임계값)으로 **오프라인 재현한 에피소드**를 기본으로 보여줘야 한다.
(b) 레이더는 카메라가 못 보는 동안 Motionless를 자주 보고한다 → 정적 클러터(벽·기둥·주차 차량)인지
실제 사람인지는 정답 표시 없이는 알 수 없다. 그래서 스냅샷 확인 기능이 핵심이다. (c) 엔진은 IDLE에서
`opposite_side`를 사람 감시에서 제외하므로 "엔진 관점" 필터가 있어야 공정하다.

### 1.4 분석을 막는 문제 (Phase 0에서 해결)

| # | 문제 | 근거 | 해결 |
|---|---|---|---|
| P1 | **레이더 단독 감지가 기록되지 않음.** LD2410 프레임은 `person_sample`(카메라 사람 창) 안에서만 저장. 창 밖의 레이더 값은 30초 링 버퍼에서 사라짐 | `raw_data.py` `tick()`, `test_ld2410_provider_is_not_called_outside_person_window` | 새 이벤트 `ld2410_sample`(1 Hz 다운샘플, 항상) + `radar_window_started/closed`(target_status≠0 지속) 기록 |
| P2 | **레이더 단독 구간에 스냅샷/영상이 없음** → 사람이 확인할 수 없음 | `evidence.py`는 person_window/vehicle/plate에만 반응 | `radar_window_started`(≥3초 지속, 30초당 1회 제한)에 `radar` 종류 스냅샷+클립 캡처 |
| P3 | **호스트 충돌.** 개발기·현장기가 `raw/YYYY-MM-DD/`에 같은 샤드명(`events-20260828-1700.jsonl.gz`)으로 업로드 → 나중 업로드가 덮어쓰고 manifest도 한쪽만 남음 | NAS 08-28 manifest는 현장기(샤드 2개), 로컬 개발기 08-28은 샤드 5개 + 업로드 마커 | 원격 경로를 `raw/<source_host>/YYYY-MM-DD/`로 변경(기존 폴더는 그대로 두고 대시보드가 둘 다 읽음, manifest의 `source_host`로 구분) |
| P4 | **엔진의 최종 사람 판정이 raw에 없음.** `person_possible`(카메라 디바운스 ∨ 레이더)이 언제 참이었는지 기록되지 않아 "실제 경고가 나갔는가"를 알 수 없음 | `engine.py` `_person_possible()` | `engine_person_state` 이벤트(상태 변화 시: possible/clear, 원인 cameras/radar, phase) |
| P5 | 09-04처럼 감시 자식이 시간당 340회 재시작한 날은 "감지 없음"이 "사람 없음"이 아님 | 09-04 샤드 | 커버리지 계산을 대시보드 1급 지표로(§3.1) |
| P6 | 사람 감시에는 **신뢰도 임계값이 없다**(디바운스 2프레임만). 자식 0.2 그대로 | `engine.observe_detections`, `PersonDebounceSettings` | 대시보드 스윕 결과로 `person_debounce.min_confidence` 운영자 설정 추가 여부 결정(별도 작업) |

---

## 2. 비교 방법론

### 2.1 에피소드(episode) — 비교의 단위

프레임/샘플 단위 비교는 두 센서의 주기·시야가 달라 공정하지 않다. 사람이 확인할 수 있는 단위는
"어느 시각부터 어느 시각까지 무언가가 감지됐다"는 **에피소드**다.

- **카메라 에피소드**: `detection_batch`의 person 라벨을 엔진과 같은 규칙으로 재생.
  파라미터(기본값 = 운영자 설정): 신뢰도 ≥ c(기본 0.2 = 현재 엔진과 동일), 연속 n프레임(`idle_frames`=2),
  stale 3초, 카메라 역할 집합(기본 IDLE = ceiling/front/rear_side; `opposite_side` 포함 토글).
  끊김 < 5초는 하나로 병합. raw `person_window`(0.2, 모든 카메라)도 "원본 창"으로 함께 표시.
- **레이더 에피소드**: `ld2410_sample`(Phase 0 이후) 또는 `person_sample.ld2410`(과거 데이터, 부분)에서
  `target_status ≠ 0`이 d초 이상 지속(기본 2초), 끊김 < 5초 병합. 옵션 게이트: Moving만 / 거리 범위 /
  에너지 하한.
- **정합(pairing)**: 시간 겹침(±3초 허용)으로 묶어 **둘 다 / 카메라만 / 레이더만** 세 종류로 분류.
- **엔진 에피소드**(P4 이후): 실제로 화면 경고가 나간 구간. "제품이 한 판단"으로 별도 표시.

### 2.2 정답(ground truth) 표시

- 에피소드마다 사람이 **사람 있음 / 사람 없음 / 판단 불가**를 고른다. 선택지 보조: "사람이 카메라
  화면 밖(문 밖)에 있음", "차량 안 탑승자", "작업자" 같은 태그(자유 텍스트 메모 포함).
- 근거: 시작 스냅샷(`person`/`radar`), 종료 스냅샷(`person_end`), 클립(브라우저 재생), 창 내부 0.5초
  샘플 타임라인, 레이더 거리/에너지 그래프.
- 저장: `data/analysis/labels/<YYYY-MM>.jsonl`(append-only: `episode_id`, `source_host`, `verdict`, `tags`,
  `note`, `reviewer`, `labeled_at`, `evidence_paths`). 같은 에피소드의 최신 레코드가 유효. 선택적으로
  NAS `${FOLDER}/analysis/labels/`에 동기화해 여러 PC/검토자가 공유(충돌 시 검토자별 표시).
- `episode_id`는 결정적(`sha1(source_host, source, start_at, end_at, params_hash)`)이라 재계산해도
  레이블이 붙는다. 파라미터를 바꾸면 에피소드 경계가 변하므로 **레이블은 기본 파라미터 세트에만** 붙이고,
  스윕은 레이블된 에피소드와의 겹침으로 평가한다.

### 2.3 미감지(둘 다 놓침) 추정 — 통제 샘플

두 센서 모두 놓친 사람은 로그에 없다. 이를 추정하려면 **주기적 통제 스냅샷**이 필요하다:
`RAW_MEDIA_CONTROL_SNAPSHOT_SECONDS`(기본 0=끔, 권장 600)로 감지 여부와 무관하게 전 카메라 스냅샷을
찍고 `control` 종류로 기록. 대시보드에서 무작위 통제 스냅샷을 라벨링하면 "사람이 있었는데 아무도 감지
못한 비율"의 하한을 얻는다. (Phase 0 선택 항목.)

### 2.4 지표

| 지표 | 정의 | 비고 |
|---|---|---|
| 정밀도(소스별) | 라벨된 해당 소스 에피소드 중 "사람 있음" 비율 | 핵심 비교 수치 |
| 상호 재현율 | 라벨 "사람 있음"인 전체 에피소드(두 소스 합집합 + 통제 샘플) 중 해당 소스가 잡은 비율 | 절대 재현율은 불가, "서로 대비"임을 UI에 명시 |
| 시간당 오탐 | "사람 없음" 에피소드 수 ÷ 커버리지 시간 | 커버리지 미보정 수치는 표시 금지 |
| 감지 지연 | 둘 다 감지한 에피소드에서 (레이더 시작 − 카메라 시작) 초 분포 | 레이더가 빠르면 그것도 정직하게 |
| 지속 시간 분포 | 소스별 에피소드 길이 히스토그램 | 블립 vs 실제 체류 |
| 카메라별/시간대별 | 카메라 ID × 시간(0~23) 히트맵 | `opposite_side` 문 밖 통행 확인 |
| 임계값 스윕 | 카메라: 신뢰도 c × 연속 n; 레이더: 지속 d × Moving/거리/에너지 게이트 → 정밀도·재현율 곡선 | 운영자 설정 근거 |
| 커버리지 | 감시 자식 가동 시간, 레이더 클라이언트 연결 시간, 카메라 정상 시간(일별·시간별) | 모든 비율의 분모 |

---

## 3. 대시보드 설계 (페이지)

원칙: 숫자마다 "어떻게 계산했는지" 클릭 한 번으로 보이고, 표의 어느 행이든 원본 JSON 레코드까지
내려갈 수 있다. 시각 언어는 운영 콘솔 시안 B(어두운 패널, 앰버 액센트)를 따르되 데이터 색은
카메라=시안, 레이더=마젠타, 둘 다=흰색, 정답 있음/없음=녹/적으로 고정.

1. **개요(Overview)** — 기간 선택(날짜·범위·호스트), KPI 타일(소스별 정밀도, 상호 재현율, 시간당 오탐,
   라벨 진행률 n/N), 일별 막대(카메라/레이더/둘 다 에피소드 수), 커버리지 띠(회색=감시 없음). KPI에
   마우스를 올리면 정의·분모·분자 표시.
2. **타임라인(Day)** — 하루 24시간 가로 띠 3줄(카메라·레이더·엔진 경고) + 커버리지 배경 + 차량 세션
   마커. 확대(드래그)·클릭 → 에피소드 상세. 사람 창 꼬리(5초 유예)는 옅게.
3. **에피소드 목록(Episodes)** — 필터(종류: 둘 다/카메라만/레이더만, 라벨 상태, 카메라, 길이, 최대
   신뢰도, 호스트), 정렬, 썸네일 열. 키보드로 빠르게 라벨링(1=있음 2=없음 3=불가, ←/→ 이동).
4. **에피소드 상세(Review)** — 좌: 시작/종료 스냅샷(카메라별, bbox 오버레이 재그리기), 클립 플레이어
   (MKV→MP4 리먹스 캐시), 우: 0.5초 샘플 타임라인(카메라별 present, 신뢰도), 레이더 거리·에너지 곡선,
   게이트 에너지 미니 히트맵. 하단: 판정 버튼+태그+메모, 원본 레코드 뷰어(JSON, 필드마다 한국어 설명).
5. **비교(Compare)** — 일치 매트릭스(둘 다/카메라만/레이더만 × 있음/없음/불가), 지연 히스토그램,
   길이 분포, 카메라×시간 히트맵, 레이더 거리·에너지 분포(정답별 색). 각 차트 아래 "이 차트의 데이터
   내려받기(CSV)".
6. **임계값 스윕(Tuning)** — 슬라이더로 c·n(카메라), d·게이트(레이더)를 바꾸면 라벨 대비 정밀도·재현율
   즉시 갱신, 현재 운영자 설정값을 기준선으로 표시. 결과를 "권장 설정"으로 내보내되 **적용은 감시 설정
   페이지에서 사람이** 한다.
7. **데이터 사전(Dictionary)** — 모든 event_type·payload 필드의 뜻, 단위, 출처 모듈, 안전 영향
   (`raw_only`) 표. 상세 페이지의 필드 툴팁이 이 표를 참조.
8. **동기화/상태(Data)** — NAS 날짜 목록(호스트별), 로컬 캐시 여부, manifest SHA-256 검증 결과, 인덱스
   재빌드 버튼, 부분 업로드(manifest 없음) 경고.

접근성·직관성: 각 페이지 상단에 한 줄 설명, 빈 상태 메시지("이 기간에는 감시 기록이 없습니다 —
커버리지 0시간"), 모든 시각은 KST, 숫자 옆에 분모 표시.

---

## 4. 아키텍처

### 4.1 실행 형태 결정: 로컬 웹 앱 (권장)

| 선택지 | 장점 | 단점 |
|---|---|---|
| **로컬 웹 앱** (`towersightai-analyze serve`, 브라우저) | 차트·표·영상 재생·키보드 라벨링에 최적, 분석 PC 어디서나(현장기 부담 없음), HTML 시안을 그대로 제품으로 | 의존성 추가(flask), 브라우저 필요 |
| 운영 콘솔 PyQt 페이지 | 한 앱 | QtCharts/WebEngine 미설치, 4.7k줄 `pyqt_app.py` 비대화, 현장기 CPU/RTSP 세션에 부담, 원격 분석 불편 |
| 정적 HTML 리포트 생성 | 의존성 0 | 라벨링 불가 |

권장: 웹 앱 + 정적 리포트 내보내기(§5). 운영 콘솔에는 `데이터 분석` 항목을 두어 서버를 띄우고 브라우저를
여는 정도만 연결(선택, Phase 3).

기술: Python 3.12, **Flask**(`pip install -e ".[analyze]"`), Jinja2 템플릿 + 바닐라 JS, **Chart.js**를
`towersightai/analyze/static/vendor/`에 동봉(오프라인 동작), **SQLite**(stdlib) 인덱스, `paramiko`(기존)
SFTP, `ffmpeg`(있으면 리먹스, 없으면 다운로드 링크). pandas 불필요(하루 ~10만 레코드는 순수 Python으로
수 초).

### 4.2 패키지 구조

```text
towersightai/analyze/
├── __init__.py
├── nas_reader.py      # SFTP 날짜/호스트 목록, manifest SHA-256 검증 다운로드 → data/analysis/cache/<host>/<day>/
├── loader.py          # 샤드(.jsonl/.jsonl.gz) → 레코드 iterator, 스키마 v2 검증, 시간대 변환
├── episodes.py        # 카메라/레이더/엔진 에피소드 재구성 + 정합 (순수 함수, 파라미터 dataclass)
├── coverage.py        # ai_started/stopped, ld2410_server_status, application_* → 구간 집합
├── metrics.py         # §2.4 지표, 임계값 스윕
├── labels.py          # JSONL append-only 저장/조회, 결정적 episode_id, NAS 공유(선택)
├── media.py           # 미디어 경로 해석, MKV→MP4 리먹스 캐시, bbox 오버레이 좌표
├── index.py           # SQLite 인덱스(에피소드·샘플·미디어), 재빌드
├── dictionary.py      # 데이터 사전(필드 설명, 한국어)
├── server.py          # Flask 앱 팩토리, JSON API (/api/days, /api/episodes, /api/label, /media/…)
├── report.py          # 정적 HTML/CSV 내보내기
├── templates/         # overview.html, day.html, episodes.html, review.html, compare.html, tuning.html, dictionary.html, data.html
└── static/            # app.css, app.js, vendor/chart.umd.js
towersightai/cli/analyze.py   # towersightai-analyze {sync,index,serve,report}
```

데이터 흐름: NAS → `nas_reader`(캐시, 검증) → `loader` → `episodes`/`coverage` → `index`(SQLite) →
`server` → 브라우저. `labels`는 인덱스와 별도 파일(재빌드해도 보존). 캐시·인덱스·라벨은
`data/analysis/`(gitignore; 라벨은 백업 대상).

### 4.3 CLI

```bash
towersightai-analyze sync  --env .env --from 2026-09-01 --to 2026-09-10 [--host pakrio-shinantower] [--no-media]
towersightai-analyze index --from 2026-09-01 --to 2026-09-10        # 에피소드/커버리지 재계산
towersightai-analyze serve --port 8765 [--open]                       # http://127.0.0.1:8765
towersightai-analyze report --from … --to … --output artifacts/analysis/report-2026-09.html
```

`serve`는 기본 `127.0.0.1` 바인드(원격 노출 없음). `.env`는 NAS 접속에만 쓰고 자격 증명은 로그·페이지에
절대 표시하지 않는다(`redact_sensitive_text`).

---

## 5. 추가 제안 (사용자 아이디어 외)

1. **레이더 상시 기록 + 레이더 트리거 증거**(P1·P2) — 이것 없이는 "카메라가 더 정확하다"를 증명할 수 없다.
2. **통제 스냅샷**(§2.3) — "둘 다 놓침"의 하한을 숫자로.
3. **엔진 판정 기록**(P4) — 센서 비교와 별개로 "제품이 실제로 경고한 횟수와 정확도"를 보고할 수 있다.
4. **임계값 스윕 → 운영자 설정 제안** — 분석 결과가 현장 튜닝(INTENT §5)의 근거가 된다. 사람 감시에
   신뢰도 임계값이 없다는 점(P6)을 스윕으로 검증한 뒤 설정 추가 여부 결정.
5. **감지 지연 비교** — 레이더가 정밀도는 낮아도 더 빠를 수 있다. 양쪽을 다 보여줘야 보고서가 신뢰받는다.
6. **다중 검토자와 일치도** — 두 사람이 같은 에피소드를 라벨하면 불일치 목록과 Cohen's κ 표시(간단).
7. **정적 보고서 내보내기** — 기간·필터·차트·표를 단일 HTML(+CSV)로 저장해 발주처 공유.
8. **NAS 상태 감시 대시보드 겸용** — 날짜별 업로드 완료/부분(manifest 없음)/호스트 표시. 09-09 같은
   부분 업로드를 바로 발견.
9. **bbox 재그리기** — 스냅샷 위에 raw 감지 박스·신뢰도를 그려 "카메라가 무엇을 사람으로 봤는지" 즉시 확인.
10. **에피소드 공유 링크** — `/review/<episode_id>` URL로 특정 사례를 문답에 첨부.

---

## 6. 단계별 구현 계획

각 단계는 테스트 동반, `pytest -q` 하드웨어 없이 통과, UI-first 규칙(§7 안전) 준수.

### Phase 0 — 기록 보강 (제품 코드, 현장 배포 필요) · 예상 1~2일

| 작업 | 파일 | 테스트 |
|---|---|---|
| `ld2410_sample` 1 Hz 상시 기록(클라이언트 연결 중, 최신 프레임 요약: target_status, 거리, 에너지, 게이트, age) | `storage/raw_data.py` `tick()`, `ui/pyqt_app.py` | 창 밖에서도 1 Hz로 기록, 미연결 시 기록 없음 |
| `radar_window_started/closed`(target_status≠0 ≥ `RAW_DATA_RADAR_WINDOW_MIN_SECONDS` 기본 2, 끊김 5초 병합) | `storage/raw_data.py` 새 `RadarWindowTracker` | 시작/종료/병합/미연결 |
| `radar` 종류 스냅샷+클립(≥3초 지속, 30초당 1회 제한, `RAW_MEDIA_RADAR_EVIDENCE=true`) | `storage/evidence.py` | 제한·실패 이벤트·simulated 무시 |
| 통제 스냅샷 `RAW_MEDIA_CONTROL_SNAPSHOT_SECONDS`(0=끔) | `storage/evidence.py`, `config/settings.py` | 주기·감지 중 중복 억제 |
| `engine_person_state` 이벤트(상태 변화 시 cameras/radar/phase) | `process/engine.py` `RawEventRequest`, `ui/pyqt_app.py` | 변화 시에만. 레이더는 검증 전용이라 엔진 판정에 포함되지 않음 |
| 원격 경로 `raw/<source_host>/YYYY-MM-DD/` + 로컬 마커 호환 | `storage/archive.py`, `raw_data.py` | 경로·기존 마커 재업로드 안 함 |
| 문서: `.env.example`, README(§raw), CLAUDE.md §8, testing-strategy | | |

완료 기준: 현장기에서 하루 이상 수집 후 `ld2410_sample`·`radar_window_*`·`radar` 스냅샷이 NAS
`raw/pakrio-shinantower/<day>/`에 있음.

### Phase 1 — 분석 코어 (순수 Python) · 예상 2~3일

`nas_reader`, `loader`, `episodes`, `coverage`, `metrics`, `labels`, `index`, CLI `sync/index`.
과거 데이터(레이더가 `person_sample` 안에만 있는 09-03)도 "부분 레이더" 모드로 읽는다.
테스트: 합성 JSONL 픽스처(사람 창 3종, 레이더 창, 재시작 구간, 호스트 2개, gz/plain 혼합), 에피소드
경계·병합·정합·episode_id 결정성, 커버리지 분모, 지표 수식, 라벨 최신값 우선, 가짜 SFTP(기존
`test_hourly_archive.py` 방식)로 검증 다운로드·부분 업로드 감지.

### Phase 2 — 대시보드 · 예상 3~4일

1. **HTML 시안 먼저**(`docs/design/analyze-dashboard-proposal.html`, 가짜 데이터) → 사용자 승인(기존 작업
   방식 INTENT §3).
2. Flask 서버 + 템플릿 8페이지(§3), JSON API, 미디어 서빙(리먹스 캐시), 라벨 API, 키보드 라벨링.
3. 실제 09-03 데이터로 라벨 20개 이상 찍어 보고 흐름 확인(스크린샷 `tmp/analyze-verification/`).
테스트: Flask test client로 각 라우트 200·빈 기간·잘못된 episode_id·라벨 왕복·자격 증명 미노출·
`127.0.0.1` 기본 바인드.

### Phase 3 — 보고서·튜닝·연결 · 예상 1~2일

`report` 내보내기, 임계값 스윕 페이지, 다중 검토자 일치도, 운영 콘솔 `데이터 분석` 항목(서버 기동+브라우저
열기, 안전 상태 무관), CLAUDE.md/README/PLAN.md 갱신.

---

## 7. 안전 규칙과의 관계

- 분석 패키지는 **raw 데이터를 읽기만** 한다. 엔진, 상태기, PLC 어댑터, `can_show_final_ok`를 import하지
  않는다(테스트로 고정: `towersightai.analyze`가 `process`/`plc`/`state_machine`/`ui`를 import하지 않음).
- Phase 0의 새 이벤트는 모두 `safety_effect: raw_only`. 레이더 창·통제 스냅샷은 엔진 입력이 아니다.
- 임계값 스윕의 "권장 설정"은 파일로 내보낼 뿐 자동 적용하지 않는다.
- 라벨은 사람의 사후 판단이며 실시간 판정에 쓰이지 않는다.
- NAS 자격 증명은 `.env`에서만 읽고 페이지·로그·리포트에 남기지 않는다. 리포트에 스냅샷을 포함할 때는
  번호판 마스킹 옵션을 둔다(발주처 공유 시).

---

## 8. 결정이 필요한 항목

1. 웹 앱(권장) vs PyQt 페이지 — §4.1.
2. Phase 0 배포 순서: 호스트별 원격 경로 변경(P3)을 먼저 현장기에 적용할지(과거 폴더는 그대로 읽음).
3. 통제 스냅샷 주기(권장 10분, 카메라 3대 × 하루 432장 ≈ 80 MB/일)와 레이더 증거 제한값(30초당 1회).
4. 라벨 공유 방식: 로컬만 vs NAS `analysis/labels/` 동기화.
5. 리포트에 스냅샷 포함 여부(번호판 마스킹).

---

## 9. 구현 상태 (2026-09-10)

구현 완료 — `towersightai/analyze/` + `towersightai-analyze` CLI + `tests/test_analyze_core.py`, `tests/test_analyze_server.py`.
계획과 달라진 결정:

- **의존성 0**: Flask/Chart.js 대신 표준 라이브러리 HTTP 서버(`http.server`)와 손으로 그린 SVG 차트. 설치 없이
  프로젝트 `.venv`만으로 실행되고 테스트가 소켓 없이 `AnalysisApp.dispatch()`를 직접 호출한다.
- **현장 등록은 `data/analysis/sites.json`** (§4.3의 `.env` 직접 사용 대신). 현장이 늘어 NAS 위치가 달라져도
  대시보드의 `데이터 · NAS 설정` 페이지나 `sites add/import-env`로 등록한다. 비밀번호는 API/화면에 나가지 않는다.
- **호스트 충돌(P3)**: 업로드는 `raw/<source_host>/<day>`로 바꿨고(`archive.remote_raw_day_dir`), 리더는 레거시
  `raw/<day>`와 새 구조를 모두 읽어 `(source_host, day)`로 구분한다. 호스트마다 현장/개발 역할을 `sites.json`에
  두고(이 PC 호스트명 = 개발 자동) 대시보드는 기본으로 현장만 보여준다. 개발 캐시는 삭제 가능(로컬만).
- **자동 최신화**: 서버가 떠 있는 동안 10분마다(`serve --auto-sync-minutes`) 새/변경/부분 날짜의 이벤트만 받는다.
  사이드바 `NAS 최신화` 버튼은 즉시 실행.
- 다이제스트는 JSON 캐시(`data/analysis/sites/<site>/index/`)이며 SQLite는 쓰지 않는다(하루 3만 레코드 ≈ 1.3초).
- 미디어는 **볼 때 개별 다운로드**(`/media` 라우트가 NAS에서 받아 캐시, manifest SHA-256 검증). `sync --media`로
  미리 받을 수도 있다. 클립은 ffmpeg `-c copy`로 MP4 리먹스.
- 라벨 키는 `sha1(site|host|source|시작 초)`: 파라미터를 바꿔도 시작 초가 같으면 라벨이 유지된다.

- **번호판(2026-09-11)**: raw에 성공한 다수결 결과만 남고 미인식·개별 판독이 없어서, 제품 코드에 `plate_attempt`
  (1초 주기 판독 1건, 거부 사유 포함)와 미인식/중단 결과 기록을 추가했다. 대시보드는 에피소드마다 겹치는 차량
  세션의 번호판을 표시하고, 검토 화면에 판독 이력과 번호판 이미지를 붙인다. 다이제스트 스키마는 v2.

아직 없는 것: 정적 HTML 보고서 내보내기(CSV만 있음), 스냅샷 위 bbox 오버레이(다이제스트에 bbox를 넣지 않음),
운영 콘솔의 `데이터 분석` 항목(현장에서 운영하지 않기로 해 불필요), 통제 스냅샷·엔진 판정 이벤트(raw 보강 범위).
