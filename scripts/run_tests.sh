#!/usr/bin/env bash

# Temporary Codex workaround: run tests through an elevated wrapper until the
# Home Assistant pytest hang inside the default sandbox is understood and fixed.
# Remove this script once sandboxed pytest runs are reliable again.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${WATTPLAN_TEST_VENV:-/tmp/wattplan-venv}"
BASE_TEMP_DIR="${WATTPLAN_TEST_BASETEMP:-/tmp/wattplan-pytest}"

export TMPDIR=/tmp
export TEMP=/tmp
export TMP=/tmp

cd "$ROOT_DIR"
exec "$VENV_DIR/bin/pytest" --basetemp="$BASE_TEMP_DIR" "$@"
