#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
ENV_FILE="${TOWERSIGHTAI_ENV_FILE:-$ROOT_DIR/.env}"
UI_MODE="${TOWERSIGHTAI_UI_MODE:---fullscreen}"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "TowerSightAI Python venv is missing: $VENV_PYTHON" >&2
  echo "Create .venv with Python 3.12 and install the project UI dependencies." >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "TowerSightAI environment file is missing: $ENV_FILE" >&2
  exit 2
fi

case "$UI_MODE" in
  --fullscreen|--windowed) ;;
  *)
    echo "Invalid TowerSightAI UI mode: $UI_MODE" >&2
    exit 2
    ;;
esac

mkdir -p "$ROOT_DIR/tmp"

# Do not inherit the legacy /opt TAPPAS plugin stack into the verified 5.1 runtime.
unset GST_PLUGIN_PATH
unset LD_LIBRARY_PATH
export GST_REGISTRY="$ROOT_DIR/tmp/towersightai-gstreamer-5.1.registry.bin"

cd "$ROOT_DIR"
exec "$VENV_PYTHON" -m towersightai.cli.operator_ui --env "$ENV_FILE" "$UI_MODE"
