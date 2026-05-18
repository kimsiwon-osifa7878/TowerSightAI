#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
OUT_DIR="${2:-tmp/operator-ui-verification}"
WAIT_SECONDS="${WAIT_SECONDS:-8}"
WINDOW_TITLE="${WINDOW_TITLE:-TowerSightAI Operator Console}"

mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
SCREENSHOT="$OUT_DIR/operator-ui-$STAMP.png"
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

cleanup() {
  if [[ -n "${APP_PID:-}" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}" \
  "$PYTHON" -m towersightai.cli.operator_ui --env "$ENV_FILE" --windowed >"$LOG_FILE" 2>&1 &
APP_PID="$!"

for _ in $(seq 1 "$WAIT_SECONDS"); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "operator ui exited before screenshot; see $LOG_FILE" >&2
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
echo "$SCREENSHOT"
