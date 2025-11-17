#!/bin/sh
set -e

# Environment defaults (already set in Dockerfile, but allow overrides)
APP_MODULE="${APP_MODULE:-app.main:app}"
APP_HOST="${HOST:-0.0.0.0}"
APP_PORT="${PORT:-8000}"
MOCK_HOST="${MOCK_HOST:-127.0.0.1}"
MOCK_PORT="${MOCK_PORT:-8085}"

echo "Starting main API: ${APP_MODULE} on ${APP_HOST}:${APP_PORT}"
uvicorn "${APP_MODULE}" --host "${APP_HOST}" --port "${APP_PORT}" --workers 2 &
MAIN_PID=$!

echo "Starting mock API on ${MOCK_HOST}:${MOCK_PORT}"
# simulation-api directory name contains a dash; use --app-dir to import module
uvicorn --app-dir simulation-api mock_api:app --host "${MOCK_HOST}" --port "${MOCK_PORT}" &
MOCK_PID=$!

term_handler() {
  echo "Shutting down services..."
  kill "${MAIN_PID}" "${MOCK_PID}" 2>/dev/null || true
  wait "${MAIN_PID}" "${MOCK_PID}" 2>/dev/null || true
}
trap term_handler TERM INT

# If main API exits, stop mock too and propagate exit code
wait "${MAIN_PID}"
STATUS=$?
kill "${MOCK_PID}" 2>/dev/null || true
wait "${MOCK_PID}" 2>/dev/null || true
exit ${STATUS}


