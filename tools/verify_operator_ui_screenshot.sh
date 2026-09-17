#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
OUT_DIR="${2:-tmp/operator-ui-verification}"
WAIT_SECONDS="${WAIT_SECONDS:-8}"
USER_WAIT_SECONDS="${USER_WAIT_SECONDS:-5}"
WINDOW_TITLE="${WINDOW_TITLE:-TowerSightAI Operator Console}"
UI_MODE="${3:-${UI_MODE:-windowed}}"

if [[ "$UI_MODE" != "windowed" && "$UI_MODE" != "fullscreen" ]]; then
  echo "UI_MODE must be either windowed or fullscreen." >&2
  exit 2
fi

mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
SCREENSHOT="$OUT_DIR/user-ui-$STAMP.png"
OPERATOR_SCREENSHOT="$OUT_DIR/operator-ui-$STAMP.png"
SIDEBAR_SCREENSHOT="$OUT_DIR/operator-ui-sidebar-$STAMP.png"
ALL_CAMERAS_SCREENSHOT="$OUT_DIR/operator-ui-all-cameras-$STAMP.png"
USER_AFTER_ALL_CAMERAS_SCREENSHOT="$OUT_DIR/user-ui-after-all-cameras-$STAMP.png"
PERSON_SCREENSHOT="$OUT_DIR/operator-ui-person-presence-$STAMP.png"
USER_AFTER_PERSON_SCREENSHOT="$OUT_DIR/user-ui-after-person-presence-$STAMP.png"
LOG_SCREENSHOT="$OUT_DIR/operator-ui-log-$STAMP.png"
LD2410_SCREENSHOT="$OUT_DIR/operator-ui-ld2410-$STAMP.png"
DRIVER_TEST_SCREENSHOT="$OUT_DIR/operator-ui-driver-test-$STAMP.png"
PROCESS_SETTINGS_SCREENSHOT="$OUT_DIR/operator-ui-process-settings-$STAMP.png"
GROUND_SCREENSHOT="$OUT_DIR/operator-ui-ground-points-$STAMP.png"
OPERATOR_BUTTON_SCREENSHOT="$OUT_DIR/operator-ui-from-user-button-$STAMP.png"
EXIT_CONFIRM_SCREENSHOT="$OUT_DIR/operator-ui-exit-confirm-$STAMP.png"
LOG_FILE="$OUT_DIR/operator-ui-$STAMP.log"

PYTHON=""
for candidate in ".venv/bin/python" "python3"; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import cv2; import PyQt6" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "No Python runtime with both PyQt6 and cv2 is available." >&2
  echo "Install the UI extra or run with a Python that can import PyQt6 and cv2." >&2
  exit 2
fi

if [[ -z "${QT_QPA_PLATFORM:-}" ]]; then
  if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    QT_QPA_PLATFORM="wayland"
  elif [[ -n "${DISPLAY:-}" ]]; then
    QT_QPA_PLATFORM="xcb"
  else
    echo "No GUI display session is available for screenshot verification." >&2
    echo "Set DISPLAY for X11 or WAYLAND_DISPLAY for Wayland, then rerun this script." >&2
    exit 2
  fi
fi

