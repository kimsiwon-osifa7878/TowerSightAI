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
SIMULATION_SCREENSHOT="$OUT_DIR/operator-ui-simulation-$STAMP.png"
LD2410_SCREENSHOT="$OUT_DIR/operator-ui-ld2410-$STAMP.png"
DRIVER_TEST_SCREENSHOT="$OUT_DIR/operator-ui-driver-test-$STAMP.png"
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

  click_at() {
    local rel_x="$1"
    local rel_y="$2"
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove --window "$WINDOW_ID" "$rel_x" "$rel_y" click 1
    sleep 1
  }

  HOTSPOT_X=$((_WINDOW_W - 36))
  enter_operator() {
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove --window "$WINDOW_ID" "$HOTSPOT_X" 36
    xdotool mousedown 1
    sleep 3
    xdotool mouseup 1
    sleep 1
  }

  enter_operator
  gnome-screenshot -f "$OPERATOR_SCREENSHOT"
  identify "$OPERATOR_SCREENSHOT"

  # xdotool --window coordinates are relative to the Qt client area.
  click_at 50 65
  gnome-screenshot -f "$SIDEBAR_SCREENSHOT"
  identify "$SIDEBAR_SCREENSHOT"

  click_at 150 162
  gnome-screenshot -f "$ALL_CAMERAS_SCREENSHOT"
  identify "$ALL_CAMERAS_SCREENSHOT"

  click_at 150 98
  gnome-screenshot -f "$USER_AFTER_ALL_CAMERAS_SCREENSHOT"
  identify "$USER_AFTER_ALL_CAMERAS_SCREENSHOT"

  enter_operator
  click_at 150 542
  gnome-screenshot -f "$PERSON_SCREENSHOT"
  identify "$PERSON_SCREENSHOT"

  click_at 150 98
  gnome-screenshot -f "$USER_AFTER_PERSON_SCREENSHOT"
  identify "$USER_AFTER_PERSON_SCREENSHOT"

  enter_operator
  click_at 150 606
  gnome-screenshot -f "$LD2410_SCREENSHOT"
  identify "$LD2410_SCREENSHOT"

  click_at 150 670
  gnome-screenshot -f "$SIMULATION_SCREENSHOT"
  identify "$SIMULATION_SCREENSHOT"

  click_at 150 734
  gnome-screenshot -f "$DRIVER_TEST_SCREENSHOT"
  identify "$DRIVER_TEST_SCREENSHOT"
fi

echo "$SCREENSHOT"
