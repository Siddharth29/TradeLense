#!/usr/bin/env bash

# Simple helper to run the FastAPI web application.

set -euo pipefail

PORT=${PORT:-8000}
HOST=${HOST:-0.0.0.0}

uvicorn apps.backend.api:app --host "$HOST" --port "$PORT" "$@"

