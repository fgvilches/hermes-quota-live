#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${HERMES_PYTHON:-/Users/shift/.hermes/hermes-agent/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

cd "$REPO"
"$PY" -m unittest discover -s tests -v
node --check desktop/plugin.js
if command -v hermes >/dev/null 2>&1; then
  hermes plugins doctor "$REPO" --ci
fi
