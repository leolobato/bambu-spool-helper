#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtualenv in $VENV_DIR with $PYTHON_BIN"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, httpx, jinja2, dotenv" >/dev/null 2>&1; then
  echo "Installing Python dependencies into $VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r requirements.txt
fi

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

PORT="${PORT:-9817}"

exec "$VENV_DIR/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
