#!/usr/bin/env bash
# 등록된 자동 실행 서비스를 지금 시작한다.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/autorun-common.sh"

autorun_require_systemctl
autorun_require_installed
"$SYSTEMCTL" --user start "$AUTORUN_SERVICE_NAME"
autorun_status_line
echo "로그 보기: journalctl --user -u $AUTORUN_SERVICE_NAME -f"
