# vehicle_box_test — 차량 직육면체(3D 외곽) 검증 랩

주차기 앱과 **완전히 분리된 실험실 폴더**입니다. 여기서 가설을 검증하고, 검증된 것만
나중에 `towersightai/` 본체로 옮깁니다.

- 이 폴더의 코드는 운영 경로(`towersightai/`)에서 **절대 import 되지 않습니다.**
- 안전 게이트·PLC·상태기계·사용자 모드와 **무관**합니다. 여기 결과가 최종 OK에 영향을 줄 수 없습니다.
- NAS는 **읽기 전용**으로만 씁니다 (목록·다운로드만, 업로드·삭제 없음).
- `pytest`가 도는 `tests/`와는 다른 폴더입니다. 여기에는 단위 테스트가 아니라 **현장 자료 검증 도구**가 들어 있습니다.

배경·의도·지금까지의 판단 근거는 [CONTEXT.md](CONTEXT.md)에 정리돼 있습니다.
설계 확정안의 원본은 저장소 루트 `INTENT.md` §4 「3D 차량 직육면체 확정안」입니다.

---

## 검증하려는 가설

> 체커보드 내부 파라미터 없이, **고정된 현장 카메라 + 설계도의 지면 실치수**
> (턴테이블 Ø6,100 mm · 레일 내폭 2,106 mm · 팔레트 5,350×2,200 mm)만으로
> 차량의 바닥 사각형(앞·뒤 끝, 좌·우 바퀴선)과 직육면체를 뽑을 수 있는가?

판정 결과는 **이미지 안에** 새깁니다. 성공이면 치수를, 실패면 실패 사유를 한국어로 적어
**사람이 이미지만 보고 판단**할 수 있게 하는 것이 이 랩의 출력 규칙입니다.

---

## 실행 순서

```bash
# 1. NAS에서 차량이 찍힌 미디어만 골라 내려받기 (읽기 전용)
.venv/bin/python -m vehicle_box_test.nas_sampler --max-videos 16 --max-snapshots 260

# 2. 증거 클립에서 프레임 추출
.venv/bin/python -m vehicle_box_test.frames --interval 1.5 --max-per-clip 24

# 3. 카메라별 '빈 주차기' 배경(중앙값) 만들기
.venv/bin/python -m vehicle_box_test.background --max-frames 200

# 4. 지면 교정 — ruler로 좌표를 읽고 check로 투영해 눈으로 확인, 필요하면 반복
.venv/bin/python -m vehicle_box_test.calibrate ruler
.venv/bin/python -m vehicle_box_test.calibrate check

# 5. 추정 + 주석 이미지 생성
.venv/bin/python -m vehicle_box_test.run --max-frames-per-clip 4

# 6. 사람이 눈으로 판정하는 HTML 보고서
.venv/bin/python -m vehicle_box_test.report
xdg-open vehicle_box_test/out/report.html
```

> **보고서 이미지에는 실제 번호판이 그대로 보입니다. 로컬에서만 열고 외부로 공유하지 마세요.**

---

## 파일 구성

| 파일 | 역할 |
|---|---|
| `nas_sampler.py` | 이벤트 샤드(JSONL)를 읽어 **차량이 담긴 미디어만** 표본 추출 → 다운로드 → SHA-256 검증 |
| `frames.py` | 증거 클립(MKV) → 일정 간격 프레임 |
| `background.py` | 카메라별 중앙값 배경 + '가장 비어 있는 실제 프레임' |
| `rails.py` | 노란 레일 띠 자동 검출 (HSV) |
| `geometry.py` | 월드 모델 · 지면 호모그래피 · 호모그래피→카메라 자세 복원 · 3D 투영 |
| `calibrate.py` | 정규화 좌표 눈금자 / 지면 모델 투영 검증 |
| `estimate.py` | 배경차분 실루엣 → 접지선 → 월드 사각형 + **실패 사유 생성** |
| `draw.py` | 한국어 주석 그리기 (Pillow + Noto Sans CJK) |
| `run.py` | 전체 실행 → 주석 이미지 + `out/results.json` |
| `report.py` | `out/report.html` |
| `site_calibration.json` | 현장 지면 교정 (카메라별 대응점). **이 파일이 모든 mm의 근거** |
| `CONTEXT.md` | 작업 배경·의도·검증 결과·다음 단계 |

생성물 폴더 (둘 다 `.gitignore` 대상):

```
vehicle_box_test/
├── data/          # NAS에서 받은 자료와 중간 산출물
│   ├── events/<host>/<day>/   이벤트 샤드
│   ├── media/<day>/           스냅샷·클립
│   ├── frames/<day>/<clip>/   추출 프레임
│   ├── background/            카메라별 배경
│   ├── calib/                 눈금자·교정 확인 이미지
│   ├── sample-index.json      표본 목록
│   └── frame-index.json       프레임 목록
└── out/           # 판정 결과
    ├── annotated/             주석 이미지 (사람이 보는 결과물)
    ├── calib/                 보고서에 실리는 교정 이미지
    ├── results.json           판정 원자료
    └── report.html            검증 보고서
```

---

## 좌표계

`INTENT.md` §4 확정안과 동일합니다.

- 원점 = 팔레트(주차구획) 사각형 중심
- **x = 진입 방향** (+가 진입구 쪽, −가 주차기 안쪽)
- y = 폭 방향 (진입 방향을 바라볼 때 왼쪽이 +)
- z = 위, 단위 **mm**

지면 호모그래피는 **z=0 평면에서만** 정확합니다. 범퍼·지붕처럼 바닥에서 뜬 점을 지면
좌표로 바꾸면 틀리므로, 차량 위치는 **타이어 접지선에서만** 읽습니다.

---

## 의존성

프로젝트 `.venv`를 그대로 씁니다. 추가로 설치한 것은 **Pillow** 하나입니다
(cv2.putText가 한글을 못 그려서 Noto Sans CJK로 이미지에 한국어를 새기기 위함).
`towersightai/` 본체는 Pillow에 의존하지 않습니다.

```bash
.venv/bin/pip install Pillow
```
