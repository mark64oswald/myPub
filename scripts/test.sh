#!/usr/bin/env bash
# scripts/test.sh — canonical test runner for myPub.
#
# Runs the full pytest suite from the project root with the venv
# interpreter, honoring a few common flags. Use this from pre-commit
# hooks or in any "did I break anything?" check — a single entry
# point so we don't have to remember path + env each time.
#
# Usage:
#   scripts/test.sh                      # full suite
#   scripts/test.sh -k resolve           # pytest -k filter passthrough
#   scripts/test.sh tests/test_schema.py # only this file
#   scripts/test.sh --cov                # with coverage (if pytest-cov installed)
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project venv; fall back to the PATH python3 if not set up.
if [[ -x ".venv/bin/python3" ]]; then
    PY=".venv/bin/python3"
else
    echo "warning: .venv not found — using system python3" >&2
    PY="python3"
fi

exec "$PY" -m pytest "$@"
