#!/usr/bin/env bash
# Hailo-8 장치 복구: 드라이버를 내리고 PCI 장치를 재열거한 뒤 다시 올린다.
#
# 현장에서 손으로 하던 순서를 그대로 스크립트로 옮긴 것이다. 칩이 버스에서 떨어져
# `HAILO_DRIVER_OPERATION_FAILED(36)` / `Device disconnected while opening device` 상태가 되면
# 이 절차로 되살아나는 경우가 많다(2026-09-16, 09-18 현장 확인).
#
# 지키는 규칙
#   * root 전용. 운영자 콘솔은 sudo 로 이 스크립트만 실행한다(tools/install_hailo_recover.sh).
#   * 운영자 UI는 절대 건드리지 않는다. /dev/hailo0 을 쥔 추론 자식만 정리한다.
#   * 복구는 승인이 아니다. 성공해도 안전 게이트·PLC·최종 OK와 무관하며, 판정은 그대로 NG에서
#     출발한다. 결과는 RESULT=/DETAIL= 줄로만 보고한다.
#   * 어떤 경우에도 하드웨어를 영구 변경하지 않는다(펌웨어 갱신·설정 쓰기 없음).
set -uo pipefail

DRY_RUN=0
KILL_HOLDERS=1
HOLDER_WAIT_SECONDS="${HOLDER_WAIT_SECONDS:-10}"
NODE_WAIT_SECONDS="${NODE_WAIT_SECONDS:-15}"
PCI_ROOT="${PCI_ROOT:-/sys/bus/pci}"
DEVICE_NODE="${DEVICE_NODE:-/dev/hailo0}"
HAILO_VENDOR_ID="0x1e60"

usage() {
  cat <<'USAGE'
사용: hailo_recover.sh [--dry-run] [--no-kill-holders]
  --dry-run           실제로 바꾸지 않고 수행할 단계만 출력한다.
  --no-kill-holders   장치를 쥔 프로세스가 있으면 종료하지 않고 실패로 끝낸다.
출력: RESULT=ok|failed 와 DETAIL=<설명> 한 줄씩. 종료코드 0=복구됨, 1=실패.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-kill-holders) KILL_HOLDERS=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "RESULT=failed"; echo "DETAIL=알 수 없는 인자: $1"; exit 1 ;;
  esac
  shift
done

log() { echo "STEP=$1"; }
finish() { echo "RESULT=$1"; echo "DETAIL=$2"; [[ "$1" == "ok" ]] && exit 0 || exit 1; }
run() { if [[ $DRY_RUN -eq 1 ]]; then echo "DRYRUN=$*"; else "$@"; fi; }

if [[ $DRY_RUN -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
  finish failed "root 권한이 필요합니다 (sudo 로 실행하세요)"
fi

# 1. Hailo PCI 장치 주소 찾기 (하드코딩하지 않는다: 슬롯이 바뀌어도 동작해야 한다)
DEVICE=""
for dir in "$PCI_ROOT"/devices/*; do
  [[ -r "$dir/vendor" ]] || continue
  if [[ "$(cat "$dir/vendor" 2>/dev/null)" == "$HAILO_VENDOR_ID" ]]; then
    DEVICE="$(basename "$dir")"
    break
  fi
done
if [[ -z "$DEVICE" ]]; then
  finish failed "PCI에서 Hailo 장치를 찾지 못했습니다 (전원/장착 확인 필요)"
fi
echo "DEVICE=$DEVICE"

# 2. 장치를 쥔 프로세스 정리. 운영자 UI(operator_ui)는 대상이 아니다.
holders() { fuser "$DEVICE_NODE" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true; }
if [[ -e "$DEVICE_NODE" ]]; then
  waited=0
  while [[ -n "$(holders)" && $waited -lt $HOLDER_WAIT_SECONDS ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  remaining="$(holders)"
  if [[ -n "$remaining" ]]; then
    if [[ $KILL_HOLDERS -eq 0 ]]; then
      finish failed "장치를 쥔 프로세스가 남아 있습니다: $(echo "$remaining" | tr '\n' ' ')"
    fi
    log "kill-holders"
    for pid in $remaining; do
      name="$(cat "/proc/$pid/comm" 2>/dev/null || echo unknown)"
      if [[ "$name" == *operator_ui* ]]; then
        finish failed "운영자 UI가 장치를 직접 쥐고 있습니다 (PID $pid). 먼저 추론을 중지하세요"
      fi
      run kill -TERM "$pid" 2>/dev/null || true
    done
    run sleep 3
    for pid in $(holders); do run kill -KILL "$pid" 2>/dev/null || true; done
    run sleep 1
  fi
fi

# 3. 드라이버 내리기 → PCI 제거 → 재스캔 → 드라이버 올리기
log "modprobe-remove"
if ! run modprobe -r hailo_pci; then
  finish failed "hailo_pci 모듈을 내리지 못했습니다 (장치를 쥔 프로세스가 드라이버에 묶였을 수 있음 → 재부팅 필요)"
fi
log "pci-remove"
run sh -c "echo 1 > '$PCI_ROOT/devices/$DEVICE/remove'" || finish failed "PCI 장치 제거 실패: $DEVICE"
run sleep 2
log "pci-rescan"
run sh -c "echo 1 > '$PCI_ROOT/rescan'" || finish failed "PCI 재스캔 실패"
run sleep 2
log "modprobe-load"
if ! run modprobe hailo_pci; then
  finish failed "hailo_pci 모듈을 다시 올리지 못했습니다"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  finish ok "dry-run: 실제 변경 없음"
fi

# 4. 장치 노드가 돌아올 때까지 기다린 뒤 칩이 응답하는지 확인
waited=0
while [[ ! -e "$DEVICE_NODE" && $waited -lt $NODE_WAIT_SECONDS ]]; do
  sleep 1
  waited=$((waited + 1))
done
if [[ ! -e "$DEVICE_NODE" ]]; then
  finish failed "$DEVICE_NODE 가 ${NODE_WAIT_SECONDS}초 안에 나타나지 않았습니다"
fi

log "identify"
if ! command -v hailortcli >/dev/null 2>&1; then
  finish ok "$DEVICE_NODE 복구됨 (hailortcli 가 없어 칩 응답은 확인하지 못함)"
fi
identify="$(timeout 20 hailortcli fw-control identify 2>&1)"
if grep -q "Device Architecture" <<<"$identify"; then
  finish ok "$(grep -m1 'Device Architecture' <<<"$identify" | tr -d '\r')"
fi
finish failed "칩이 여전히 응답하지 않습니다: $(grep -m1 -i 'error' <<<"$identify" | tail -c 160)"