cleanup() {
  if [[ -n "${APP_PID:-}" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    # A leftover UI keeps camera RTSP sessions and starves later inference runs
    # (per-camera concurrent-session limit), so force-kill if it survives SIGTERM.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$APP_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$APP_PID" 2>/dev/null; then
      kill -9 "$APP_PID" 2>/dev/null || true
    fi
    wait "$APP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

QT_QPA_PLATFORM="$QT_QPA_PLATFORM" \
  "$PYTHON" -m towersightai.cli.operator_ui --env "$ENV_FILE" "--$UI_MODE" >"$LOG_FILE" 2>&1 &
APP_PID="$!"

for _ in $(seq 1 "$WAIT_SECONDS"); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "operator ui exited before screenshot; see $LOG_FILE" >&2
    sed -n '1,80p' "$LOG_FILE" >&2 || true
    exit 2
  fi
  if xdotool search --name "$WINDOW_TITLE" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

WINDOW_ID="$(xdotool search --name "$WINDOW_TITLE" 2>/dev/null | head -n 1 || true)"
if [[ -n "$WINDOW_ID" ]]; then
  xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
fi
sleep "$USER_WAIT_SECONDS"

gnome-screenshot -f "$SCREENSHOT"
identify "$SCREENSHOT"

if [[ -n "$WINDOW_ID" ]]; then
  read -r WINDOW_X WINDOW_Y _WINDOW_W _WINDOW_H < <(xdotool getwindowgeometry --shell "$WINDOW_ID" | awk -F= '
    $1 == "X" {x=$2}
    $1 == "Y" {y=$2}
    $1 == "WIDTH" {w=$2}
    $1 == "HEIGHT" {h=$2}
    END {print x, y, w, h}
  ')
  echo "Window geometry: ${_WINDOW_W}x${_WINDOW_H}+${WINDOW_X}+${WINDOW_Y}"
  WINDOW_STATE="$(xprop -id "$WINDOW_ID" _NET_WM_STATE 2>/dev/null || true)"
  if [[ "$UI_MODE" == "fullscreen" && "$WINDOW_STATE" != *"_NET_WM_STATE_FULLSCREEN"* ]]; then
    echo "UI window did not enter true fullscreen mode: $WINDOW_STATE" >&2
    exit 2
  fi
  if [[ "$UI_MODE" == "windowed" ]] && ((_WINDOW_W > 1920 || _WINDOW_H > 1024)); then
    echo "UI window exceeds the 1920x1024 safety bound." >&2
    exit 2
  fi

  # BoundedContentViewport caps the UI canvas at 1920x1024 and centers it, so a
  # fullscreen window is larger than the clickable content. Every coordinate below is
  # content-relative and click_at() adds the centering offset.
  CONTENT_W=$(( _WINDOW_W < 1920 ? _WINDOW_W : 1920 ))
  CONTENT_H=$(( _WINDOW_H < 1024 ? _WINDOW_H : 1024 ))
  OFFSET_X=$(( (_WINDOW_W - CONTENT_W) / 2 ))
  OFFSET_Y=$(( (_WINDOW_H - CONTENT_H) / 2 ))
  echo "Content canvas: ${CONTENT_W}x${CONTENT_H}+${OFFSET_X}+${OFFSET_Y}"

  # A coordinate outside the content canvas used to be clicked anyway: on 2026-09-17 the exit
  # row at y=1007 fell past a 900 px tall window and the click landed on the browser behind the
  # app. Refuse instead — a verification script must never click whatever happens to be under
  # the pointer on the desktop.
  click_at() {
    local x=$1
    local y=$2
    if (( x < 0 || x >= CONTENT_W || y < 0 || y >= CONTENT_H )); then
      echo "click_at ${x},${y} is outside the ${CONTENT_W}x${CONTENT_H} content canvas." >&2
      echo "Run this script in fullscreen on a >=1920x1080 screen, or re-measure the sidebar rows." >&2
      exit 2
    fi
    local rel_x=$(( OFFSET_X + x ))
    local rel_y=$(( OFFSET_Y + y ))
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove --window "$WINDOW_ID" "$rel_x" "$rel_y" click 1
    sleep 1
  }

  # The sidebar scrolls: the 시스템 section sits below the viewport even on a 1024 px canvas,
  # so the last rows have to be scrolled into view before they can be clicked.
  # `xdotool click --window` does not deliver wheel events to Qt scroll areas, so the pointer is
  # moved in absolute screen coordinates and the wheel is sent globally.
  scroll_sidebar_to_bottom() {
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove $(( WINDOW_X + OFFSET_X + 150 )) $(( WINDOW_Y + OFFSET_Y + 600 ))
    sleep 1
    for _ in $(seq 1 15); do
      xdotool click 5
      sleep 0.05
    done
    sleep 1
  }

  HOTSPOT_X=$(( OFFSET_X + CONTENT_W - 36 ))
  HOTSPOT_Y=$(( OFFSET_Y + 36 ))
  enter_operator() {
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove --window "$WINDOW_ID" "$HOTSPOT_X" "$HOTSPOT_Y"
    xdotool mousedown 1
    sleep 3
    xdotool mouseup 1
    sleep 1
  }

  # Visible bottom-right service control added for on-site operator entry.
  enter_operator_by_button() {
    click_at $(( CONTENT_W - 60 )) $(( CONTENT_H - 72 ))
  }

  enter_operator
  gnome-screenshot -f "$OPERATOR_SCREENSHOT"
  identify "$OPERATOR_SCREENSHOT"

  # xdotool --window coordinates are relative to the Qt client area.
  #
  # Sidebar row centres, measured 2026-09-17 by rendering OperatorWindow at the 1920x1024 content
  # canvas and reading each button's geometry, so they are not guesses. They are only valid at
  # that canvas size: the sidebar scrolls, so on a shorter window the lower rows are not visible
  # at all. Run `UI_MODE=fullscreen` on a 1920x1080 screen for the full sweep; click_at() now
  # refuses anything outside the canvas rather than clicking the desktop.
  #
  #   127 사용자 화면 · 181 감시 설정 · 235 주차 프로세스 테스트
  #   324 전체 카메라 · 378 차량 감지 · 432 사람 감지 · 486 번호판 인식 · 540 레이더 (LD2410)
  #   594 NAS 연결 확인 · 648 NAS 파일 전송 · 702 카메라 캘리브레이션 · 756 지면 기준점
  #   810 시스템 점검 · 864 실행 로그
  #   953 카메라 설정 · 1007 프로그램 종료
  #
  # Re-measure the same way whenever SIDEBAR_SECTIONS changes — the old hardcoded values had
  # drifted several rows and were clicking the wrong pages (see CLAUDE.md gotchas).
  click_at 50 65
  gnome-screenshot -f "$SIDEBAR_SCREENSHOT"
  identify "$SIDEBAR_SCREENSHOT"

  click_at 150 324
  gnome-screenshot -f "$ALL_CAMERAS_SCREENSHOT"
  identify "$ALL_CAMERAS_SCREENSHOT"

  click_at 150 127
  gnome-screenshot -f "$USER_AFTER_ALL_CAMERAS_SCREENSHOT"
  identify "$USER_AFTER_ALL_CAMERAS_SCREENSHOT"

  enter_operator
  click_at 150 432
  gnome-screenshot -f "$PERSON_SCREENSHOT"
  identify "$PERSON_SCREENSHOT"

  click_at 150 127
  gnome-screenshot -f "$USER_AFTER_PERSON_SCREENSHOT"
  identify "$USER_AFTER_PERSON_SCREENSHOT"

  enter_operator
  click_at 150 540
  gnome-screenshot -f "$LD2410_SCREENSHOT"
  identify "$LD2410_SCREENSHOT"

  click_at 150 864
  gnome-screenshot -f "$LOG_SCREENSHOT"
  identify "$LOG_SCREENSHOT"

  click_at 150 181
  gnome-screenshot -f "$PROCESS_SETTINGS_SCREENSHOT"
  identify "$PROCESS_SETTINGS_SCREENSHOT"

  click_at 150 235
  gnome-screenshot -f "$DRIVER_TEST_SCREENSHOT"
  identify "$DRIVER_TEST_SCREENSHOT"

  click_at 150 756
  gnome-screenshot -f "$GROUND_SCREENSHOT"
  identify "$GROUND_SCREENSHOT"

  # Operator menu exit control, the last row of the scrolled sidebar. Escape dismisses the
  # confirmation so the app survives.
  scroll_sidebar_to_bottom
  click_at 150 $(( CONTENT_H - 64 ))
  gnome-screenshot -f "$EXIT_CONFIRM_SCREENSHOT"
  identify "$EXIT_CONFIRM_SCREENSHOT"
  xdotool key --clearmodifiers Escape
  sleep 1
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "operator ui exited after the cancelled shutdown confirmation; see $LOG_FILE" >&2
    exit 2
  fi

  # Return to user mode and re-enter operator mode with the visible bottom-right button.
  click_at 150 127
  sleep 1
  enter_operator_by_button
  gnome-screenshot -f "$OPERATOR_BUTTON_SCREENSHOT"
  identify "$OPERATOR_BUTTON_SCREENSHOT"
fi

echo "$SCREENSHOT"
