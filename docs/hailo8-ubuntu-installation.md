# Ubuntu 24.04에서 TowerSightAI와 Hailo-8 처음부터 설치하기

이 문서는 **Ubuntu 24.04가 새로 설치된 x86_64 컴퓨터**, **Hailo-8 M.2 모듈**, **인터넷 연결**을 기준으로 합니다. 다른 컴퓨터에서 파일을 복사하거나 오프라인 번들을 사용하는 절차는 다루지 않습니다.

> **Developer Zone의 전체 `Latest` 패키지를 그대로 선택하지 마십시오.**
>
> 이 프로젝트의 Hailo-8 기준 조합은 **HailoRT 4.23.0 + TAPPAS Core 5.1.0**입니다. HailoRT 5.x는 Hailo-10H용 조합이므로 Hailo-8 장비에 설치하지 않습니다. PCIe 드라이버, HailoRT `.deb`, HailoRT Python `.whl`은 모두 4.23.0으로 맞추고, TAPPAS Core와 그 Python binding은 5.1.0으로 맞춥니다.

공식 호환성 및 명령은 [Hailo Apps 설치 문서](https://github.com/hailo-ai/hailo-apps/blob/main/doc/user_guide/installation.md)와 [TAPPAS 저장소](https://github.com/hailo-ai/tappas)를 참고하십시오.

설치 확인이나 추론이 실패한 상태는 안전 승인이 아닙니다. TowerSightAI는 원인이 해결될 때까지 PLC OK를 차단하고 NG를 유지해야 합니다.

## 설치 후 만들어질 구조

```text
~/Downloads/hailo/             Developer Zone에서 직접 받은 .deb와 .whl
~/hailo-apps/                  Hailo 공식 애플리케이션과 Python 가상환경
/usr/local/hailo/resources/   내려받은 HEF와 컴파일된 postprocess SO
~/hailo-apps/resources         위 resources 디렉터리를 가리키는 심볼릭 링크
~/TowerSightAI/                이 프로젝트와 전용 Python 가상환경
~/.cache/open-image-models/   FastALPR의 CPU ONNX 모델 캐시
```

실제 `.env`, RTSP 비밀번호, PLC 접속 정보는 Git 저장소에 커밋하거나 Hailo 지원 게시판에 첨부하지 마십시오.

## 1. Ubuntu 기본 환경 준비

터미널을 열고 시스템과 Python 버전을 확인합니다.

```bash
lsb_release -ds
uname -m
uname -r
python3 --version
python3 -c 'import sys; print(f"필요한 wheel ABI: cp{sys.version_info.major}{sys.version_info.minor}")'
```

다음을 확인합니다.

- OS가 Ubuntu 24.04입니다.
- `uname -m` 결과가 `x86_64`입니다.
- Ubuntu 24.04 기본 Python 3.12를 사용한다면 ABI 결과가 `cp312`입니다.

기본 패키지와 GStreamer, C++ postprocess 빌드 도구를 설치합니다.

```bash
sudo apt update
sudo apt install -y \
  "linux-headers-$(uname -r)" \
  build-essential cmake meson ninja-build pkg-config git curl wget zstd \
  python3 python3-pip python3-venv python3-dev \
  python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gtk-4.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  libopencv-dev rapidjson-dev

mkdir -p "$HOME/Downloads/hailo"
```

## 2. Hailo Developer Zone에서 5개 패키지 받기

1. 브라우저에서 [Hailo Developer Zone](https://hailo.ai/developer-zone/)을 엽니다.
2. 계정을 만들고 이메일 인증을 완료한 뒤 로그인합니다.
3. `Software Downloads` 또는 다운로드 메뉴로 이동합니다. 사이트 개편에 따라 메뉴 이름은 조금 달라질 수 있습니다.
4. 장치를 **Hailo-8**, 운영체제를 **Linux/Ubuntu**, 호스트를 **x86_64/amd64**로 선택합니다.
5. 전체 `Latest`가 HailoRT 5.x를 가리키면 선택하지 말고, 이전 릴리스 또는 아카이브에서 아래 버전을 찾습니다.
6. 다음 5개 파일을 `~/Downloads/hailo`에 저장합니다.

| 받아야 하는 파일 | 선택할 버전 | 용도 |
|---|---:|---|
| `hailort-pcie-driver_4.23.0_*.deb` | 4.23.0 | Hailo-8 PCIe 드라이버와 펌웨어 |
| `hailort_4.23.0_amd64.deb` | 4.23.0 | HailoRT 시스템 런타임 |
| `hailo-tappas-core_5.1.0_amd64.deb` | 5.1.0 | Hailo GStreamer 플러그인 |
| `hailort-4.23.0-cp312-cp312-linux_x86_64.whl` | 4.23.0 | Python 3.12용 HailoRT binding |
| `hailo_tappas_core_python_binding-5.1.0-*.whl` | 5.1.0 | TAPPAS Python binding |

파일명 끝의 빌드 번호는 다운로드 화면에 따라 달라질 수 있습니다. 중요한 것은 버전, `amd64`/`x86_64`, Python ABI입니다. Python이 `cp312`이면 HailoRT wheel에도 `cp312-cp312`가 있어야 합니다.

다운로드가 끝났는지 확인합니다.

```bash
cd "$HOME/Downloads/hailo"

printf '%s\n' "=== 받은 Hailo 패키지 ==="
find . -maxdepth 1 -type f \( -name '*.deb' -o -name '*.whl' \) -printf '%f\n' | sort

test "$(find . -maxdepth 1 -type f -name 'hailort-pcie-driver_4.23.0_*.deb' | wc -l)" -eq 1
test "$(find . -maxdepth 1 -type f -name 'hailort_4.23.0*amd64.deb' | wc -l)" -eq 1
test "$(find . -maxdepth 1 -type f -name 'hailo-tappas-core_5.1.0*amd64.deb' | wc -l)" -eq 1
test "$(find . -maxdepth 1 -type f -name 'hailort-4.23.0-cp312-cp312-linux_x86_64.whl' | wc -l)" -eq 1
test "$(find . -maxdepth 1 -type f -name 'hailo_tappas_core_python_binding-5.1.0-*.whl' | wc -l)" -eq 1

echo "5개 패키지 확인 완료"
```

Python ABI가 `cp312`가 아니라면 위 네 번째 `test`의 `cp312-cp312`를 실제 ABI로 바꿉니다.

## 3. Hailo 드라이버와 런타임 설치

다운로드 디렉터리에서 세 개의 시스템 패키지를 설치합니다.

```bash
cd "$HOME/Downloads/hailo"

sudo apt install -y \
  ./hailort-pcie-driver_4.23.0_*.deb \
  ./hailort_4.23.0*amd64.deb \
  ./hailo-tappas-core_5.1.0*amd64.deb

sudo reboot
```

재부팅 후 다시 로그인하고 장치가 인식되는지 확인합니다.

```bash
lspci -nn | grep -i hailo
lsmod | grep hailo
hailortcli fw-control identify
```

마지막 명령에 Hailo 장치 정보와 펌웨어 정보가 나오면 드라이버와 런타임이 연결된 것입니다. 여기서 실패하면 Hailo Apps를 먼저 설치하지 말고 다음을 확인합니다.

```bash
dpkg -l | grep -E 'hailort|hailo-tappas'
dmesg | grep -i hailo | tail -n 50
mokutil --sb-state
```

Secure Boot가 켜져 있고 `lsmod`에 Hailo 모듈이 없다면 DKMS 모듈 로딩이 차단됐을 수 있습니다. 장비 정책에 따라 Secure Boot를 끄거나 모듈 서명을 등록한 뒤 다시 확인합니다.

## 4. Hailo Apps 설치

Hailo Apps는 계속 변하는 `main` 대신 이 문서에서 확인한 릴리스 `26.03.1`로 고정합니다. 이것은 Hailo Apps의 릴리스 번호이며, 앞에서 고정한 HailoRT 4.23.0을 5.x로 바꾸라는 뜻이 아닙니다.

```bash
git clone --branch 26.03.1 --depth 1 \
  https://github.com/hailo-ai/hailo-apps.git \
  "$HOME/hailo-apps"

cd "$HOME/hailo-apps"
git rev-parse HEAD
git describe --tags --always
```

Hailo Apps용 가상환경을 만들고 Developer Zone에서 받은 두 Python wheel을 설치합니다.

```bash
cd "$HOME/hailo-apps"
python3 -m venv --system-site-packages venv_hailo_apps
source venv_hailo_apps/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install \
  "$HOME"/Downloads/hailo/hailort-4.23.0-*.whl \
  "$HOME"/Downloads/hailo/hailo_tappas_core_python_binding-5.1.0-*.whl
python -m pip install -e .
hash -r

python -c 'import hailo_platform; print("HailoRT Python binding OK")'
```

Hailo 리소스 디렉터리를 준비합니다.

```bash
sudo mkdir -p /usr/local/hailo/resources/packages
sudo chown -R "$USER:$USER" /usr/local/hailo
```

### TowerSightAI에 필요한 모델과 SO만 준비

`--all`은 사용하지 않습니다. 먼저 모델 다운로드 없이 postprocess SO와 환경 파일만 컴파일한 뒤, Hailo-8용 `yolov8m`만 받습니다.

```bash
cd "$HOME/hailo-apps"
source venv_hailo_apps/bin/activate

# 모델은 받지 않고 postprocess .so와 환경 설정만 준비
hailo-post-install --skip-download

# TowerSightAI가 사용하는 Hailo-8 모델 하나만 다운로드
hailo-download-resources --model yolov8m --arch hailo8

# setup_env.sh가 새로 만든 Hailo 환경을 현재 셸에 적용
source setup_env.sh
```

`hailo-post-install --skip-download`는 HEF를 추가로 받지 않지만 Hailo Apps에 포함된 C++ postprocess들을 로컬에서 컴파일합니다. 개별 SO 하나만 내려받는 공식 명령은 없으므로, TowerSightAI는 그 결과 중 필요한 `libyolo_hailortpp_postprocess.so`와 `libstream_id_tool.so`만 참조합니다.

TowerSightAI의 기본 경로가 공식 리소스 디렉터리를 보도록 심볼릭 링크를 만듭니다.

```bash
if [ ! -e "$HOME/hailo-apps/resources" ]; then
  ln -s /usr/local/hailo/resources "$HOME/hailo-apps/resources"
fi

readlink -f "$HOME/hailo-apps/resources"
```

`readlink` 결과는 `/usr/local/hailo/resources`여야 합니다.

필요 파일이 제 위치에 있는지 한 번에 확인합니다.

```bash
set -e

HAILO_RESOURCES="$(readlink -f "$HOME/hailo-apps/resources")"

test -s "$HAILO_RESOURCES/models/hailo8/yolov8m.hef"
test -s "$HAILO_RESOURCES/so/libyolo_hailortpp_postprocess.so"
test -n "${TAPPAS_POSTPROC_PATH:-}"
test -s "$TAPPAS_POSTPROC_PATH/libstream_id_tool.so"

ls -lh \
  "$HAILO_RESOURCES/models/hailo8/yolov8m.hef" \
  "$HAILO_RESOURCES/so/libyolo_hailortpp_postprocess.so" \
  "$TAPPAS_POSTPROC_PATH/libstream_id_tool.so"

echo "TowerSightAI용 Hailo 모델과 SO 확인 완료"
```

파일이 없다면 전체 모델을 받지 말고 필요한 단계만 다시 실행합니다.

```bash
cd "$HOME/hailo-apps"
source venv_hailo_apps/bin/activate

# 다운로드 가능한 정확한 모델 이름 확인
hailo-download-resources --list-models --arch hailo8 | grep -i yolov8m

# HEF가 없을 때
hailo-download-resources --model yolov8m --arch hailo8

# SO가 없을 때
hailo-post-install --skip-download
source setup_env.sh
```

## 5. TowerSightAI와 FastALPR 모델 설치

TowerSightAI를 내려받아 별도 가상환경에 설치합니다.

```bash
git clone \
  https://github.com/kimsiwon-osifa7878/TowerSightAI.git \
  "$HOME/TowerSightAI"

cd "$HOME/TowerSightAI"
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[ui]"
cp .env.example .env
```

번호판 이미지를 읽는 기능은 Hailo HEF가 아니라 CPU용 FastALPR 모델 두 개를 사용합니다.

- 검출: `yolo-v9-t-384-license-plate-end2end`
- OCR: `cct-xs-v2-global-model`

UI를 처음 실행할 때 자동으로 받을 수도 있지만, 설치 단계에서 미리 받아 두면 모델 준비 성공과 실제 OCR 성공을 구분하기 쉽습니다.

```bash
cd "$HOME/TowerSightAI"
source .venv/bin/activate

python - <<'PY'
from fast_alpr import ALPR

ALPR(
    detector_model="yolo-v9-t-384-license-plate-end2end",
    ocr_model="cct-xs-v2-global-model",
    ocr_device="cpu",
)
print("FastALPR detector/OCR model initialization OK")
PY

find "$HOME/.cache/open-image-models" -maxdepth 3 -type f \
  \( -name '*.onnx' -o -name '*.yaml' \) -printf '%p %k KB\n' | sort
```

모델 초기화 성공은 ONNX 파일을 읽을 수 있다는 뜻이며, 번호판을 실제로 인식했다는 뜻은 아닙니다. 실제 번호판 사진을 이용한 검사는 7절에서 별도로 수행합니다.

## 6. TowerSightAI 설정

`~/TowerSightAI/.env`의 Hailo 관련 부분을 다음과 같이 둡니다. `~`와 `${...}`는 TowerSightAI 설정 로더가 절대 경로로 해석합니다.

```dotenv
HAILO_APPS_WORKSPACE=~/hailo-apps
HAILO_APPS_RESOURCES=${HAILO_APPS_WORKSPACE}/resources
HAILO_APPS_PYTHON=${HAILO_APPS_WORKSPACE}/venv_hailo_apps/bin/python
HAILO_ARCH=hailo8

# 과거 설정명과의 호환용이며, 레거시 TAPPAS 파이프라인을 실행하지 않습니다.
TAPPAS_WORKSPACE=${HAILO_APPS_WORKSPACE}

HAILO_MODEL_DIR=${HAILO_APPS_RESOURCES}/models/${HAILO_ARCH}
HAILO_HEF_PATH=${HAILO_MODEL_DIR}/yolov8m.hef
HAILO_POSTPROCESS_SO=${HAILO_APPS_RESOURCES}/so/libyolo_hailortpp_postprocess.so
HAILO_NETWORK_NAME=filter_letterbox

# 차량과 사람은 같은 COCO yolov8m 모델 결과에서 각각의 라벨을 사용합니다.
HAILO_VEHICLE_DETECTION_HEF_PATH=${HAILO_HEF_PATH}
HAILO_VEHICLE_DETECTION_POSTPROCESS_SO=${HAILO_POSTPROCESS_SO}
HAILO_PERSON_PRESENCE_HEF_PATH=${HAILO_HEF_PATH}
HAILO_PERSON_PRESENCE_POSTPROCESS_SO=${HAILO_POSTPROCESS_SO}

FAST_ALPR_DETECTOR_MODEL=yolo-v9-t-384-license-plate-end2end
FAST_ALPR_OCR_MODEL=cct-xs-v2-global-model
```

카메라와 PLC 항목은 현장 값으로 별도로 설정하되, 비밀번호가 포함된 `.env`를 다른 사람에게 보내지 마십시오.

## 7. 모델과 실제 추론 확인

### 7-1. 설치 및 파일 확인

```bash
cd "$HOME/hailo-apps"
source setup_env.sh

hailortcli fw-control identify

for element in hailonet hailofilter hailoroundrobin hailostreamrouter; do
  gst-inspect-1.0 "$element" >/dev/null
  echo "GStreamer OK: $element"
done

cd "$HOME/TowerSightAI"
source .venv/bin/activate
towersightai-check-settings --env .env --check-hailo
```

모든 항목이 OK여야 다음 추론 확인으로 진행합니다. `--check-hailo`는 설치와 기본 Hailo 파일을 확인하는 명령이며 실제 영상에서 객체가 검출되는 것까지 보장하지는 않습니다.

### 7-2. `yolov8m.hef` 실제 추론 확인

TowerSightAI에 포함된 공개 테스트 이미지를 Hailo-8에 실제로 입력합니다.

```bash
cd "$HOME/TowerSightAI"
source .venv/bin/activate

RUN_HARDWARE_TESTS=1 towersightai-hailo-image-smoke \
  --env .env \
  --image data/samples/test-car.png \
  --check-installation \
  --run \
  --no-output-image
```

정상일 때는 다음을 확인합니다.

- `PIPELINE: OK`
- `DETECTIONS:`가 1 이상
- 뒤에 `car`, `truck`, `bus` 등의 검출 라벨과 confidence가 출력됨
- `artifacts/hailo/sample-detections.jsonl`에 JSON 레코드가 생성됨

프로세스만 정상 종료하고 `DETECTIONS: 0`이면 모델 실행은 됐지만 유효 검출이 없었던 것이므로 안전 판정은 계속 NG입니다. 다른 명확한 차량 이미지로 다시 확인하고 confidence 및 로그를 조사합니다.

### 7-3. FastALPR 실제 인식 확인

실제 번호판이 잘 보이는 테스트 사진을 `tmp/car_number-test`에 넣은 뒤 UI의 `번호판 이미지 LPR`을 실행합니다. 결과 로그에서 다음 두 상태를 따로 봅니다.

- `model-init-status=ready`: FastALPR 모델 로딩 성공
- `status=recognized`: 해당 이미지에서 실제 번호판 OCR 성공

모델 초기화가 성공해도 `no_result`이면 실제 인식은 실패한 것입니다. 사진 해상도, 번호판 크기, 각도와 조명을 확인하되 PLC OK로 처리하지 않습니다.

### 7-4. UI에서 재현하고 진단 파일 수집

```bash
cd "$HOME/TowerSightAI"
source .venv/bin/activate

LOG_LEVEL=DEBUG towersightai-operator-ui --env .env
```

문제가 발생한 직후 다른 AI 작업을 실행하기 전에 진단 파일을 만듭니다.

```bash
cd "$HOME/TowerSightAI"
source .venv/bin/activate

towersightai-ai-diagnostics --env .env \
  --output artifacts/runtime/ai-diagnostics.txt
```

`artifacts/runtime/ai-diagnostics.txt`에는 비밀번호를 마스킹한 설정 경로, 최신 run-id, 종료 코드, 이벤트 수, Hailo/FastALPR 로그 끝부분이 들어갑니다.

## 자주 발생하는 오류

| 오류 | 의미 | 확인할 것 |
|---|---|---|
| `HAILO_HEF_NOT_SUPPORTED` | HEF와 HailoRT 조합이 맞지 않음 | Hailo-8용 `yolov8m.hef`와 HailoRT 4.23.0인지 확인 |
| `undefined symbol: filter_letterbox` | 다른 버전의 postprocess SO가 로드됨 | Hailo Apps venv에서 `hailo-post-install --skip-download` 재실행 |
| `no element "hailo..."` | TAPPAS Core GStreamer 플러그인이 없음 | `hailo-tappas-core_5.1.0_amd64.deb` 설치와 `gst-inspect-1.0` 확인 |
| `No module named hailo_platform` | 잘못된 Python 또는 wheel ABI | Hailo Apps venv와 HailoRT 4.23.0 `cp312` wheel 확인 |
| `libyolo_hailortpp_postprocess.so` 없음 | postprocess 컴파일을 하지 않음 | `hailo-post-install --skip-download` 실행 |
| FastALPR가 실행 때마다 다운로드 시도 | ONNX 캐시가 완성되지 않음 | 인터넷 연결 상태에서 5절의 FastALPR 초기화 재실행 |
| 파이프라인은 살아 있지만 `events=0` | 유효 detection callback이 없음 | 모델 경로, SO, 입력 영상, confidence와 진단 로그 확인; 계속 NG 유지 |
