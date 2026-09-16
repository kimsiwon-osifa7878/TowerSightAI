# TowerSightAI

주차기(기계식 주차 설비) 내부를 4대의 RTSP 카메라와 Hailo-8 AI 가속기로 감시해, 차량 진입·번호판 인식·
정렬 안내·사람 감지를 수행하고 **모든 안전 조건이 증명될 때만** PLC에 OK를 보내는 시스템입니다.

현재는 구현 프로토타입 단계입니다. 핵심 규칙은 변하지 않습니다: 불확실하거나, 오래됐거나, 시뮬레이션이거나,
비정상인 입력은 **항상 최종 OK를 차단**합니다.

이 문서는 **설치·운영·문제 해결 방법** 위주입니다. 개발/에이전트용 문서는 [맨 아래](#개발자에이전트-문서)를 보세요.

---

## 1. 설치

### 요구 스택 (Hailo-8 기준, 버전 고정)

- Ubuntu 24.04, Python 3.12
- **HailoRT 4.23.0 + TAPPAS Core 5.1.0 + Hailo Apps 26.03.1** — HailoRT 5.x는 Hailo-10H용이므로 설치 금지
- 새 장비는 이 가이드를 처음부터 따라가면 됩니다: **[Hailo-8 Ubuntu 설치 가이드](docs/hailo8-ubuntu-installation.md)**

### 프로젝트 설치

```bash
cd ~/TowerSightAI
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[ui]" pytest
```

`fast-alpr[onnx]`(번호판 인식, CPU)와 `paramiko`(NAS)가 함께 설치됩니다. 번호판 모델은 최초 1회
온라인 상태에서 초기화되어 `~/.cache/`에 받아집니다(설치 가이드에 포함).

### 사이트 설정 (.env)

```bash
cp .env.example .env
```

`.env.example`의 주석을 따라 채우면 됩니다. 특히 주의할 값:

- `CAMERA_1..4_*`: ID/역할/RTSP URL/계정/회전. 역할 4종(ceiling, front, rear_side, opposite_side)은
  모두 있어야 합니다. 천장 카메라 미설치 현장은 `BIRDVIEW_MODE=disabled` + ceiling URL은 접속 불가
  placeholder(`192.0.2.10` 등)로 두세요.
- **`CAMERA_N_RECORD_RTSP_URL`은 반드시 `stream2`로.** Tapo 카메라는 스트림당 동시 세션이 2개라,
  프리뷰와 AI 추론이 stream1을 쓰는 동안 증거 레코더까지 stream1로 붙으면 추론이 RTSP 400으로
  거부됩니다.
- `SYNOLOGY_NAS_*`: NAS 업로드 계정. 호스트 키는 아래 §5 참고.
- 카메라 IP는 공유기에서 **DHCP 고정 예약**을 해두세요. 재부팅 후 IP가 바뀌면 카메라가 NG로 뜹니다.

---

## 2. 실행

```bash
./run.sh          # 현장 전체화면
./run-window.sh   # 개발/점검용 창모드
```

첫 화면은 운전자용 **사용자 화면**입니다.

> **자동 감시**: 앱을 켜면 카메라 수신이 시작되는 대로 **주차 프로세스 감시가 자동 시작**됩니다
> (`프로세스 감시` 추론 — 사람+차량 동시 감지). IDLE에서는 버드뷰·전면·좌측면에서 사람을 감시하고
> (우측면은 문이 열리면 바깥이 보여 제외), 우측면에서 차량이 연속 감지되면 진입 → 번호판 인식(1초
> 주기, 차량진입선 아래만, 다수결) → 유도선 안내 → 하차 안내 → 무인 10초 확인 → PLC OK 전송(현재는
> 모의) → 주차기 작동(60초 가정) → 대기 복귀로 순환합니다. 진단 메뉴에서 수동 AI를 실행하면 감시가
> 잠시 멈추고, 끝나면 자동 재개됩니다. 상태줄의 `프로세스 …` 항목에서 현재 단계를 확인하세요.

운영자(개발자) 콘솔 진입 방법 3가지:

1. 우하단 **`운영자 모드` 버튼** (현장 권장)
2. 우상단 보이지 않는 72×72px 영역을 **2초간 꾹** (조기 release/이탈 시 취소)
3. 키보드 `Ctrl+Shift+O`

운영자 콘솔에서 `사용자 화면`으로 복귀, `프로그램 종료`(확인창)로 종료합니다.

---

## 3. 운영자 콘솔 사용법

사이드바는 `메뉴` 버튼으로 열고, 세 섹션으로 나뉩니다. 각 항목은 페이지를 열며, 실행 버튼은 페이지
안에 있습니다. **모든 진단·시뮬레이션은 최종 OK를 절대 허용하지 않습니다.**

### 운영
| 메뉴 | 하는 일 |
|---|---|
| 사용자 화면 | 운전자용 화면으로 복귀 |
| 감시 설정 | 프로세스 엔진 튜닝: 차량 감지 임계값(기본 0.6)·연속 프레임 수(기본 5), 사람 감지 디바운스, 차량진입선, 사다리꼴 바퀴 유도선/정지선, 무인 확인·주차기 작동 시간, NAS 업로드 방식(예약/즉시). 전면 카메라 미리보기에 차량진입선·사다리꼴 유도선이 표시되고, 저장 즉시 적용. `프로세스 감시 일시중지/재개` 버튼 포함 |
| 주차 프로세스 테스트 | 주차 단계(IDLE→진입→진입완료→번호판인식→주차시작)를 버튼으로 재현. `차량 진입 시뮬레이션` 포함. UI 확인 전용 |

### 진단
| 메뉴 | 하는 일 |
|---|---|
| 전체 카메라 | 활성 카메라 실시간 그리드. `사람 감지 시작`/`차량 감지 시작` 버튼 포함(누르면 실행 중인 추론을 멈추고 누른 추론을 시작) |
| 차량 감지 | front 카메라에서 Hailo 검출(차량 라벨만). `차량 감지 시작/중지` |
| 사람 감지 | 수신 중인 모든 카메라에서 person 감지. 박스는 각 타일에 표시 |
| 번호판 인식 | `정면 카메라 인식`(현재 프레임 1장) / `번호판 이미지 인식`(tmp/car_number-test 일괄). FastALPR(CPU) |
| 레이더 (LD2410) | ESP32가 보내는 레이더 원시 프레임 콘솔. 표시 전용(안전판정 미사용) |
| NAS 연결 확인 | NAS `connectiontest/`에 검증 페이로드 기록+SHA-256 재확인. 카메라 수신 중이면 2초 클립 동봉 |
| NAS 파일 전송 | `파일 선택` → `NAS로 보내기`. 선택한 파일을 NAS `transfer/` 폴더 한 곳에 SHA-256 검증 업로드. 원격 접속으로 파일을 못 옮길 때 NAS 중계용 |
| 카메라 캘리브레이션 | 체커보드(`towersightai-checkerboard`로 인쇄) 유도 촬영 15자세 → `측정 실행`으로 렌즈 내부 파라미터 측정. 결과 `data/calibration/intrinsics/<camera>.json`(reviewed=false, 측정 파일일 뿐) |
| 시스템 점검 | 설정/Hailo 설치/샘플 추론/카메라별 프레임/PLC 시뮬레이터 개별 실행, `전체 스모크`는 전부 순차 실행. **Hailo 장치 상태 패널**(60초 자동 갱신) 포함 |
| 실행 로그 | `towersightai.log` 실시간 tail + 문자열 필터 |

> 실행 중인 AI가 있을 때 다른 AI를 시작하면 **기존 것이 자동 중지되고 새 것이 이어서 시작**됩니다.

### 시스템
| 메뉴 | 하는 일 |
|---|---|
| 카메라 설정 | 카메라별 회전(0/90/180/270) 런타임 변경 |
| 프로그램 종료 | 확인창 후 앱 종료 |

### 하단 상태줄 읽는 법

`상태 | PLC | 카메라 n/m | 모델 | AI 추론 | HAILO | 증거 | 시각`

- **HAILO 알약**: 초록 `HAILO 정상 65°C` / 노랑 `HAILO 링크오류 +N`(PCIe 오류 증가 — §6 참고) /
  빨강 `HAILO 오류`(장치 응답 없음). 상세는 시스템 점검 페이지.
- 카메라 손실, 버드뷰 OFF, PLC UNKNOWN 등 차단 사유는 상단 경고줄에 항상 표시됩니다.

---

## 4. 명령어 (CLI)

```bash
pytest -q                                                        # 하드웨어 없이 전체 테스트
towersightai-check-settings --env .env --health-check-cameras    # 카메라별 1프레임 수신 진단
towersightai-check-settings --env .env --check-hailo             # Hailo 설치 점검
towersightai-ai-diagnostics --env .env --output artifacts/runtime/ai-diagnostics.txt   # 장애 증거 수집(읽기 전용)
towersightai-sync-raw-data --env .env                            # 완료된 날짜 NAS 업로드 수동 실행
RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke --env .env --image data/samples/test-car.png --check-installation --run
```

자세한 로그가 필요하면 `LOG_LEVEL=DEBUG ./run-window.sh`.

---

## 5. 원격 아카이브(NAS)

`RAW_DATA_ENABLED=true`면 이벤트가 `artifacts/raw/YYYY-MM-DD/`에 시간별 JSONL로 쌓이고, 완료된 날짜는
백그라운드로 Synology SFTP(`${SYNOLOGY_NAS_FOLDER}/raw/`)에 업로드됩니다(파일별 SHA-256 검증, 검증된
업로드 14일 후 로컬 삭제). `RAW_MEDIA_ENABLED=true`면 실제 차량/사람/번호판 이벤트의 스냅샷과 무재인코딩
H.264 클립도 함께 보관됩니다. **아카이브 성공/실패는 안전 판정과 무관한 감사 기능입니다.**

레이더(LD2410)는 카메라 사람 창과 무관하게 1초마다 `ld2410_sample`(`raw_hex` 제외)로 기록되고, 레이더가
3초 이상 연속 감지하면 `radar_window_started`, 5초 이상 미감지/불명이면 `radar_window_closed`가 남습니다.
창이 열릴 때 `*-radar-*.jpg` 스냅샷과 최대 30초 `radar` 클립, 닫힐 때 `*-radar_end-*.jpg`가 추가됩니다
(카메라 사람 창이 이미 열려 있으면 `person_window_active`, 60초 안에 또 열리면 `radar_evidence_throttled`
사유만 기록). 설정 키: `RAW_DATA_LD2410_SAMPLE_INTERVAL_SECONDS`(0=끔), `RAW_DATA_RADAR_WINDOW_MIN_SECONDS`,
`RAW_DATA_RADAR_WINDOW_CLEAR_SECONDS`, `RAW_MEDIA_RADAR_EVIDENCE`, `RAW_MEDIA_RADAR_MIN_INTERVAL_SECONDS`,
`RAW_MEDIA_RADAR_CLIP_MAX_SECONDS`. 모두 분석 전용이며 안전 판정에는 쓰이지 않습니다.

**새 장비 최초 1회 — NAS 호스트 키 등록** (안 하면 `not found in known_hosts`로 업로드 실패):

```bash
sftp -P 45222 <NAS계정>@<NAS호스트>
```

지문을 기존 장비와 대조 후 `yes` → 비밀번호 확인 → `exit`. 이후 운영자 콘솔의 `NAS 연결 확인`으로 검증.

---

## 6. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| 카메라 타일이 `NG: 카메라 연결 이상` | ① `--health-check-cameras`로 진단 ② ping으로 IP 확인(DHCP 변경이 최다 원인) ③ Tapo 앱의 "카메라 계정"(RTSP 전용 계정) 확인 |
| front만 나오고 나머지 안 나옴 (구버전) | 최신 코드로 `git pull` — pip OpenCV의 GStreamer 부재를 전 카메라 FFmpeg 폴백으로 처리함 |
| AI 시작하자마자 `Bad Request (400)` | 카메라 동시 세션 초과. ① `.env`의 `CAMERA_N_RECORD_RTSP_URL`이 stream2인지 ② **잔존 UI 프로세스**(`pgrep -f operator_ui`)가 세션을 물고 있는지 확인 후 종료 |
| AI 시작하자마자 `HAILO_DRIVER_OPERATION_FAILED(36)` / HAILO 알약 빨강 | Hailo 장치가 응답하지 않음. `sudo modprobe -r hailo_pci && sudo modprobe hailo_pci` → 안 되면 **콜드 부팅(전원 완전 차단 30초)**, 재발 시 M.2 재장착 |
| HAILO 알약 노랑 `링크오류 +N` | PCIe 링크 신호 불량 누적(장치 행의 전조). M.2 장착 상태 점검, 지속되면 BIOS에서 해당 슬롯 PCIe Gen3→Gen2 |
| AI 버튼 오류의 상세 확인 | `실행 로그` 페이지에서 `hailo-health`, `ai-`, `camera-capture` 필터. 파일 로그: `artifacts/runtime/purpose-ai/<task>/…gst.log` |
| NAS 업로드 실패 | `실행 로그`에서 `raw-data`/`nas` 필터. `not found in known_hosts`면 §5 |
| 장애 보고 시 | `towersightai-ai-diagnostics` 출력 파일을 전달 (자격증명 미포함) |

---

## 7. 안전 규칙 (요약)

- 카메라 손실, 낮은 신뢰도, 미검증 캘리브레이션, PLC 미상, 시뮬레이션 입력, 사람 가능성 → **절대 OK 금지**
- 진단·테스트 통과는 구현 확인일 뿐 안전 승인이 아님 (`safe_to_operate=False`)
- 실제 자격증명은 `.env`에만. 로그·화면은 자동 마스킹됨
- 전체 규칙: [AGENTS.md](AGENTS.md)

---

## 개발자/에이전트 문서

코드를 수정하려는 사람/에이전트는 README가 아니라 아래를 읽으세요:

| 문서 | 내용 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | **에이전트 진입점**: 저장소 구조, 안전 게이트 위치, Hailo/GStreamer 런타임, 함정(gotchas) |
| [INTENT.md](INTENT.md) | 사용자와 합의된 작업 방식, 의사결정 이유, 현장 이력, 미해결 항목 |
| [AGENTS.md](AGENTS.md) | 안전 규칙, 아키텍처 경계, UI 검증 체크리스트 |
| [DESIGN.md](DESIGN.md) | 화면 설계 계약 (사용자 화면 시안/네이비, 운영자 콘솔 패널 HMI) |
| [PLAN.md](PLAN.md) | UI-first 작업 큐 |
| [docs/주차기_AI_안전감시_시스템_설계안.md](docs/주차기_AI_안전감시_시스템_설계안.md) | 제품 동작 명세 (상태 흐름, PLC 페이로드, 안전 원칙) |
| [docs/implementation/](docs/implementation/) | 영역별 구현 가이드 (아키텍처/카메라/Hailo/AI 스테이지/UI·캘리브레이션/테스트/로드맵) |
| [docs/design/](docs/design/) | 승인된 UI 시안 (사용자 화면 프로토타입, 운영 콘솔 시안 A·B) |

UI를 바꿨다면 커밋 전에 실제 화면 검증:

```bash
WAIT_SECONDS=15 tools/verify_operator_ui_screenshot.sh .env tmp/operator-ui-verification
```

## 부록 A. 데이터 분석 대시보드 (개발·검증 전용)

NAS에 쌓인 raw 데이터(JSONL·스냅샷·클립)를 읽어 **카메라 사람 감지와 레이더(LD2410) 사람 감지의 정확도를
비교**하는 로컬 웹 대시보드다. 현장 장비에서 운영하지 않고 개발/분석 PC에서만 실행한다. 읽기 전용이며
안전 게이트·엔진·PLC와 무관하다(`towersightai/analyze/`, 설계: `docs/implementation/analyze-dashboard.md`).

```bash
# 0) CLI 진입점이 없으면 한 번 재설치 (편집 설치 뒤 pyproject에 추가된 스크립트)
python -m pip install -e ".[ui]"
# 1) 현장(NAS) 등록 — 배포 .env의 SYNOLOGY_NAS_* 값을 가져오거나 직접 입력
towersightai-analyze sites import-env shinantower --env .env --label "구로 신안타워"
towersightai-analyze sites add other --host nas.example.com --port 45222 --user u --password p --folder /home/share
# 2) NAS 날짜 목록 확인 후 내려받기 (검증된 SHA-256, 이벤트만; 미디어는 볼 때 개별 다운로드)
towersightai-analyze days --site shinantower --remote
towersightai-analyze sync --site shinantower --from 2026-09-03 --to 2026-09-05 [--media]
# 3) 대시보드 (아래 한 줄이면 됨; PORT / AUTO_SYNC_MINUTES / NO_OPEN 환경변수로 조정)
./run-dashboard.sh                          # = towersightai-analyze serve --open, http://127.0.0.1:8765
```

- **현장/개발 데이터 구분**: 날짜의 소유 장비는 NAS 호스트 폴더(`raw/<host>/날짜`) > manifest의 `source_host` >
  현장 설정의 **기본 호스트**(`default_host`, 폴더도 manifest도 없이 올라온 날짜 = 재배포할 수 없는 현장기) 순서로 정한다.
  구로 신안타워는 `pakrio-shinantower`가 기본 호스트다. 예외는 `데이터 · NAS 설정`의 로컬 캐시 표에서 날짜별
  **소유 장비**를 직접 지정한다(예: 개발기가 manifest 없이 올린 2026-09-09 → `erumtni-NucBox-G3`). 지정은 `sites.json`에
  남아 다시 내려받아도 유지된다. 이 PC의 호스트명은
  자동으로 `개발`, 나머지는 `현장`으로 보며(`데이터 · NAS 설정`의 호스트 표에서 바꿀 수 있음) 모든 페이지는 기본으로
  **현장 데이터만** 보여준다(사이드바 호스트 선택: 현장만/전체/개발기만/특정 호스트). 개발 데이터의 로컬 캐시는 같은
  표에서 삭제할 수 있다(NAS 원본과 라벨은 유지). 업로드 쪽도 이제 `raw/<source_host>/YYYY-MM-DD/`로 나눠 저장하므로
  두 장비가 같은 날짜 폴더를 덮어쓰는 일은 생기지 않는다(기존 `raw/YYYY-MM-DD/`도 계속 읽는다).
- 서버가 떠 있는 동안 기본 10분마다 NAS를 확인해 새 날짜·바뀐 날짜의 이벤트 파일만 자동으로 받는다
  (`serve --auto-sync-minutes 0`으로 끔). 사이드바의 `NAS 최신화` 버튼은 같은 동작을 즉시 실행한다.
  스냅샷·클립은 자동 최신화 대상이 아니며 검토 화면에서 볼 때 개별로 받는다.
- 현장 등록은 대시보드의 `데이터 · NAS 설정` 페이지에서도 할 수 있다. 값은 `data/analysis/sites.json`
  (git 미추적, 0600)에 저장되며 비밀번호는 화면·API에 노출되지 않는다.
- 레이더가 몇 초 이상(기본 3초) 사람을 감지하면 그 창 동안 **전 카메라 스냅샷·클립과 1초 단위 카메라 상태**가 함께
  저장되어 NAS로 올라간다. 대시보드 에피소드 목록의 `카메라 대조` 열은 '레이더 감지 중 카메라가 사람을 본 초 / 전체 초'이고,
  검토 화면에 초 단위 대조표가 나온다. 동의 0회면 레이더 단독 감지다.
- 에피소드마다 그 차량의 **번호판**이 함께 표시된다(겹치는 차량 세션의 결과, 없으면 ±60초 안의 판독). 판독 실패는
  `미인식`으로 표시되고, 검토 화면에서 1초 주기 판독 이력(반영/제외 사유)과 번호판 잘라내기 이미지를 볼 수 있다.
- 페이지: 개요(정밀도·상호 재현율·시간당 오탐·커버리지), 타임라인(하루 띠, 드래그 확대), 에피소드(필터·CSV),
  검토(스냅샷·클립·0.5초 샘플·레이더 곡선·판정 1/2/3 단축키), 비교(일치 매트릭스·지연·분포), 임계값 스윕,
  데이터 사전, 데이터·NAS 설정.
- 레이더 단독 감지는 raw 보강(`ld2410_sample`, `radar_window_*`, `radar` 스냅샷) 이후 날짜부터 계산되며,
  그 전 날짜는 카메라 사람 창 안의 레이더 값만 있어 `부분`으로 표시된다.
- 클립 재생은 `ffmpeg`(MKV→MP4 리먹스)가 필요하다. 없으면 MKV 저장만 된다.
- 라벨은 `data/analysis/sites/<site>/labels.jsonl`에 append-only로 쌓인다(백업 대상).
