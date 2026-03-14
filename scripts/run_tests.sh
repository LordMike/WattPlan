#!/usr/bin/env bash

# Temporary Codex workaround: run tests through an elevated wrapper until the
# Home Assistant pytest hang inside the default sandbox is understood and fixed.
# Remove this script once sandboxed pytest runs are reliable again.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE_KEY="$(printf '%s' "$ROOT_DIR" | cksum | awk '{print $1}')"
DEFAULT_VENV_DIR="/tmp/wattplan-venv-$WORKTREE_KEY"
DEFAULT_BASE_TEMP_DIR="/tmp/wattplan-pytest-$WORKTREE_KEY"
VENV_DIR="${WATTPLAN_TEST_VENV:-$DEFAULT_VENV_DIR}"
BASE_TEMP_DIR="${WATTPLAN_TEST_BASETEMP:-$DEFAULT_BASE_TEMP_DIR}"

export TMPDIR=/tmp
export TEMP=/tmp
export TMP=/tmp

cd "$ROOT_DIR"
exec "$VENV_DIR/bin/pytest" --basetemp="$BASE_TEMP_DIR" "$@"
