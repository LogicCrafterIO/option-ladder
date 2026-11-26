#!/usr/bin/env bash
# Start the option ladder (http://127.0.0.1:8795 by default).
#
# Uses the workspace virtualenv when it already carries the deps — that is the
# interpreter every other tool in this repo runs under, so a ladder built here
# and a ladder built in a skill agree. Falls back to its own venv, then to
# whatever python3 is on PATH.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8795}"
HOST="${HOST:-127.0.0.1}"

pick_python() {
  for cand in "../.venv/bin/python" ".venv/bin/python" "$(command -v python3 || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, numpy
PY
    then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "· no interpreter with fastapi/uvicorn/numpy — creating .venv here"
  python3 -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  PY=".venv/bin/python"
fi

echo "· option-ladder → http://${HOST}:${PORT}  (python: ${PY})"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level warning
