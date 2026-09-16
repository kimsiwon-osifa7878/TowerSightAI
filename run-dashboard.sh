#!/usr/bin/env bash
# TowerSightAI 데이터 분석 대시보드 (개발·검증 전용, 로컬 실행).
#   ./run-dashboard.sh                 # http://127.0.0.1:8765 를 브라우저로 연다
#   PORT=8800 ./run-dashboard.sh       # 다른 포트
#   AUTO_SYNC_MINUTES=0 ./run-dashboard.sh   # NAS 자동 최신화 끄기
#   NO_OPEN=1 ./run-dashboard.sh       # 브라우저를 열지 않음
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
PORT="${PORT:-8765}"
AUTO_SYNC_MINUTES="${AUTO_SYNC_MINUTES:-10}"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "TowerSightAI Python venv is missing: $VENV_PYTHON" >&2
  echo "Create .venv with Python 3.12 and run: python -m pip install -e \".[ui]\"" >&2
  exit 2
fi

# The console script appears only after an editable install that includes it; fall back to -m.
if [[ -x "$ROOT_DIR/.venv/bin/towersightai-analyze" ]]; then
  CMD=("$ROOT_DIR/.venv/bin/towersightai-analyze")
else
  CMD=("$VENV_PYTHON" -m towersightai.cli.analyze)
fi

if [[ ! -f "$ROOT_DIR/data/analysis/sites.json" ]]; then
  echo "등록된 현장(NAS)이 없습니다. 브라우저의 '데이터 · NAS 설정' 페이지에서 .env를 가져오거나 주소를 입력하세요." >&2
  if [[ -f "$ROOT_DIR/.env" ]]; then
    echo "  또는: $ROOT_DIR/.venv/bin/towersightai-analyze sites import-env <site-name> --env .env --default-host <현장기 호스트명>" >&2
  fi
fi

# Replace a dashboard instance that is already serving this port (its own process only —
# never anything else that happens to listen there).
existing=$(pgrep -f "towersightai.cli.analyze serve.*--port ${PORT}( |$)|towersightai-analyze serve.*--port ${PORT}( |$)" || true)
if [[ -n "$existing" ]]; then
  echo "기존 대시보드 서버(PID $existing)를 종료합니다." >&2
  kill $existing 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 $existing 2>/dev/null; then break; fi
    sleep 0.25
  done
  kill -9 $existing 2>/dev/null || true
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q ":${PORT}"; then
  echo "포트 ${PORT}를 다른 프로그램이 사용 중입니다. PORT=<다른 포트> ./run-dashboard.sh 로 실행하세요." >&2
  exit 2
fi

ARGS=(serve --port "$PORT" --auto-sync-minutes "$AUTO_SYNC_MINUTES")
if [[ -z "${NO_OPEN:-}" ]]; then
  ARGS+=(--open)
fi

cd "$ROOT_DIR"
exec "${CMD[@]}" "${ARGS[@]}"
