#!/usr/bin/env bash
# 자동 실행 등록 해제: 서비스를 멈추고 disable 한 뒤 유닛 파일을 지운다.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/autorun-common.sh"

autorun_require_systemctl
if [[ ! -f "$AUTORUN_UNIT_PATH" ]]; then
  echo "등록되어 있지 않습니다: $AUTORUN_UNIT_PATH"
  exit 0
fi

"$SYSTEMCTL" --user disable --now "$AUTORUN_SERVICE_NAME" || true
rm -f "$AUTORUN_UNIT_PATH"
"$SYSTEMCTL" --user daemon-reload

echo "등록 해제 완료: $AUTORUN_UNIT_PATH 삭제"
echo "앱은 기존처럼 ./run.sh 로 직접 실행할 수 있습니다."
