#!/usr/bin/env bash
# 부팅 시 자동 실행 등록: systemd 사용자 서비스 파일을 만들고 enable 한다.
# 사용: ./install_autorun.sh        (등록만. 바로 켜려면 ./start_autorun.sh)
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/autorun-common.sh"

autorun_require_systemctl
if [[ ! -x "$AUTORUN_ROOT_DIR/run.sh" ]]; then
  echo "run.sh 를 실행할 수 없습니다: $AUTORUN_ROOT_DIR/run.sh" >&2
  exit 2
fi

mkdir -p "$AUTORUN_UNIT_DIR"
cat > "$AUTORUN_UNIT_PATH" <<UNIT
[Unit]
Description=TowerSightAI 주차기 AI 안전감시
Documentation=file://$AUTORUN_ROOT_DIR/README.md
# GUI 앱이라 데스크톱 세션이 올라온 뒤에 시작하고, 세션이 내려가면 함께 내려간다.
After=graphical-session.target
PartOf=graphical-session.target
# 재시작 횟수 제한 없음: 감시가 꺼진 채 방치되는 것이 포기보다 나쁘다.
# (이 키는 [Unit] 에 있어야 한다. [Service] 에 두면 systemd가 조용히 무시한다.)
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$AUTORUN_ROOT_DIR
ExecStart=$AUTORUN_ROOT_DIR/run.sh
# 감시가 멈춘 채 방치되지 않도록 항상 되살린다.
# 실패가 이어지면 실행 로그와 NAS 데이터 공백으로 드러난다.
Restart=always
RestartSec=10
# 자식(Hailo 추론, 증거 녹화)은 이 유닛의 cgroup에 속하므로 함께 정리된다.
# 드라이버에 붙잡혀 안 죽는 경우를 대비해 30초 뒤 강제 종료한다.
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=graphical-session.target
UNIT

"$SYSTEMCTL" --user daemon-reload
"$SYSTEMCTL" --user enable "$AUTORUN_SERVICE_NAME"

echo "등록 완료: $AUTORUN_UNIT_PATH"
autorun_status_line
echo
echo "확인할 것:"
echo "  1) 자동 로그인이 켜져 있어야 부팅만으로 화면이 뜹니다 (설정 > 사용자)."
echo "  2) 지금 바로 시작하려면: ./start_autorun.sh"
echo "  3) 로그 보기: journalctl --user -u $AUTORUN_SERVICE_NAME -f"
