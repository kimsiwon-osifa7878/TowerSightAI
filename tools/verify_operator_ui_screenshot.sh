#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
OUT_DIR="${2:-tmp/operator-ui-verification}"
WAIT_SECONDS="${WAIT_SECONDS:-8}"
WINDOW_TITLE="${WINDOW_TITLE:-TowerSightAI Operator Console}"

mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
SCREENSHOT="$OUT_DIR/operator-ui-$STAMP.png"
SIDEBAR_SCREENSHOT="$OUT_DIR/operator-ui-sidebar-$STAMP.png"
ALL_CAMERAS_SCREENSHOT="$OUT_DIR/operator-ui-all-cameras-$STAMP.png"
PERSON_SCREENSHOT="$OUT_DIR/operator-ui-person-presence-$STAMP.png"
SIMULATION_SCREENSHOT="$OUT_DIR/operator-ui-simulation-$STAMP.png"
EMPTY_SCREENSHOT="$OUT_DIR/operator-ui-empty-$STAMP.png"
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
  "$PYTHON" -m towersightai.cli.operator_ui --env "$ENV_FILE" --windowed >"$LOG_FILE" 2>&1 &
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
sleep 1

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

  click_at() {
    local rel_x="$1"
    local rel_y="$2"
    xdotool windowactivate "$WINDOW_ID" 2>/dev/null || true
    xdotool mousemove $((WINDOW_X + rel_x)) $((WINDOW_Y + rel_y)) click 1
    sleep 1
  }

  click_at 45 35
  gnome-screenshot -f "$SIDEBAR_SCREENSHOT"
  identify "$SIDEBAR_SCREENSHOT"

  click_at 150 105
  gnome-screenshot -f "$ALL_CAMERAS_SCREENSHOT"
  identify "$ALL_CAMERAS_SCREENSHOT"

  click_at 150 295
  gnome-screenshot -f "$PERSON_SCREENSHOT"
  identify "$PERSON_SCREENSHOT"

  click_at 150 333
  gnome-screenshot -f "$SIMULATION_SCREENSHOT"
  identify "$SIMULATION_SCREENSHOT"

  click_at 150 371
  gnome-screenshot -f "$EMPTY_SCREENSHOT"
  identify "$EMPTY_SCREENSHOT"
fi

echo "$SCREENSHOT"
