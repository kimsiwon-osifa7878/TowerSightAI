#!/usr/bin/env bash
# TowerSightAI 자동 실행(systemd) 스크립트 공용 설정. 직접 실행하지 말고 source 해서 쓴다.
#
# 왜 사용자(user) 서비스인가: 운영자/사용자 화면은 PyQt6 GUI라 데스크톱 세션이 있어야 뜬다.
# 시스템 서비스로 올리면 화면 없이 실행되어 실패한다. 그래서 데스크톱 세션이 올라올 때
# 함께 시작되는 `graphical-session.target`에 붙인다.
#
# 이 스크립트들은 진단·운영 편의용이다. 안전 판정, PLC 출력, 최종 OK와는 무관하다.

set -euo pipefail

AUTORUN_ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AUTORUN_SERVICE_NAME="${TOWERSIGHTAI_SERVICE_NAME:-towersightai.service}"
AUTORUN_UNIT_DIR="${TOWERSIGHTAI_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
AUTORUN_UNIT_PATH="$AUTORUN_UNIT_DIR/$AUTORUN_SERVICE_NAME"
# 테스트에서 가짜 systemctl로 바꿔 끼울 수 있게 변수로 둔다.
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

autorun_require_systemctl() {
  if ! command -v "$SYSTEMCTL" >/dev/null 2>&1; then
    echo "systemctl을 찾을 수 없습니다: $SYSTEMCTL" >&2
    exit 2
  fi
}

autorun_require_installed() {
  if [[ ! -f "$AUTORUN_UNIT_PATH" ]]; then
    echo "자동 실행이 등록되어 있지 않습니다: $AUTORUN_UNIT_PATH" >&2
    echo "먼저 ./install_autorun.sh 를 실행하세요." >&2
    exit 3
  fi
}

autorun_status_line() {
  local enabled active
  enabled="$("$SYSTEMCTL" --user is-enabled "$AUTORUN_SERVICE_NAME" 2>/dev/null || true)"
  active="$("$SYSTEMCTL" --user is-active "$AUTORUN_SERVICE_NAME" 2>/dev/null || true)"
  echo "서비스: $AUTORUN_SERVICE_NAME | 부팅 시 자동 실행: ${enabled:-unknown} | 현재: ${active:-unknown}"
}
