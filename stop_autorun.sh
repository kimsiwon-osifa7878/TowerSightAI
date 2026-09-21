#!/usr/bin/env bash
# 실행 중인 자동 실행 서비스를 멈춘다. 등록(부팅 시 자동 실행)은 그대로 남는다.
# 등록까지 빼려면 ./uninstall_autorun.sh 를 쓴다.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/autorun-common.sh"

autorun_require_systemctl
autorun_require_installed
"$SYSTEMCTL" --user stop "$AUTORUN_SERVICE_NAME"
autorun_status_line
