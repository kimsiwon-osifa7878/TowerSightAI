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
| 전체 카메라 | 활성 카메라 실시간 그리드. `이전 AI Detection`(회귀 격리용 구 경로) 토글 포함 |
| 차량 감지 | front 카메라에서 Hailo 검출(차량 라벨만). `차량 감지 시작/중지` |
| 사람 감지 | 수신 중인 모든 카메라에서 person 감지. 박스는 각 타일에 표시 |
| 번호판 인식 | `정면 카메라 인식`(현재 프레임 1장) / `번호판 이미지 인식`(tmp/car_number-test 일괄). FastALPR(CPU) |
| 레이더 (LD2410) | ESP32가 보내는 레이더 원시 프레임 콘솔. 표시 전용(안전판정 미사용) |
| NAS 연결 확인 | NAS `connectiontest/`에 검증 페이로드 기록+SHA-256 재확인. 카메라 수신 중이면 2초 클립 동봉 |
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
