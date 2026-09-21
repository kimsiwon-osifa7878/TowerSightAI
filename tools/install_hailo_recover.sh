#!/usr/bin/env bash
# 운영자 콘솔에서 Hailo 장치 복구를 실행할 수 있게 등록한다. 현장 장비에서 한 번만 실행:
#
#   sudo tools/install_hailo_recover.sh
#
# 하는 일
#   1) tools/hailo_recover.sh 를 /usr/local/sbin/towersightai-hailo-recover 로 복사(root 소유, 0755)
#   2) 그 경로 하나만 비밀번호 없이 실행할 수 있는 sudoers 규칙을 /etc/sudoers.d/ 에 추가
#
# 왜 복사하는가: 저장소 파일은 앱 사용자가 수정할 수 있다. 그 경로에 NOPASSWD를 주면 임의의
# 코드를 root로 실행할 수 있게 되므로, root 소유 사본을 만들고 그 사본만 허용한다.
# 스크립트를 고친 뒤에는 이 설치를 다시 실행해야 반영된다.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT_DIR/tools/hailo_recover.sh"
TARGET="${TOWERSIGHTAI_RECOVER_BIN:-/usr/local/sbin/towersightai-hailo-recover}"
SUDOERS_DIR="${TOWERSIGHTAI_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_FILE="$SUDOERS_DIR/towersightai-hailo-recover"
APP_USER="${TOWERSIGHTAI_APP_USER:-${SUDO_USER:-$(id -un)}}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "root 권한이 필요합니다: sudo tools/install_hailo_recover.sh" >&2
  exit 2
fi
if [[ ! -f "$SOURCE" ]]; then
  echo "복구 스크립트를 찾을 수 없습니다: $SOURCE" >&2
  exit 2
fi

install -o root -g root -m 0755 "$SOURCE" "$TARGET"
mkdir -p "$SUDOERS_DIR"
umask 077
cat > "$SUDOERS_FILE" <<RULE
# TowerSightAI: 운영자 콘솔의 'Hailo 장치 복구' 버튼. 이 경로 하나만 허용한다.
$APP_USER ALL=(root) NOPASSWD: $TARGET
RULE
chmod 0440 "$SUDOERS_FILE"

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$SUDOERS_FILE" >/dev/null || { rm -f "$SUDOERS_FILE"; echo "sudoers 문법 오류로 되돌렸습니다" >&2; exit 3; }
fi

echo "설치 완료"
echo "  실행 파일 : $TARGET"
echo "  sudoers   : $SUDOERS_FILE ($APP_USER)"
echo "확인: sudo -n $TARGET --dry-run"
